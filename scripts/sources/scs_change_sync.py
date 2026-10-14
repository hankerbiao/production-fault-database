#!/usr/bin/env python3
"""Synchronize SCS ``数据查询 -> 换上换下`` records.

The endpoint is server-rendered HTML and uses the same SCS session as the DOA
source.  Records are upserted by a deterministic hash because the result table
does not expose a reliable row id.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pymongo import MongoClient, UpdateOne

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sources.hana.hana_view_sync import install_graceful_shutdown, load_dotenv, mongo_client_options, mongo_uri
from scripts.sources.scs_doa_sync import (
    BASE_URL,
    ListParser,
    env,
    parse_datetime,
    query_window,
    login as session_login,
    scs_scope,
    throttled_request,
    validate_list,
    validate_scs_row,
)
from scripts.sync_runtime import Snapshot, archive_cleanup, check_leases, print_failure, resource_lock, validate_snapshot_cleanup

ROOT = PROJECT_ROOT
CHANGE_PATH = "/PM_sp_change_Result_01.pml"
CHANGE_FORM_ID = "a8a80e692948867c501948be54ba50b6f"
CHECKPOINT_ID = "scs_changes"
DEFAULT_COLLECTION = "scs_change_records"
DEFAULT_CHECKPOINTS = "sync_checkpoints"
DATA_YEAR_START = "2026-01-01"
DATA_YEAR_END = "2026-12-31"
LOGGER = logging.getLogger("scs_change_sync")

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "so_code": ("so_code", "服务单号"),
    "customer_name": ("customer_name", "客户名称"),
    "device_sn": ("prd_sn", "报修设备", "设备SN"),
    "change_type": ("change_type", "操作类型"),
    "part_number": ("mtr_code", "PN"),
    "part_sn": ("mtr_sn", "SN"),
    "part_name": ("mtr_name", "备件名称"),
    "original_code": ("original_code", "原厂原码"),
    "remark": ("remark", "备注"),
    "need_return": ("need_return", "是否需要归还"),
    "operator": ("uname", "操作人"),
    "create_time": ("create_time", "操作时间"),
    "is_revoked": ("is_revoked", "是否撤销"),
    "revoked_by": ("revoked_by", "撤销人"),
    "revoked_time": ("revoked_time", "撤销时间"),
}


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Throttle every SCS change request with a configurable 5-10s delay."""
    return throttled_request(client, method, url, "SCS_CHANGE_REQUEST_DELAY", (5, 10), **kwargs)


def login(client: httpx.Client) -> None:
    """Log in using the same session while honoring the change-sync delay."""
    session_login(client, send=request)


def bounded_change_range(start: str = "", end: str = "") -> tuple[str, str]:
    """Clamp all change queries to the 2026 calendar year."""
    def parse(value: str, fallback: date) -> date:
        value = str(value or "").strip()
        if len(value) == 8 and value.isdigit():
            value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
        return date.fromisoformat(value[:10]) if value else fallback

    lower, upper = date.fromisoformat(DATA_YEAR_START), date.fromisoformat(DATA_YEAR_END)
    bounded_start = max(parse(start, lower), lower)
    bounded_end = min(parse(end, upper), upper)
    return bounded_start.isoformat(), bounded_end.isoformat()


def parse_changes(html_text: str) -> list[dict[str, Any]]:
    parser = ListParser(CHANGE_FORM_ID)
    parser.feed(html_text)
    validate_list(parser)
    if not set(FIELD_ALIASES["so_code"]).intersection(parser.headers):
        raise RuntimeError("SCS 换上换下结果表缺少服务单号列")
    rows: list[dict[str, Any]] = []
    for raw in parser.rows:
        row: dict[str, Any] = {}
        for canonical, aliases in FIELD_ALIASES.items():
            row[canonical] = next((str(raw.get(alias) or "").strip() for alias in aliases if str(raw.get(alias) or "").strip()), "")
        row["_context_value"] = raw.get("_context_value", "")
        row["_detail_href"] = raw.get("_detail_href", "")
        row["raw"] = raw
        rows.append(row)
    return rows


