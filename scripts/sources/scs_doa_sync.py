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
from urllib.parse import unquote, urlsplit, parse_qs

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
from scripts.sync_runtime import (
    Snapshot, check_leases, print_failure, resource_locks, source_hash, batches,
    verified_bulk_write, required_prodh_list, save_scope_pending, checkpoint_id,
    validate_scope_environment,
    clear_scope_pending,
)
from scripts.sales_order_reference import SalesReferences
from scripts.scs_classification import enqueue_change, service_order_values

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
DETAIL_SCHEMA_VERSION = 3
SALES_ORDER_LABELS = ("合同/订单号", "合同订单号", "销售订单", "订单号")

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
    client._sync_request_count = getattr(client, "_sync_request_count", 0) + 1
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
                row["_context_value"] = self.current_attrs.get("value", "")
                row["_detail_href"] = next((cell["href"] for cell in self.current if cell["href"]), "")
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
    for row in parser.rows:
        if "customer_name" in row:
            row["customer_uid"] = text(row.get("customer_name")) or text(row.get("customer_uid"))
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
    href = html.unescape(href or "")
    values = parse_qs(urlsplit(href).query).get("contextValue")
    if values:
        return values[0]
    match = re.search(r"[?&]contextValue=([^&'\"}\s)]+)", href)
    return unquote(match.group(1)) if match else ""


def parse_pml_context(html_text: str, pml_name: str) -> str:
    match = re.search(rf"['\"]pml['\"]\s*:\s*['\"]{re.escape(pml_name)}[^}}]*?contextValue=([^&'\"}}\s]+)", html.unescape(html_text), re.S)
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
    seen: set[str] = set()
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
        fingerprint = source_hash(batch)
        if fingerprint in seen:
            raise RuntimeError("SCS 返回重复页，拒绝推进检查点")
        seen.add(fingerprint)
        if len(batch) > page_size:
            raise RuntimeError("SCS 页大小超出请求范围")
        yield batch
        if len(batch) < page_size:
            break
        page += 1


def validate_detail_fields(fields: Any, *, previous: Any = None, order_required: bool = False) -> None:
    if not isinstance(fields, dict) or not fields:
        raise RuntimeError("SCS 详情页面无效或会话过期")
    if order_required and not any(label in fields for label in SALES_ORDER_LABELS):
        raise RuntimeError("SCS 详情缺少订单字段")
    if previous and set(previous) - fields.keys():
        raise RuntimeError("SCS 详情字段缺失，保留已有详情并等待核对")


def fetch_detail(client: httpx.Client, row: dict[str, Any]) -> dict[str, Any]:
    context = text(row.get("_context_value")) or parse_context_value(text(row.get("_detail_href")))
    result: dict[str, Any] = {"context_value": context}
    if not context:
        raise RuntimeError("DOA 缺少详情上下文")
    detail = request(client, "GET", BASE_URL + DOA_DETAIL_PATH, params={"dataBus": "setContext", "contextKey": "sp_doa", "contextValue": context})
    detail.raise_for_status()
    result["fields"] = parse_labels(detail.text)
    validate_detail_fields(result["fields"], order_required=True)
    service_uid = text(row.get("service_uid"))
    if service_uid:
        service_context = parse_pml_context(detail.text, "PM_so_service_order_control_main") or parse_pml_context(detail.text, "PM_so_service_order_browse_only_show")
        if not service_context:
            raise RuntimeError("SCS 服务单缺少详情上下文")
        service = request(client, "GET", BASE_URL + SERVICE_PATH, params={"dataBus": "setContext", "contextKey": "so_service_order", "contextValue": service_context})
        service.raise_for_status()
        # The service context is more reliably obtained from the service link in
        # the DOA detail; fall back to the service number if the page exposes it.
        service_fields = parse_labels(service.text)
        validate_detail_fields(service_fields)
        result["service_fields"] = service_fields
        result["equipment"] = extract_equipment(service.text)
        for equipment in result["equipment"]:
            context_value = equipment.get("context_value")
            if context_value:
                product = request(client, "GET", BASE_URL + PRODUCT_PATH, params={"dataBus": "setContext", "contextKey": "so_sub_service_order", "contextValue": context_value})
                product.raise_for_status()
                equipment["product_fields"] = parse_labels(product.text)
                validate_detail_fields(equipment["product_fields"], order_required=True)
    return result


def extract_equipment(html_text: str) -> list[dict[str, Any]]:
    # The equipment table has a stable form id in the SCS template.
    parser = ListParser("a8a80e692922312b1019226d5b47200b5")
    parser.feed(html_text)
    validate_list(parser)
    for row in parser.rows:
        attribute = row.get("_context_value")
        linked = parse_context_value(row.get("_detail_href", ""))
        if attribute and linked and attribute != linked:
            raise RuntimeError("SCS 设备详情上下文冲突")
        row["context_value"] = attribute or linked
        row["material_sn"] = row.get("mtr_sn", "")
        if not row["context_value"]:
            raise RuntimeError("SCS 设备缺少产品详情上下文")
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


