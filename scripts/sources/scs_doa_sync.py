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
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from pymongo import MongoClient, UpdateOne

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sources.hana.hana_view_sync import (
    env,
    load_dotenv,
    mongo_client_options,
    mongo_uri,
    install_graceful_shutdown,
)
from scripts.sync_runtime import Snapshot, archive_cleanup, check_leases, print_failure, resource_lock, validate_snapshot_cleanup

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


def text(value: Any) -> str:
    return str(value or "").strip()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Throttle every SCS request to avoid issuing a burst of traffic."""
    return throttled_request(client, method, url, "SCS_REQUEST_DELAY", (3, 5), **kwargs)


def throttled_request(client: Any, method: str, url: str, prefix: str, defaults: tuple[int, int], **kwargs: Any) -> Any:
    minimum = float(env(prefix + "_MIN", str(defaults[0])))
    maximum = float(env(prefix + "_MAX", str(defaults[1])))
    if minimum < 0 or maximum < minimum:
        raise ValueError("SCS_REQUEST_DELAY_MIN/MAX 配置无效")
    time.sleep(random.uniform(minimum, maximum))
    check_leases()
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
        self.found_table = False
        self.invalid_rows = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {k: v or "" for k, v in attrs}
        if tag == "form" and attrs_map.get("id") == self.form_id:
            self.form_depth = 1
        elif self.form_depth and tag == "form":
            self.form_depth += 1
        if self.form_depth and tag == "table":
            self.found_table = True
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
            else:
                self.invalid_rows += 1
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
    validate_list(parser)
    if "doa_code" not in parser.headers:
        raise RuntimeError("SCS DOA 结果表缺少 doa_code 列")
    return parser.rows


def validate_list(parser: ListParser) -> None:
    if not parser.found_table or not parser.headers or parser.invalid_rows:
        raise RuntimeError("SCS 结果表结构无效或会话过期，拒绝将页面视为空结果")


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


def login(client: httpx.Client, send: Any = None) -> None:
    username, password = env("SCS_USERNAME"), env("SCS_PASSWORD")
    if not username or not password:
        raise RuntimeError("缺少 SCS_USERNAME 或 SCS_PASSWORD")
    response = (send or request)(client, "POST", BASE_URL + LOGIN_PATH, data={
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


def iter_list(client: httpx.Client, start: str = "", end: str = "", page_size: int = 500):
    if page_size < 1:
        raise ValueError("page-size 必须大于 0")
    page = 1
    while True:
        params = {
            "1": "1", "clientstamp": str(int(datetime.now().timestamp() * 1000)),
            "doa_code": "", "service_uid": "", "customer_uid": "", "sugon_sn_multi": "",
            "contract": "", "declare_time": start, "declare_time2": end,
            "detail_problem": "", "problem_conclusion": "", "pageNo": page,
            "pageSize": page_size, "target": "PM_sp_doa_Result",
        }
        response = request(client, "GET", BASE_URL + LIST_PATH, params=params)
        response.raise_for_status()
        batch = parse_list(response.text)
        LOGGER.info("SCS DOA 列表进度：第 %d 页，%d 条", page, len(batch))
        if not batch:
            break
        yield batch
        if len(batch) < page_size:
            break
        page += 1


def fetch_detail(client: httpx.Client, row: dict[str, Any]) -> dict[str, Any]:
    context = text(row.get("_context_value")) or parse_context_value(text(row.get("_detail_href")))
    result: dict[str, Any] = {"context_value": context}
    if not context:
        raise RuntimeError("DOA 缺少详情上下文")
    detail = request(client, "GET", BASE_URL + DOA_DETAIL_PATH, params={"dataBus": "setContext", "contextKey": "sp_doa", "contextValue": context})
    detail.raise_for_status()
    result["fields"] = parse_labels(detail.text)
    if not result["fields"]:
        raise RuntimeError("SCS 详情页面无效或会话过期")
    service_uid = text(row.get("service_uid"))
    if service_uid:
        service_context = parse_pml_context(detail.text, "PM_so_service_order_control_main") or parse_pml_context(detail.text, "PM_so_service_order_browse_only_show")
        service = request(client, "GET", BASE_URL + SERVICE_PATH, params={"dataBus": "setContext", "contextKey": "so_service_order", "contextValue": service_context})
        service.raise_for_status()
        # The service context is more reliably obtained from the service link in
        # the DOA detail; fall back to the service number if the page exposes it.
        service_fields = parse_labels(service.text)
        if not service_fields:
            raise RuntimeError("SCS 服务单页面无效或会话过期")
        result["service_fields"] = service_fields
        result["equipment"] = extract_equipment(service.text)
        for equipment in result["equipment"]:
            context_value = equipment.get("context_value")
            if context_value:
                product = request(client, "GET", BASE_URL + PRODUCT_PATH, params={"dataBus": "setContext", "contextKey": "so_sub_service_order", "contextValue": context_value})
                product.raise_for_status()
                equipment["product_fields"] = parse_labels(product.text)
                if not equipment["product_fields"]:
                    raise RuntimeError("SCS 产品页面无效或会话过期")
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
    iso = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def query_window(args: argparse.Namespace, checkpoint: dict[str, Any] | None) -> tuple[str, str]:
    end = text(getattr(args, "end_date", ""))
    if end:
        end = datetime.strptime(end, "%Y-%m-%d").date().isoformat()
    if args.full:
        return datetime.strptime(text(args.start_date), "%Y-%m-%d").date().isoformat(), end
    watermark = parse_datetime((checkpoint or {}).get("watermark"))
    basis = watermark or datetime.now(timezone.utc)
    lookback_days = max(0, int(getattr(args, "lookback_days", 0) or 0))
    return (basis - timedelta(days=lookback_days)).date().isoformat(), end


def should_refresh_details(existing: dict[str, Any] | None, row: dict[str, Any]) -> bool:
    if not existing:
        return True
    details = existing.get("details") if isinstance(existing.get("details"), dict) else {}
    if existing.get("_detail_pending") or not details or details.get("error") or not details.get("fields"):
        return True
    return any(text(existing.get(field)) != text(row.get(field)) for field in LIST_FIELDS)


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


def load_5000_customer_purchase_orders(
    database: Any,
    collection_name: str = "sales_order_delivery_records_sap",
) -> set[str]:
    """Load BSTNK values for delivery rows classified as 5000-company orders."""
    values: set[str] = set()
    query = {"VBELN": {"$regex": "^XNC", "$options": "i"}}
    for document in database[collection_name].find(query, {"BSTNK": 1}):
        value = text(document.get("BSTNK"))
        if value:
            values.add(value)
    return values


def classify_doa_document(document: dict[str, Any], customer_purchase_orders: set[str]) -> dict[str, Any]:
    """Classify a DOA by matching its order number to a 5000-company BSTNK."""
    sales_order = extract_sales_order(document.get("details", {})) or text(document.get("sales_order"))
    return {
        "sales_order": sales_order,
        "is_5000_company": bool(sales_order and sales_order in customer_purchase_orders),
    }


def backfill_5000_company(database: Any, collection_name: str, customer_purchase_orders: set[str]) -> int:
    """Re-evaluate every existing DOA document against delivery-order rows."""
    collection = database[collection_name]
    operations: list[UpdateOne] = []
    modified = 0
    for document in collection.find({}, {"sales_order": 1, "details.fields": 1, "is_5000_company": 1}):
        derived = classify_doa_document(document, customer_purchase_orders)
        if document.get("sales_order") != derived["sales_order"] or document.get("is_5000_company") != derived["is_5000_company"]:
            operations.append(UpdateOne({"_id": document["_id"]}, {"$set": derived}))
        if len(operations) >= 500:
            check_leases(verify=True)
            modified += collection.bulk_write(operations, ordered=False).modified_count
            operations.clear()
    if operations:
        check_leases(verify=True)
        modified += collection.bulk_write(operations, ordered=False).modified_count
    return modified


def sync(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    collection_name = env("SCS_DOA_COLLECTION", DEFAULT_COLLECTION)
    checkpoint_name = env("SYNC_CHECKPOINT_COLLECTION", DEFAULT_CHECKPOINTS)
    allow_cleanup = bool(getattr(args, "allow_large_cleanup", False))
    if args.page_size < 1 or args.lookback_days < 0:
        raise ValueError("page-size 必须大于 0，lookback-days 不能为负数")
    client = MongoClient(mongo_uri(), **mongo_client_options())
    try:
        db = client[env("MONGODB_DATABASE")]
        with resource_lock(db, collection_name) if not args.dry_run else nullcontext(), \
                httpx.Client(base_url=BASE_URL, timeout=float(env("SCS_TIMEOUT", "120")), follow_redirects=True) as http, \
                Snapshot() as snapshot:
            login(http)
            checkpoints = db[checkpoint_name]
            checkpoint = checkpoints.find_one({"_id": CHECKPOINT_ID})
            start, end = query_window(args, checkpoint)
            if start and end and start > end:
                raise ValueError("SCS 查询开始日期不能晚于结束日期")
            fetched = 0
            for batch in iter_list(http, start, end, args.page_size):
                for row in batch:
                    validate_scs_row(row, "declare_time", start, end)
                    snapshot.add(source_key(row), row)
                    fetched += 1
            stats: dict[str, Any] = {
                "success": True, "mode": "full" if args.full else "incremental",
                "fetched": fetched, "unique_rows": snapshot.count, "snapshot_complete": True,
                "upserted": 0, "detail_errors": 0, "skipped_details": 0,
                "range": {"start": start, "end": end}, "run_id": run_id,
            }
            coll = db[collection_name]
            scope = scs_scope("declare_time", start, end)
            if args.full:
                validate_snapshot_cleanup(coll, scope, snapshot, allow=allow_cleanup)
            if args.dry_run:
                return stats
            check_leases(verify=True)
            checkpoints.update_one({"_id": CHECKPOINT_ID}, {"$set": {
                "dataset": CHECKPOINT_ID, "status": "running", "mode": stats["mode"],
                "query_start": start, "query_end": end, "run_id": run_id,
                "updated_at": datetime.now(timezone.utc),
            }}, upsert=True)
            customer_purchase_orders = load_5000_customer_purchase_orders(db)
            coll.create_index("_source_key", unique=True, name="scs_doa_source_key")
            operations: list[UpdateOne] = []
            watermark = parse_datetime((checkpoint or {}).get("watermark"))
            batch_size = max(1, int(env("SCS_WRITE_BATCH_SIZE", "25")))

            def flush() -> None:
                if operations:
                    check_leases(verify=True)
                    result = coll.bulk_write(operations, ordered=False)
                    stats["upserted"] += result.upserted_count + result.modified_count
                    operations.clear()

            for key, row in snapshot:
                existing = coll.find_one({"_source_key": key})
                detail_fields = refreshed_details(http, row, existing, stats)
                declared = parse_datetime(row.get("declare_time"))
                if declared and (watermark is None or declared > watermark):
                    watermark = declared
                doc = {field: row.get(field, "") for field in LIST_FIELDS}
                doc.update(detail_fields)
                doc.update({"_source_key": key, "_source": "scs", "_context_value": row.get("_context_value", ""),
                    "_detail_href": row.get("_detail_href", ""), "_sync_run_id": run_id,
                    "_synced_at": datetime.now(timezone.utc)})
                doc.update(classify_doa_document(doc, customer_purchase_orders))
                operations.append(UpdateOne({"_source_key": key}, {"$set": doc,
                    "$setOnInsert": {"_id": hashlib.sha256(key.encode()).hexdigest()}}, upsert=True))
                if len(operations) >= batch_size:
                    flush()
                    LOGGER.info("SCS DOA 已写入 %d 条，待补详情 %d 条", stats["upserted"], stats["detail_errors"])
            flush()
            if args.full:
                stats["deleted_out_of_scope"] = archive_cleanup(db, collection_name, scope,
                    {**scope, "_sync_run_id": {"$ne": run_id}}, run_id, snapshot.count,
                    allow_large_cleanup=allow_cleanup)
            # Retry historical failures even after the list watermark has passed them.
            pending = coll.find({"$or": [{"_detail_pending": True}, {"details.error": {"$exists": True}}]}).sort(
                "_detail_attempted_at", 1).limit(max(1, int(env("SCS_DETAIL_RETRY_LIMIT", "100"))))
            try:
                for document in pending:
                    if snapshot.contains(text(document.get("_source_key"))):
                        continue
                    row = {**document, "_context_value": document.get("_context_value") or
                           (document.get("details") or {}).get("context_value", "")}
                    updates = refreshed_details(http, row, document, stats)
                    updates.update(classify_doa_document({**document, **updates}, customer_purchase_orders))
                    check_leases(verify=True)
                    coll.update_one({"_id": document["_id"]}, {"$set": updates})
            finally:
                pending.close()
            stats["5000_company_backfilled"] = backfill_5000_company(db, collection_name, customer_purchase_orders)
            check_leases(verify=True)
            checkpoints.update_one({"_id": CHECKPOINT_ID}, {"$set": {
                "status": "completed", "watermark": watermark.isoformat() if watermark else None,
                "run_id": run_id, "total_rows": fetched, "updated_at": datetime.now(timezone.utc),
            }}, upsert=True)
            return stats
    finally:
        client.close()


def scs_scope(field: str, start: str, end: str) -> dict[str, Any]:
    scope: dict[str, Any] = {"_source": "scs"}
    bounds = {}
    if start:
        bounds["$gte"] = start
    if end:
        bounds["$lt"] = (datetime.fromisoformat(end) + timedelta(days=1)).date().isoformat()
    if bounds:
        scope[field] = bounds
    return scope


def validate_scs_row(row: dict[str, Any], field: str, start: str, end: str) -> None:
    parsed = parse_datetime(row.get(field))
    if parsed is None:
        raise RuntimeError(f"SCS 源记录缺少有效日期 {field}")
    day = parsed.date().isoformat()
    if (start and day < start) or (end and day > end):
        raise RuntimeError(f"SCS 源记录 {field} 超出查询窗口")


def refreshed_details(http: Any, row: dict[str, Any], existing: dict[str, Any] | None,
                      stats: dict[str, Any]) -> dict[str, Any]:
    details = (existing or {}).get("details") or {}
    pending, error = False, ""
    if should_refresh_details(existing, row):
        try:
            details = fetch_detail(http, row)
        except Exception as exc:
            stats["detail_errors"] += 1
            pending, error = True, str(exc)
    else:
        stats["skipped_details"] += 1
    return {"details": details, "_detail_pending": pending, "_detail_error": error,
            "_detail_attempted_at": datetime.now(timezone.utc)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步 SCS DOA 申报及设备产品信息")
    parser.add_argument("--full", action="store_true", help="首次全量同步")
    parser.add_argument("--start-date", default=env("SCS_FULL_START_DATE", "2026-01-01"), help="全量开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", default=datetime.now().date().isoformat())
    parser.add_argument("--lookback-days", type=int, default=int(env("SCS_LOOKBACK_DAYS", "2")))
    parser.add_argument("--page-size", type=int, default=int(env("SCS_PAGE_SIZE", "500")))
    parser.add_argument("--dry-run", action="store_true", help="只登录、读取和统计，不写 MongoDB")
    parser.add_argument("--allow-large-cleanup", action="store_true", help="确认源收缩后允许空快照或大量旧记录清理")
    return parser


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    install_graceful_shutdown()
    logging.basicConfig(level=env("SCS_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        print(json.dumps(sync(build_parser().parse_args()), ensure_ascii=False, default=str))
    except Exception as exc:
        raise SystemExit(print_failure(exc))
