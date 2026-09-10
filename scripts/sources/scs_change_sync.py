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
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from pymongo import MongoClient, UpdateOne

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sources.hana.hana_view_sync import load_dotenv, mongo_client_options, mongo_uri
from scripts.sources.scs_doa_sync import (
    BASE_URL,
    LOGIN_PATH,
    ListParser,
    env,
    parse_datetime,
)

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
    minimum = float(env("SCS_CHANGE_REQUEST_DELAY_MIN", "5"))
    maximum = float(env("SCS_CHANGE_REQUEST_DELAY_MAX", "10"))
    if minimum < 0 or maximum < minimum:
        raise ValueError("SCS_CHANGE_REQUEST_DELAY_MIN/MAX 配置无效")
    time.sleep(random.uniform(minimum, maximum))
    return client.request(method, url, **kwargs)


def login(client: httpx.Client) -> None:
    """Log in using the same session while honoring the change-sync delay."""
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


def bounded_change_range(start: str = "", end: str = "") -> tuple[str, str]:
    """Clamp all change queries to the 2026 calendar year."""
    def parse(value: str, fallback: date) -> date:
        value = str(value or "").strip()
        if len(value) == 8 and value.isdigit():
            value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return fallback

    lower, upper = date.fromisoformat(DATA_YEAR_START), date.fromisoformat(DATA_YEAR_END)
    bounded_start = max(parse(start, lower), lower)
    bounded_end = min(parse(end, upper), upper)
    return bounded_start.isoformat(), bounded_end.isoformat()


def parse_changes(html_text: str) -> list[dict[str, Any]]:
    parser = ListParser(CHANGE_FORM_ID)
    parser.feed(html_text)
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


def load_5000_service_orders(database: Any, collection_name: str = "scs_doa_records") -> set[str]:
    """Return service-order numbers belonging to 5000 companies."""
    values: set[str] = set()
    for document in database[collection_name].find({"is_5000_company": True}, {"service_uid": 1, "details.service_fields": 1}):
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


