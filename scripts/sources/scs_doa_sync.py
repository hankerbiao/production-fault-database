#!/usr/bin/env python3
"""Synchronize SCS DOA declarations and linked equipment information.

The SCS pages are server-rendered HTML.  A login creates a normal HTTP
session, after which the DOA list, declaration detail, service-order equipment
list, and product-entity pages can be fetched without browser automation.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from pymongo import ASCENDING, MongoClient, UpdateOne

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sources.hana.hana_view_sync import (
    load_dotenv,
    mongo_client_options,
    mongo_uri,
)

ROOT = PROJECT_ROOT
BASE_URL = "https://scs.sugon.com/prod"
LOGIN_PATH = "/ssocontroller"
LIST_PATH = "/PM_sp_doa_Result.pml"
DOA_DETAIL_PATH = "/PM_sp_doa_control_main_01.pml"
SERVICE_PATH = "/PM_so_service_order_control_static_tab_01.pml"
PRODUCT_PATH = "/PM_pd_product_entity_05_control_main_new_window.pml"
CHECKPOINT_ID = "scs_doa"
LIST_FORM_ID = "a8a80e69292b8364e0192bd28d8220c1c"
DEFAULT_COLLECTION = "scs_doa_records"
DEFAULT_CHECKPOINTS = "sync_checkpoints"
LOGGER = logging.getLogger("scs_doa_sync")

LIST_FIELDS = (
    "doa_code doa_type declare_reason service_uid status doa_judge customer_uid "
    "sugon_sn spare_part_sn product_name review_name shipment_date "
    "problem_batch_number engineer_uid detail_problem problem_conclusion "
    "declare_uid declare_time review_time business_unit review_uid "
    "acceptance_time acceptance_uid"
).split()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def text(value: Any) -> str:
    return str(value or "").strip()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Throttle every SCS request to avoid issuing a burst of traffic."""
    minimum = float(env("SCS_REQUEST_DELAY_MIN", "3"))
    maximum = float(env("SCS_REQUEST_DELAY_MAX", "5"))
    if minimum < 0 or maximum < minimum:
        raise ValueError("SCS_REQUEST_DELAY_MIN/MAX 配置无效")
    time.sleep(random.uniform(minimum, maximum))
    return client.request(method, url, **kwargs)


class ListParser(HTMLParser):
    """Extract one named table from the supplied form without bs4."""

    def __init__(self, form_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.form_depth = 0
        self.table_depth = 0
        self.in_cell = False
        self.cell_tag = ""
        self.cell_parts: list[str] = []
        self.cell_link: str | None = None
        self.headers: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.current: list[dict[str, Any]] = []
        self.current_attrs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {k: v or "" for k, v in attrs}
        if tag == "form" and attrs_map.get("id") == self.form_id:
            self.form_depth = 1
        elif self.form_depth and tag == "form":
            self.form_depth += 1
        if self.form_depth and tag == "table":
            self.table_depth += 1
        if self.table_depth and tag in {"th", "td"}:
            self.in_cell, self.cell_tag = True, tag
            self.cell_parts, self.cell_link = [], None
            self.cell_attrs = attrs_map
        if self.form_depth and tag == "tr":
            self.current_attrs = attrs_map
        if self.in_cell and tag == "a":
            self.cell_link = attrs_map.get("href") or self.cell_link

    def handle_endtag(self, tag: str) -> None:
        if self.in_cell and tag == self.cell_tag:
            value = normalize_text("".join(self.cell_parts))
            if self.cell_tag == "th":
                name = self.cell_attrs.get("name") or self.cell_attrs.get("id") or value
                if text(name):
                    self.headers.append(text(name))
            else:
                self.current.append({"value": value, "href": self.cell_link})
            self.in_cell = False
        if self.form_depth and tag == "tr" and self.current:
            if self.headers and len(self.current) >= len(self.headers):
                row = {key: self.current[i]["value"] for i, key in enumerate(self.headers)}
                first = self.current[0]
                row["_context_value"] = self.current_attrs.get("value", "")
                row["_detail_href"] = first.get("href") or ""
                self.rows.append(row)
            self.current = []
            self.current_attrs = {}
        if self.form_depth and tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
        if self.form_depth and tag == "form":
            self.form_depth = max(0, self.form_depth - 1)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.in_cell and tag == "br":
            self.cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)

def parse_list(html_text: str) -> list[dict[str, Any]]:
    parser = ListParser(LIST_FORM_ID)
    parser.feed(html_text)
    return parser.rows