def change_source_key(row: dict[str, Any]) -> str:
    values = [
        row.get("so_code", ""), row.get("device_sn", ""), row.get("change_type", ""),
        row.get("part_number", ""), row.get("part_sn", ""), row.get("create_time", ""),
        row.get("operator", ""),
    ]
    payload = "\x1f".join(str(value or "").strip() for value in values)
    if not payload.strip("\x1f"):
        payload = json.dumps(row.get("raw", row), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def service_order_values(document: dict[str, Any]) -> set[str]:
    """Extract every service-order identifier available on a DOA document."""
    values: set[str] = set()
    service = str(document.get("service_uid") or "").strip()
    if service:
        values.add(service)
    details = document.get("details")
    fields = details.get("service_fields") if isinstance(details, dict) else None
    if isinstance(fields, dict):
        for key in ("服务单号", "服务订单号", "服务单"):
            value = str(fields.get(key) or "").strip()
            if value:
                values.add(value)
    return values


def load_5000_service_orders(database: Any, collection_name: str = "scs_doa_records") -> set[str]:
    """Return service-order numbers belonging to 5000 companies."""
    values: set[str] = set()
    for document in database[collection_name].find({"is_5000_company": True}, {"service_uid": 1, "details.service_fields": 1}):
        values.update(service_order_values(document))
    return values


def fetch_change_page(
    client: Any,
    page: int,
    start: str = "",
    end: str = "",
    page_size: int = 500,
) -> list[dict[str, Any]]:
    """Fetch and parse one SCS result page.

    Keeping page retrieval separate lets the sync loop persist progress before
    requesting the next page. A page is bounded by ``page_size`` and is the
    only source data kept in memory at once.
    """
    start, end = bounded_change_range(start, end)
    params = {
        "1": "1", "clientstamp": str(int(datetime.now().timestamp() * 1000)),
        "so_code": "", "prd_sn": "", "customer_name": "", "mtr_sn": "",
        "change_type": "", "mtr_code": "", "mtr_name": "", "original_code": "",
        "uname": "", "create_time": start, "create_time2": end,
        "pageNo": page, "pageSize": page_size, "target": "PM_sp_change_Result_01",
    }
    response = request(client, "GET", BASE_URL + CHANGE_PATH, params=params)
    response.raise_for_status()
    return parse_changes(response.text)


def iter_changes(
    client: Any,
    start: str = "",
    end: str = "",
    page_size: int = 500,
):
    """Yield validated result pages without accumulating a source-sized list."""
    page = 1
    while True:
        batch = fetch_change_page(client, page, start, end, page_size)
        if not batch:
            break
        yield batch
        if len(batch) < page_size:
            break
        page += 1


def sync(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    collection_name = env("SCS_CHANGE_COLLECTION", DEFAULT_COLLECTION)
    checkpoint_name = env("SYNC_CHECKPOINT_COLLECTION", DEFAULT_CHECKPOINTS)
    allow_cleanup = bool(getattr(args, "allow_large_cleanup", False))
    mode = "full" if args.full else "incremental"
    if args.page_size < 1 or args.lookback_days < 0:
        raise ValueError("page-size 必须大于 0，lookback-days 不能为负数")
    client = MongoClient(mongo_uri(), **mongo_client_options())
    try:
        db = client[env("MONGODB_DATABASE")]
        with resource_lock(db, collection_name) if not args.dry_run else nullcontext(), \
                httpx.Client(base_url=BASE_URL, timeout=float(env("SCS_TIMEOUT", "120")), follow_redirects=True) as http, \
                Snapshot() as snapshot:
            login(http)
            collection = db[collection_name]
            checkpoints = db[checkpoint_name]
            checkpoint = checkpoints.find_one({"_id": CHECKPOINT_ID}) or {}
            start, end = query_window(args, checkpoint)
            start, end = bounded_change_range(start, end)
            if start > end:
                raise ValueError("SCS 查询窗口与 2026 年数据范围没有交集")
            fetched = 0
            for page, batch in enumerate(iter_changes(http, start, end, args.page_size), 1):
                for row in batch:
                    validate_scs_row(row, "create_time", start, end)
                    snapshot.add(change_source_key(row), row)
                    fetched += 1
                LOGGER.info("SCS 换上换下读取：第 %d 页，共 %d 条", page, fetched)
            stats = {"success": True, "mode": mode, "fetched": fetched, "unique_rows": snapshot.count,
                     "upserted": 0, "snapshot_complete": True, "range": {"start": start, "end": end}}
            scope = scs_scope("create_time", start, end)
            if args.full:
                validate_snapshot_cleanup(collection, scope, snapshot, allow=allow_cleanup)
            if args.dry_run:
                return stats
            check_leases(verify=True)
            collection.create_index("_source_key", unique=True, name="scs_change_source_key")
            company_orders = load_5000_service_orders(db, env("SCS_DOA_COLLECTION", "scs_doa_records"))
            watermark = parse_datetime(checkpoint.get("watermark"))
            batch_size = max(1, int(env("SCS_WRITE_BATCH_SIZE", "100")))
            def save_checkpoint(status: str) -> None:
                check_leases(verify=True)
                checkpoints.update_one(
                    {"_id": CHECKPOINT_ID},
                    {"$set": {
                        "dataset": "scs_changes", "status": status, "mode": mode,
                        "start_date": args.start_date if args.full else None,
                        "query_start": start, "query_end": end, "run_id": run_id,
                        "total_rows": fetched,
                        "watermark": (watermark.isoformat() if watermark else None) if status == "completed" else checkpoint.get("watermark"),
                        "updated_at": datetime.now(timezone.utc),
                    }},
                    upsert=True,
                )

            save_checkpoint("running")
            operations: list[UpdateOne] = []
            def flush() -> None:
                if operations:
                    check_leases(verify=True)
                    result = collection.bulk_write(operations, ordered=False)
                    stats["upserted"] += result.upserted_count + result.modified_count
                    operations.clear()
            for key, row in snapshot:
                created = parse_datetime(row.get("create_time"))
                if created and (watermark is None or created > watermark):
                    watermark = created
                document = {field: row.get(field, "") for field in FIELD_ALIASES}
                document.update({
                    "_source_key": key, "_source": "scs", "raw": row.get("raw", {}),
                    "is_5000_company": bool(row.get("so_code") and row["so_code"] in company_orders),
                    "_sync_run_id": run_id, "_synced_at": datetime.now(timezone.utc),
                })
                operations.append(UpdateOne({"_source_key": key}, {"$set": document,
                                            "$setOnInsert": {"_id": key}}, upsert=True))
                if len(operations) >= batch_size:
                    flush()
            flush()
            if args.full:
                stats["deleted_out_of_scope"] = archive_cleanup(db, collection_name, scope,
                    {**scope, "_sync_run_id": {"$ne": run_id}}, run_id, snapshot.count,
                    allow_large_cleanup=allow_cleanup)
            stats["5000_company_backfilled"] = backfill_company(collection, company_orders)
            save_checkpoint("completed")
            LOGGER.info(
                "SCS 换上换下同步完成：进度 100%%，共 %d 条，写入 %d 条",
                stats["fetched"], stats["upserted"],
            )
            return stats
    finally:
        client.close()


def backfill_company(collection: Any, company_orders: set[str]) -> int:
    operations, modified = [], 0
    cursor = collection.find({}, {"so_code": 1, "is_5000_company": 1})
    try:
        for row in cursor:
            value = bool(row.get("so_code") and row["so_code"] in company_orders)
            if row.get("is_5000_company") != value:
                operations.append(UpdateOne({"_id": row["_id"]}, {"$set": {"is_5000_company": value}}))
            if len(operations) >= 500:
                check_leases(verify=True)
                modified += collection.bulk_write(operations, ordered=False).modified_count
                operations.clear()
        if operations:
            check_leases(verify=True)
            modified += collection.bulk_write(operations, ordered=False).modified_count
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    return modified


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步 SCS 换上换下数据查询记录")
    parser.add_argument("--full", action="store_true", help="首次全量同步")
    parser.add_argument("--start-date", default=env("SCS_FULL_START_DATE", "2026-01-01"), help="全量开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", default=date.today().isoformat())
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