def fetch_changes(
    client: Any,
    start: str = "",
    end: str = "",
    page_size: int = 500,
) -> list[dict[str, Any]]:
    """Fetch all pages for callers that still need the legacy list API.

    The production sync path deliberately does not use this helper because it
    would retain the complete result set in memory.
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = fetch_change_page(client, page, start, end, page_size)
        LOGGER.info(
            "SCS 换上换下列表进度：第 %d 页，当前累计 %d 条",
            page, len(rows) + len(batch),
        )
        if not batch:
            LOGGER.info("SCS 换上换下列表读取完成：共 %d 条", len(rows))
            break
        rows.extend(batch)
        if len(batch) < page_size:
            LOGGER.info("SCS 换上换下列表读取完成：共 %d 条", len(rows))
            break
        page += 1
    return rows


def sync(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    collection_name = env("SCS_CHANGE_COLLECTION", DEFAULT_COLLECTION)
    checkpoint_name = env("SYNC_CHECKPOINT_COLLECTION", DEFAULT_CHECKPOINTS)
    mode = "full" if args.full else "incremental"
    client: MongoClient | None = None
    try:
        with httpx.Client(base_url=BASE_URL, timeout=float(env("SCS_TIMEOUT", "120")), follow_redirects=True) as http:
            login(http)
            checkpoint = None
            if not args.dry_run:
                client = MongoClient(mongo_uri(), **mongo_client_options())
                checkpoint = client[env("MONGODB_DATABASE")][checkpoint_name].find_one({"_id": CHECKPOINT_ID})
            checkpoint_mode = checkpoint.get("mode") if checkpoint else None
            can_resume = bool(
                checkpoint
                and checkpoint.get("status") == "running"
                and (checkpoint_mode is None or checkpoint_mode == mode)
                and (not args.full or checkpoint.get("start_date") == args.start_date)
                and (not checkpoint.get("page_size") or int(checkpoint["page_size"]) == args.page_size)
            )
            if can_resume:
                run_id = str(checkpoint.get("run_id") or "").strip() or run_id
            start, end = "", ""
            if can_resume and checkpoint.get("query_start") is not None:
                start = str(checkpoint.get("query_start") or "")
            elif args.full:
                start = args.start_date
            elif checkpoint and checkpoint.get("watermark"):
                watermark = parse_datetime(checkpoint["watermark"])
                if watermark:
                    start = (watermark - timedelta(days=args.lookback_days)).strftime("%Y-%m-%d")
            start, end = bounded_change_range(start, end)

            resume_page = 1
            resume_row_index = 0
            processed_rows = 0
            if can_resume and checkpoint:
                # ``page`` was added with the streaming checkpoint format. For
                # a legacy checkpoint, convert its global row index to page +
                # offset so an interrupted old run can still be continued.
                legacy_index = max(0, int(checkpoint.get("row_index", 0)))
                resume_page = max(1, int(checkpoint.get("page", legacy_index // args.page_size + 1)))
                resume_row_index = max(0, int(checkpoint.get("page_row_index", checkpoint.get("row_index", 0) % args.page_size)))
                processed_rows = max(0, int(checkpoint.get("fetched_rows", legacy_index)))

            stats: dict[str, Any] = {"success": True, "mode": mode, "fetched": processed_rows, "upserted": 0, "range": {"start": start, "end": end}}
            if args.dry_run:
                page = 1
                while True:
                    batch = fetch_change_page(http, page, start, end, args.page_size)
                    LOGGER.info("SCS 换上换下列表进度：第 %d 页，当前累计 %d 条", page, stats["fetched"] + len(batch))
                    stats["fetched"] += len(batch)
                    if not batch or len(batch) < args.page_size:
                        break
                    page += 1
                LOGGER.info("SCS 换上换下列表读取完成：共 %d 条", stats["fetched"])
                return stats
            if client is None:
                client = MongoClient(mongo_uri(), **mongo_client_options())
            db = client[env("MONGODB_DATABASE")]
            collection = db[collection_name]
            checkpoints = db[checkpoint_name]
            collection.create_index("_source_key", unique=True, name="scs_change_source_key")
            company_orders = load_5000_service_orders(db, env("SCS_DOA_COLLECTION", "scs_doa_records"))
            watermark: datetime | None = parse_datetime(checkpoint.get("watermark")) if can_resume and checkpoint else None
            batch_size = max(1, int(env("SCS_WRITE_BATCH_SIZE", "100")))
            progress_interval = max(1, int(env("SCS_PROGRESS_INTERVAL", str(batch_size))))
            def save_checkpoint(page: int, page_row_index: int, status: str = "running") -> None:
                checkpoints.update_one(
                    {"_id": CHECKPOINT_ID},
                    {"$set": {
                        "dataset": "scs_changes", "status": status, "mode": mode,
                        "start_date": args.start_date if args.full else None,
                        "query_start": start, "query_end": end, "run_id": run_id,
                        "page_size": args.page_size,
                        "page": page, "page_row_index": page_row_index,
                        # Keep row_index as a global offset for compatibility
                        # with existing operational tooling.
                        "row_index": processed_rows,
                        "fetched_rows": processed_rows, "total_rows": processed_rows if status == "completed" else None,
                        "watermark": watermark.isoformat() if watermark else None,
                        "updated_at": datetime.now(timezone.utc),
                    }},
                    upsert=True,
                )

            save_checkpoint(resume_page, resume_row_index)
            LOGGER.info(
                "SCS 换上换下开始处理：从第 %d 页第 %d 条继续，批量写入 %d 条",
                resume_page, resume_row_index, batch_size,
            )
            page = resume_page
            page_row_index = resume_row_index
            while True:
                batch = fetch_change_page(http, page, start, end, args.page_size)
                fetched_estimate = max(stats["fetched"], (page - 1) * args.page_size + len(batch))
                stats["fetched"] = fetched_estimate
                LOGGER.info(
                    "SCS 换上换下抓取进度：第 %d 页，本页 %d 条，已抓取约 %d 条，已入库 %d 条",
                    page, len(batch), fetched_estimate, processed_rows,
                )
                if not batch:
                    break
                operations: list[UpdateOne] = []
                for row_index, row in enumerate(batch):
                    if row_index < page_row_index:
                        continue
                    created = parse_datetime(row.get("create_time"))
                    if created and (watermark is None or created > watermark):
                        watermark = created
                    document = {key: row.get(key, "") for key in FIELD_ALIASES}
                    document.update({
                        "_source_key": change_source_key(row), "_source": "scs", "raw": row.get("raw", {}),
                        "is_5000_company": bool(row.get("so_code") and row["so_code"] in company_orders),
                        "_sync_run_id": run_id, "_synced_at": datetime.now(timezone.utc),
                    })
                    operations.append(UpdateOne({"_source_key": document["_source_key"]}, {"$set": document, "$setOnInsert": {"_id": document["_source_key"]}}, upsert=True))
                    if len(operations) >= batch_size:
                        operation_count = len(operations)
                        result = collection.bulk_write(operations, ordered=False)
                        stats["upserted"] += result.upserted_count + result.modified_count
                        processed_rows += operation_count
                        stats["fetched"] = max(stats["fetched"], processed_rows)
                        operations = []
                        save_checkpoint(page, row_index + 1)
                        LOGGER.info("SCS 换上换下处理进度：第 %d 页第 %d 条，已写入 %d 条", page, row_index + 1, stats["upserted"])
                    elif (row_index + 1) % progress_interval == 0:
                        LOGGER.info("SCS 换上换下页内处理进度：第 %d 页第 %d 条", page, row_index + 1)
                if operations:
                    result = collection.bulk_write(operations, ordered=False)
                    stats["upserted"] += result.upserted_count + result.modified_count
                    processed_rows += len(operations)
                    stats["fetched"] = max(stats["fetched"], processed_rows)
                    save_checkpoint(page, len(batch))
                    LOGGER.info("SCS 换上换下处理进度：第 %d 页完成，已写入 %d 条", page, stats["upserted"])
                if len(batch) < args.page_size:
                    break
                page += 1
                page_row_index = 0
                save_checkpoint(page, page_row_index)
            if args.full:
                stats["deleted_out_of_scope"] = collection.delete_many({"_source": "scs", "_sync_run_id": {"$ne": run_id}}).deleted_count
            save_checkpoint(page + 1 if batch else page, 0, status="completed")
            LOGGER.info(
                "SCS 换上换下同步完成：进度 100%%，共 %d 条，写入 %d 条",
                stats["fetched"], stats["upserted"],
            )
            return stats
    finally:
        if client is not None:
            client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步 SCS 换上换下数据查询记录")
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