class LabelParser(HTMLParser):
    """Read label/value pairs represented as adjacent table cells."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pending_label = ""
        self.in_th = False
        self.in_td = False
        self.parts: list[str] = []
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"th", "td"}:
            self.parts = []
            self.in_th, self.in_td = tag == "th", tag == "td"

    def handle_endtag(self, tag: str) -> None:
        if tag == "th" and self.in_th:
            self.pending_label = normalize_text("".join(self.parts)).rstrip(":")
            self.in_th = False
        elif tag == "td" and self.in_td:
            if self.pending_label:
                self.values[self.pending_label] = normalize_text("".join(self.parts))
            self.in_td = False

    def handle_data(self, data: str) -> None:
        if self.in_th or self.in_td:
            self.parts.append(data)


def parse_labels(html_text: str) -> dict[str, str]:
    parser = LabelParser()
    parser.feed(html_text)
    return parser.values


def parse_context_value(href: str) -> str:
    match = re.search(r"contextValue=([^&'}]+)", href or "")
    return unquote(match.group(1)) if match else ""


def parse_pml_context(html_text: str, pml_name: str) -> str:
    match = re.search(rf"['\"]pml['\"]\s*:\s*['\"]{re.escape(pml_name)}.*?contextValue=([A-Za-z0-9]+)", html_text, re.S)
    return unquote(match.group(1)) if match else ""


def login(client: httpx.Client) -> None:
    username, password = env("SCS_USERNAME"), env("SCS_PASSWORD")
    if not username or not password:
        raise RuntimeError("缺少 SCS_USERNAME 或 SCS_PASSWORD")
    response = request(client, "POST", BASE_URL + LOGIN_PATH, data={
        "name": username,
        "password": password,
        "randcode": env("SCS_RANDCODE"),
        "contextServiceName": "do_org_user_findbynameandpwd_sugon",
        "mobileclient": "true",
        "clientstamp": str(int(datetime.now().timestamp() * 1000)),
    })
    response.raise_for_status()
    body = response.json()
    if body.get("echoStr") != "success":
        raise RuntimeError(f"SCS 登录失败: {body}")


def fetch_list(client: httpx.Client, start: str = "", end: str = "", page_size: int = 500) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {
            "1": "1", "clientstamp": str(int(datetime.now().timestamp() * 1000)),
            "doa_code": "", "service_uid": "", "customer_uid": "", "sugon_sn_multi": "",
            "contract": "", "declare_time": start, "declare_time2": end,
            "detail_problem": "", "problem_conclusion": "", "pageNo": page,
            "pageSize": page_size, "target": "PM_sp_doa_Result",
        }
        try:
            response = request(client, "GET", BASE_URL + LIST_PATH, params=params)
        except httpx.ReadTimeout:
            if page > 1 and rows:
                LOGGER.warning("SCS 列表第 %d 页读取超时，保留已读取的 %d 行", page, len(rows))
                break
            raise
        response.raise_for_status()
        batch = parse_list(response.text)
        LOGGER.info("SCS DOA 列表进度：第 %d 页，当前累计 %d 条", page, len(rows) + len(batch))
        if not batch:
            LOGGER.info("SCS DOA 列表读取完成：共 %d 条", len(rows))
            break
        rows.extend(batch)
        if len(batch) < page_size:
            LOGGER.info("SCS DOA 列表读取完成：共 %d 条", len(rows))
            break
        page += 1
    return rows


def fetch_detail(client: httpx.Client, row: dict[str, Any]) -> dict[str, Any]:
    context = text(row.get("_context_value")) or parse_context_value(text(row.get("_detail_href")))
    result: dict[str, Any] = {"context_value": context}
    if not context:
        return result
    detail = request(client, "GET", BASE_URL + DOA_DETAIL_PATH, params={"dataBus": "setContext", "contextKey": "sp_doa", "contextValue": context})
    detail.raise_for_status()
    result["fields"] = parse_labels(detail.text)
    service_uid = text(row.get("service_uid"))
    if service_uid:
        service_context = parse_pml_context(detail.text, "PM_so_service_order_control_main") or parse_pml_context(detail.text, "PM_so_service_order_browse_only_show")
        service = request(client, "GET", BASE_URL + SERVICE_PATH, params={"dataBus": "setContext", "contextKey": "so_service_order", "contextValue": service_context})
        # The service context is more reliably obtained from the service link in
        # the DOA detail; fall back to the service number if the page exposes it.
        service_fields = parse_labels(service.text)
        result["service_fields"] = service_fields
        result["equipment"] = extract_equipment(service.text)
        for equipment in result["equipment"]:
            context_value = equipment.get("context_value")
            if context_value:
                product = request(client, "GET", BASE_URL + PRODUCT_PATH, params={"dataBus": "setContext", "contextKey": "so_sub_service_order", "contextValue": context_value})
                equipment["product_fields"] = parse_labels(product.text)
    return result


def extract_equipment(html_text: str) -> list[dict[str, Any]]:
    # The equipment table has a stable form id in the SCS template.
    parser = ListParser("a8a80e692922312b1019226d5b47200b5")
    parser.feed(html_text)
    contexts = re.findall(r"contextKey=so_sub_service_order&contextValue=([A-Za-z0-9]+)", html_text)
    for index, row in enumerate(parser.rows):
        if index < len(contexts):
            row["context_value"] = contexts[index]
        row["material_sn"] = row.get("mtr_sn", "")
    return parser.rows


def source_key(row: dict[str, Any]) -> str:
    doa_code = text(row.get("doa_code"))
    if doa_code:
        return doa_code
    return "full_row:" + hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def parse_datetime(value: Any) -> datetime | None:
    value = text(value)
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def extract_sales_order(details: dict[str, Any]) -> str:
    """Return the sales order exposed by the DOA detail page."""
    fields = details.get("fields") if isinstance(details, dict) else None
    if not isinstance(fields, dict):
        return ""
    for key in ("合同/订单号", "合同订单号", "销售订单", "订单号"):
        value = text(fields.get(key))
        if value:
            return value
    return ""


def load_sales_orders(database: Any, collection_name: str = "sales_orders_sap") -> set[str]:
    """Load the non-empty VBELN values used to classify SCS records."""
    values: set[str] = set()
    for document in database[collection_name].find({}, {"data.VBELN": 1}):
        data = document.get("data")
        if isinstance(data, dict):
            value = text(data.get("VBELN"))
            if value:
                values.add(value)
    return values


def classify_doa_document(document: dict[str, Any], sales_orders: set[str]) -> dict[str, Any]:
    """Build the derived DOA sales-order and 5000-company fields."""
    sales_order = extract_sales_order(document.get("details", {})) or text(document.get("sales_order"))
    return {"sales_order": sales_order, "is_5000_company": bool(sales_order and sales_order in sales_orders)}


def backfill_5000_company(database: Any, collection_name: str, sales_orders: set[str]) -> int:
    """Re-evaluate every existing DOA document after the order table changes."""
    collection = database[collection_name]
    operations: list[UpdateOne] = []
    for document in collection.find({}, {"sales_order": 1, "details.fields": 1}):
        derived = classify_doa_document(document, sales_orders)
        if document.get("sales_order") != derived["sales_order"] or document.get("is_5000_company") != derived["is_5000_company"]:
            operations.append(UpdateOne({"_id": document["_id"]}, {"$set": derived}))
    if not operations:
        return 0
    return collection.bulk_write(operations, ordered=False).modified_count


def sync(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    options = mongo_client_options()
    client: MongoClient | None = None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    collection_name = env("SCS_DOA_COLLECTION", DEFAULT_COLLECTION)
    checkpoint_name = env("SYNC_CHECKPOINT_COLLECTION", DEFAULT_CHECKPOINTS)
    try:
        with httpx.Client(base_url=BASE_URL, timeout=float(env("SCS_TIMEOUT", "120")), follow_redirects=True) as http:
            login(http)
            checkpoint = None
            if not args.dry_run:
                client = MongoClient(mongo_uri(), **options)
                checkpoint = client[env("MONGODB_DATABASE")][checkpoint_name].find_one({"_id": CHECKPOINT_ID})
                if args.full and checkpoint and checkpoint.get("status") == "running" and checkpoint.get("start_date") == args.start_date:
                    run_id = text(checkpoint.get("run_id")) or run_id
            start, end = "", ""
            if args.full:
                start = args.start_date
            elif checkpoint and checkpoint.get("watermark"):
                wm = parse_datetime(checkpoint["watermark"])
                if wm:
                    start = (wm - timedelta(days=args.lookback_days)).strftime("%Y-%m-%d")
            rows = fetch_list(http, start, end, args.page_size)
            stats: dict[str, Any] = {"success": True, "mode": "full" if args.full else "incremental", "fetched": len(rows), "upserted": 0, "detail_errors": 0, "range": {"start": start, "end": end}}
            if args.dry_run:
                return stats
            if client is None:
                client = MongoClient(mongo_uri(), **options)
            db = client[env("MONGODB_DATABASE")]
            coll = db[collection_name]
            checkpoints = db[checkpoint_name]
            resume_index = 0
            if args.full and checkpoint and checkpoint.get("status") == "running" and checkpoint.get("run_id") == run_id and checkpoint.get("start_date") == args.start_date:
                resume_index = max(0, int(checkpoint.get("row_index", 0)))
            checkpoints.update_one({"_id": CHECKPOINT_ID}, {"$set": {"dataset": "scs_doa", "status": "running", "mode": "full" if args.full else "incremental", "start_date": args.start_date if args.full else None, "run_id": run_id, "row_index": resume_index, "total_rows": len(rows), "updated_at": datetime.now(timezone.utc)}}, upsert=True)
            sales_orders = load_sales_orders(db, env("SCS_SALES_ORDER_COLLECTION", "sales_orders_sap"))
            coll.create_index("_source_key", unique=True, name="scs_doa_source_key")
            operations: list[UpdateOne] = []
            watermark: datetime | None = None
            batch_size = max(1, int(env("SCS_WRITE_BATCH_SIZE", "25")))
            progress_interval = max(1, int(env("SCS_PROGRESS_INTERVAL", str(batch_size))))
            LOGGER.info(
                "SCS DOA 开始处理：总计 %d 条，从断点 %d 继续，批量写入 %d 条",
                len(rows), resume_index, batch_size,
            )
            for row_index, row in enumerate(rows):
                if row_index < resume_index:
                    continue
                details: dict[str, Any] = {}
                try:
                    details = fetch_detail(http, row)
                except Exception as exc:  # preserve list data even if one detail is unavailable
                    stats["detail_errors"] += 1
                    details = {"error": str(exc)}
                declared = parse_datetime(row.get("declare_time"))
                if declared and (watermark is None or declared > watermark):
                    watermark = declared
                doc = {key: row.get(key, "") for key in LIST_FIELDS}
                doc.update({"_source_key": source_key(row), "_source": "scs", "details": details, "_sync_run_id": run_id, "_synced_at": datetime.now(timezone.utc)})
                doc.update(classify_doa_document(doc, sales_orders))
                operations.append(UpdateOne({"_source_key": doc["_source_key"]}, {"$set": doc, "$setOnInsert": {"_id": hashlib.sha256(doc["_source_key"].encode()).hexdigest()}}, upsert=True))
                if len(operations) >= batch_size:
                    result = coll.bulk_write(operations, ordered=False)
                    stats["upserted"] += result.upserted_count + result.modified_count
                    operations = []
                    checkpoints.update_one({"_id": CHECKPOINT_ID}, {"$set": {"status": "running", "run_id": run_id, "row_index": row_index + 1, "total_rows": len(rows), "updated_at": datetime.now(timezone.utc)}})
                    LOGGER.info(
                        "SCS DOA 处理进度：%d/%d（%.1f%%），已写入 %d 条，详情错误 %d 条",
                        row_index + 1,
                        len(rows),
                        (row_index + 1) * 100 / max(1, len(rows)),
                        stats["upserted"],
                        stats["detail_errors"],
                    )
                elif (row_index + 1) % progress_interval == 0:
                    LOGGER.info(
                        "SCS DOA 抓取进度：%d/%d（%.1f%%）",
                        row_index + 1,
                        len(rows),
                        (row_index + 1) * 100 / max(1, len(rows)),
                    )
            if operations:
                result = coll.bulk_write(operations, ordered=False)
                stats["upserted"] += result.upserted_count + result.modified_count
                LOGGER.info(
                    "SCS DOA 处理进度：%d/%d（%.1f%%），已写入 %d 条，详情错误 %d 条",
                    len(rows), len(rows), 100.0, stats["upserted"], stats["detail_errors"],
                )
            if args.full:
                stats["deleted_out_of_scope"] = coll.delete_many({"_source": "scs", "_sync_run_id": {"$ne": run_id}}).deleted_count
            stats["5000_company_backfilled"] = backfill_5000_company(db, collection_name, sales_orders)
            if watermark is None and checkpoint:
                watermark = parse_datetime(checkpoint.get("watermark"))
            checkpoints.update_one({"_id": CHECKPOINT_ID}, {"$set": {"dataset": "scs_doa", "status": "completed", "watermark": watermark.isoformat() if watermark else None, "run_id": run_id, "row_index": len(rows), "total_rows": len(rows), "updated_at": datetime.now(timezone.utc)}} , upsert=True)
            LOGGER.info(
                "SCS DOA 同步完成：共 %d 条，写入 %d 条，详情错误 %d 条",
                len(rows), stats["upserted"], stats["detail_errors"],
            )
            return stats
    finally:
        if client is not None:
            client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步 SCS DOA 申报及设备产品信息")
    parser.add_argument("--full", action="store_true", help="首次全量同步")
    parser.add_argument("--start-date", default=env("SCS_FULL_START_DATE", "2026-01-01"), help="全量开始日期 YYYY-MM-DD")
    parser.add_argument("--lookback-days", type=int, default=int(env("SCS_LOOKBACK_DAYS", "7")))
    parser.add_argument("--page-size", type=int, default=int(env("SCS_PAGE_SIZE", "500")))
    parser.add_argument("--dry-run", action="store_true", help="只登录、读取和统计，不写 MongoDB")
    return parser


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=env("SCS_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    print(json.dumps(sync(build_parser().parse_args()), ensure_ascii=False, default=str))