def should_refresh_details(existing: dict[str, Any] | None, row: dict[str, Any], *, full: bool = False) -> bool:
    if full or not existing:
        return True
    details = existing.get("details") if isinstance(existing.get("details"), dict) else {}
    if existing.get("_detail_pending") or not details or details.get("error") or not details.get("fields"):
        return True
    fetched_at = parse_datetime(existing.get("_detail_fetched_at"))
    ttl = int(env("SCS_DETAIL_TTL_DAYS", "7"))
    if ttl < 1:
        raise ValueError("SCS_DETAIL_TTL_DAYS 必须大于 0")
    if existing.get("_detail_schema_version") != DETAIL_SCHEMA_VERSION or not fetched_at:
        return True
    if fetched_at <= datetime.now(timezone.utc) - timedelta(days=ttl):
        return True
    return any(text(existing.get(field)) != text(row.get(field)) for field in LIST_FIELDS)


def extract_sales_order(details: dict[str, Any]) -> str:
    """Return the sales order exposed by the DOA detail page."""
    fields = details.get("fields") if isinstance(details, dict) else None
    if not isinstance(fields, dict):
        return ""
    for key in SALES_ORDER_LABELS:
        value = text(fields.get(key))
        if value:
            return value
    return ""


def classify_doa_document(document: dict[str, Any]) -> dict[str, Any]:
    """Classify a DOA directly by its sales-order prefix."""
    sales_order = extract_sales_order(document.get("details", {})) or text(document.get("sales_order"))
    return {
        "sales_order": sales_order,
        "is_5000_company": sales_order.upper().startswith("XNC"),
    }


def backfill_5000_company(database: Any, collection_name: str) -> int:
    """Re-evaluate every existing DOA document using the sales-order prefix."""
    collection = database[collection_name]
    operations: list[UpdateOne] = []
    modified = 0
    cursor = collection.find({"_prodh_scope": "00100"}, {
        "sales_order": 1, "details": 1, "service_uid": 1, "service_order_keys": 1,
        "is_5000_company": 1, "_prodh_scope": 1,
    })
    try:
        for document in cursor:
            derived = classify_doa_document(document)
            derived["service_order_keys"] = sorted(service_order_values(document))
            if any(document.get(key) != value for key, value in derived.items()):
                enqueue_change(database, document, {**document, **derived})
                operations.append(UpdateOne({"_id": document["_id"]}, {"$set": derived}))
            if len(operations) >= 500:
                check_leases(verify=True)
                modified += verified_bulk_write(collection, operations).modified_count
                operations.clear()
        if operations:
            check_leases(verify=True)
            modified += verified_bulk_write(collection, operations).modified_count
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    return modified


def sync(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    load_dotenv(ROOT / ".env")
    validate_scope_environment()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    collection_name = env("SCS_DOA_COLLECTION", DEFAULT_COLLECTION)
    checkpoint_name = env("SYNC_CHECKPOINT_COLLECTION", DEFAULT_CHECKPOINTS)
    if args.page_size < 1 or args.lookback_days < 0:
        raise ValueError("page-size 必须大于 0，lookback-days 不能为负数")
    client = MongoClient(mongo_uri(), **mongo_client_options())
    try:
        db = client[env("MONGODB_DATABASE")]
        sales_collection = env("TARGET_COLLECTION", "sales_orders_sap")
        change_collection = env("SCS_CHANGE_COLLECTION", "scs_change_records")
        with resource_locks(db, collection_name, sales_collection, change_collection) if not args.dry_run else nullcontext(), \
                httpx.Client(base_url=BASE_URL, timeout=float(env("SCS_TIMEOUT", "120")), follow_redirects=True) as http, \
                Snapshot() as snapshot, Snapshot() as planned, Snapshot() as resolved:
            login(http)
            checkpoints = db[checkpoint_name]
            checkpoint_key = checkpoint_id(CHECKPOINT_ID, "scs")
            checkpoint = checkpoints.find_one({"_id": checkpoint_key})
            start, end = query_window(args, checkpoint)
            if start and end and start > end:
                raise ValueError("SCS 查询开始日期不能晚于结束日期")
            fetched = 0
            for batch in iter_list(http, start, end, args.page_size):
                for row in batch:
                    validate_scs_row(row, "declare_time", start, end)
                    if not text(row.get("doa_code")):
                        raise RuntimeError("DOA 缺少稳定申报单号")
                    snapshot.add(source_key(row), row)
                    fetched += 1
            stats: dict[str, Any] = {
                "success": True, "mode": "full" if args.full else "incremental",
                "fetched": fetched, "unique_rows": snapshot.count, "snapshot_complete": True,
                "upserted": 0, "detail_errors": 0, "skipped_details": 0,
                "range": {"start": start, "end": end}, "run_id": run_id,
                "prodh_list": required_prodh_list(), "scope_rule_version": 1,
                "in_scope": 0, "out_of_scope": 0, "scope_pending": 0, "unchanged": 0,
                "lookup_batches": 0, "coverage_verified": False,
                "cleanup": "disabled_unverified_coverage", "deleted_out_of_scope": 0,
            }
            coll = db[collection_name]
            refs = SalesReferences.load(db[sales_collection])
            for document in db["sync_scope_pending"].find({"dataset": CHECKPOINT_ID}):
                if not snapshot.contains(document["key"]):
                    snapshot.add(document["key"], document["payload"])
            ttl = int(env("SCS_DETAIL_TTL_DAYS", "7"))
            retry_limit = int(env("SCS_DETAIL_RETRY_LIMIT", "100"))
            if ttl < 1 or retry_limit < 1:
                raise ValueError("详情 TTL 和重试数量必须大于 0")
            historical = coll.find({"$or": [
                {"_detail_pending": True}, {"_detail_schema_version": {"$ne": DETAIL_SCHEMA_VERSION}},
                {"_detail_fetched_at": {"$lt": datetime.now(timezone.utc) - timedelta(days=ttl)}},
            ]}).sort("_detail_attempted_at", 1).limit(retry_limit)
            try:
                for document in historical:
                    key = text(document.get("_source_key"))
                    if key and not snapshot.contains(key):
                        snapshot.add(key, document)
            finally:
                historical.close()
            watermark = parse_datetime((checkpoint or {}).get("watermark"))
            batch_size = max(1, int(env("SCS_WRITE_BATCH_SIZE", "25")))
            for batch in batches(snapshot, batch_size):
                existing_by_key = {doc["_source_key"]: doc for doc in coll.find({
                    "_source_key": {"$in": [key for key, _ in batch]},
                })}
                stats["lookup_batches"] += 1
                for key, row in batch:
                    existing = existing_by_key.get(key)
                    detail_fields = refreshed_details(http, row, existing or (row if row.get("details") else None),
                                                      stats, full=args.full)
                    declared = parse_datetime(row.get("declare_time"))
                    if declared and (watermark is None or declared > watermark):
                        watermark = declared
                    if detail_fields["_detail_pending"]:
                        stats["scope_pending"] += 1
                        if not args.dry_run:
                            save_scope_pending(db, CHECKPOINT_ID, key, {**row, **detail_fields})
                        if existing and existing.get("_prodh_scope") == "00100":
                            planned.add(key, {"fields": detail_fields, "upsert": False})
                        continue
                    doc = {field: row.get(field, "") for field in LIST_FIELDS}
                    if not text(doc.get("customer_uid")):
                        doc["customer_uid"] = text((existing or {}).get("customer_uid")) or text((detail_fields.get("details") or {}).get("fields", {}).get("客户全称"))
                    doc.update(detail_fields)
                    doc.update(classify_doa_document({**(existing or {}), **doc}))
                    scope = refs.resolve(sales=doc["sales_order"])
                    stats["scope_pending" if scope == "pending" else scope] += 1
                    if scope != "in_scope":
                        # Keep the entire row and successful details for independent retries.
                        if scope == "out_of_scope":
                            resolved.add(key, {})
                        elif not args.dry_run:
                            save_scope_pending(db, CHECKPOINT_ID, key, {**row, **doc})
                        continue
                    resolved.add(key, {})
                    doc.update({"_source_key": key, "_source": "scs",
                                "_context_value": row.get("_context_value", ""),
                                "_detail_href": row.get("_detail_href", ""),
                                "_prodh_scope": "00100", "_scope_rule_version": 1})
                    doc["service_order_keys"] = sorted(service_order_values(doc))
                    digest = source_hash({**{field: doc.get(field) for field in LIST_FIELDS},
                                          "details": doc["details"],
                                          "schema": DETAIL_SCHEMA_VERSION})
                    doc["_source_hash"] = digest
                    unchanged = (not args.full and existing and existing.get("_source_hash") == digest
                                 and "_detail_attempted_at" not in detail_fields
                                 and all(existing.get(field) == doc[field]
                                         for field in ("sales_order", "is_5000_company", "service_order_keys")))
                    stats["unchanged"] += bool(unchanged)
                    if unchanged:
                        continue
                    doc.update(_sync_run_id=run_id, _synced_at=datetime.now(timezone.utc))
                    if not args.dry_run:
                        enqueue_change(db, existing or {}, doc)
                    planned.add(key, {"fields": doc, "upsert": True})
            stats["success"] = not (args.full and stats["detail_errors"])
            stats.update(http_requests=getattr(http, "_sync_request_count", 0),
                         elapsed_seconds=round(time.monotonic() - started, 3),
                         scope_backlog=db["sync_scope_pending"].count_documents({"dataset": CHECKPOINT_ID}))
            if args.dry_run:
                return stats
            coll.create_index("_source_key", unique=True, name="scs_doa_source_key")
            coll.create_index("service_order_keys", name="scs_doa_service_orders")
            for batch in batches(planned, batch_size):
                operations = []
                for key, plan in batch:
                    update = {"$set": plan["fields"]}
                    if plan["upsert"]:
                        update["$setOnInsert"] = {"_id": hashlib.sha256(key.encode()).hexdigest()}
                    operations.append(UpdateOne({"_source_key": key}, update, upsert=plan["upsert"]))
                result = verified_bulk_write(coll, operations)
                stats["upserted"] += result.upserted_count + result.modified_count
            clear_scope_pending(db, CHECKPOINT_ID, (key for key, _ in resolved))
            stats["5000_company_backfilled"] = backfill_5000_company(db, collection_name)
            check_leases(verify=True)
            if stats["success"]:
                checkpoints.update_one({"_id": checkpoint_key}, {"$set": {
                    "status": "completed", "watermark": watermark.isoformat() if watermark else None,
                    "query_start": start, "query_end": end,
                    "run_id": run_id, "total_rows": fetched, "updated_at": datetime.now(timezone.utc),
                }}, upsert=True)
            stats.update(elapsed_seconds=round(time.monotonic() - started, 3),
                         scope_backlog=db["sync_scope_pending"].count_documents({"dataset": CHECKPOINT_ID}),
                         detail_pending=coll.count_documents({"_detail_pending": True}),
                         classification_pending=db["sync_classification_pending"].count_documents({}))
            return stats
    finally:
        client.close()


def validate_scs_row(row: dict[str, Any], field: str, start: str, end: str) -> None:
    parsed = parse_datetime(row.get(field))
    if parsed is None:
        raise RuntimeError(f"SCS 源记录缺少有效日期 {field}")
    day = parsed.date().isoformat()
    if (start and day < start) or (end and day > end):
        raise RuntimeError(f"SCS 源记录 {field} 超出查询窗口")


def refreshed_details(http: Any, row: dict[str, Any], existing: dict[str, Any] | None,
                      stats: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    details = (existing or {}).get("details") or {}
    pending, error = False, ""
    updates: dict[str, Any] = {}
    if should_refresh_details(existing, row, full=full):
        updates["_detail_attempted_at"] = datetime.now(timezone.utc)
        try:
            candidate = fetch_detail(http, row)
            for name in ("fields", "service_fields"):
                if details.get(name):
                    validate_detail_fields(candidate.get(name), previous=details[name])
            old_equipment = {item.get("context_value"): item for item in details.get("equipment", [])}
            for item in candidate.get("equipment", []):
                previous = old_equipment.get(item.get("context_value"), {}).get("product_fields")
                if previous:
                    validate_detail_fields(item.get("product_fields"), previous=previous)
            details = candidate
            updates.update(_detail_fetched_at=datetime.now(timezone.utc),
                           _detail_schema_version=DETAIL_SCHEMA_VERSION)
        except Exception as exc:
            stats["detail_errors"] += 1
            pending, error = True, str(exc)
    else:
        stats["skipped_details"] += 1
    return {"details": details, "_detail_pending": pending, "_detail_error": error, **updates}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步 SCS DOA 申报及设备产品信息")
    parser.add_argument("--full", action="store_true", help="首次全量同步")
    parser.add_argument("--start-date", default=env("SCS_FULL_START_DATE", "2026-01-01"), help="全量开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", default=datetime.now().date().isoformat())
    parser.add_argument("--lookback-days", type=int, default=int(env("SCS_LOOKBACK_DAYS", "2")))
    parser.add_argument("--page-size", type=int, default=int(env("SCS_PAGE_SIZE", "500")))
    parser.add_argument("--dry-run", action="store_true", help="只登录、读取和统计，不写 MongoDB")
    parser.add_argument("--allow-large-cleanup", action="store_true", help="兼容旧命令，不解除来源覆盖校验")
    return parser


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    install_graceful_shutdown()
    logging.basicConfig(level=env("SCS_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        print(json.dumps(sync(build_parser().parse_args()), ensure_ascii=False, default=str))
    except Exception as exc:
        raise SystemExit(print_failure(exc))
