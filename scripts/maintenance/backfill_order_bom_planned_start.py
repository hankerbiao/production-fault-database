#!/usr/bin/env python3
"""Backfill BOM posting planned start time from synchronized sales orders.

The sales-order collection is the authoritative local source for GSTRS. A BOM
row is matched by AUFNR_1 first and VBELN_EX second. Numeric production-order
keys are compared after removing SAP left padding zeros. Existing GSTRS values
are never overwritten; rows without both order numbers are ignored.

The default mode is a read-only preview. Cron should pass ``--apply``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pymongo import MongoClient, UpdateOne

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sources.hana.hana_view_sync import (
    env,
    load_dotenv,
    mongo_client_options,
    mongo_lease_lock,
    mongo_uri,
    process_lock,
)


ORCHESTRATOR_RUN_COLLECTION = "sync_orchestrator_runs"
TASK_ID = "order_bom_planned_start"


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def order_key(value: Any) -> str:
    """Normalize SAP numeric order numbers while preserving alphanumeric IDs."""
    value = text(value)
    if value.isdigit():
        return value.lstrip("0") or "0"
    return value


def missing_text_filter(field: str) -> dict[str, Any]:
    return {
        "$or": [
            {field: {"$exists": False}},
            {field: None},
            {field: ""},
            {field: {"$regex": r"^\s*$"}},
        ]
    }


def add_candidate(index: dict[str, set[str]], key: str, value: str) -> None:
    if key and value:
        index[key].add(value)


def unique_value(index: Mapping[str, set[str]], key: str) -> tuple[str, str]:
    values = index.get(key, set())
    if len(values) == 1:
        return next(iter(values)), "matched"
    if len(values) > 1:
        return "", "ambiguous"
    return "", "unmatched"


class ProgressReporter:
    def __init__(self, database: Any, run_id: str | None) -> None:
        self.collection = database[ORCHESTRATOR_RUN_COLLECTION] if run_id else None
        self.run_id = run_id
        self.last_report_at = 0.0

    def report(self, phase: str, scanned: int, total: int, *, force: bool = False) -> None:
        if self.collection is None or not self.run_id:
            return
        now = time.monotonic()
        if not force and scanned < total and now - self.last_report_at < 1.0:
            return
        self.last_report_at = now
        percent = round(scanned * 100 / total, 1) if total else 100.0
        self.collection.update_one(
            {"_id": self.run_id, "state": "running", "stages.task_id": TASK_ID},
            {"$set": {
                "stages.$.progress": {"phase": phase, "scanned": scanned, "total": total, "percent": percent},
                "heartbeat_at": datetime.now(timezone.utc),
            }},
        )


def build_source_indexes(
    collection: Any,
    progress: ProgressReporter | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, int]]:
    """Build unambiguous production-order and sales-order -> GSTRS indexes."""
    production: dict[str, set[str]] = defaultdict(set)
    sales: dict[str, set[str]] = defaultdict(set)
    stats = {
        "source_rows_scanned": 0,
        "source_rows_with_planned_start": 0,
        "source_rows_without_planned_start": 0,
    }
    total = collection.count_documents({})
    progress and progress.report("构建销售订单索引", 0, total, force=True)
    projection = {
        "aufnr": 1,
        "gstrs_date": 1,
        "data.AUFNR": 1,
        "data.VBELN": 1,
        "data.GSTRS": 1,
    }
    cursor = collection.find({}, projection)
    try:
        for document in cursor:
            stats["source_rows_scanned"] += 1
            progress and progress.report("构建销售订单索引", stats["source_rows_scanned"], total)
            data = document.get("data") if isinstance(document.get("data"), Mapping) else {}
            planned_start = text(data.get("GSTRS") or document.get("gstrs_date"))
            if not planned_start:
                stats["source_rows_without_planned_start"] += 1
                continue
            stats["source_rows_with_planned_start"] += 1
            production_order = order_key(data.get("AUFNR") or document.get("aufnr"))
            sales_order = order_key(data.get("VBELN"))
            add_candidate(production, production_order, planned_start)
            add_candidate(sales, sales_order, planned_start)
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    progress and progress.report("构建销售订单索引", total, total, force=True)
    return production, sales, stats


def flush_operations(collection: Any, operations: list[UpdateOne], *, apply: bool) -> tuple[int, int]:
    if not operations or not apply:
        return 0, 0
    result = collection.bulk_write(operations, ordered=False)
    return result.matched_count, result.modified_count


def backfill(
    db: Any,
    *,
    bom_collection_name: str,
    sales_collection_name: str,
    batch_size: int,
    preview_limit: int,
    apply: bool,
    run_id: str | None = None,
) -> dict[str, Any]:
    bom = db[bom_collection_name]
    sales_orders = db[sales_collection_name]
    progress = ProgressReporter(db, run_id)
    production_index, sales_index, source_stats = build_source_indexes(sales_orders, progress)
    summary: dict[str, Any] = {
        "success": True,
        "dry_run": not apply,
        "bom_collection": bom_collection_name,
        "source_collection": sales_collection_name,
        **source_stats,
        "bom_rows_scanned": 0,
        "skipped_empty_orders": 0,
        "matched_by_production_order": 0,
        "matched_by_sales_order": 0,
        "unmatched_orders": 0,
        "ambiguous_orders": 0,
        "write_attempted": 0,
        "write_matched": 0,
        "write_modified": 0,
        "preview": [],
    }
    operations: list[UpdateOne] = []
    total = bom.count_documents(missing_text_filter("GSTRS"))
    progress.report("扫描待补充 BOM 明细", 0, total, force=True)
    cursor = bom.find(
        missing_text_filter("GSTRS"),
        {"_id": 1, "AUFNR_1": 1, "VBELN_EX": 1, "GSTRS": 1},
    )
    try:
        for document in cursor:
            summary["bom_rows_scanned"] += 1
            progress.report("扫描待补充 BOM 明细", summary["bom_rows_scanned"], total)
            production_order_raw = text(document.get("AUFNR_1"))
            sales_order_raw = text(document.get("VBELN_EX"))
            production_order = order_key(production_order_raw)
            sales_order = order_key(sales_order_raw)
            if not production_order and not sales_order:
                summary["skipped_empty_orders"] += 1
                continue

            planned_start = ""
            match_type = ""
            if production_order:
                planned_start, status = unique_value(production_index, production_order)
                if status == "matched":
                    match_type = "production_order"
                elif status == "ambiguous":
                    summary["ambiguous_orders"] += 1
            if not planned_start and sales_order:
                planned_start, status = unique_value(sales_index, sales_order)
                if status == "matched":
                    match_type = "sales_order"
                elif status == "ambiguous":
                    summary["ambiguous_orders"] += 1

            if not planned_start:
                if production_order or sales_order:
                    if not (
                        production_order in production_index
                        or sales_order in sales_index
                    ):
                        summary["unmatched_orders"] += 1
                continue

            if match_type == "production_order":
                summary["matched_by_production_order"] += 1
            else:
                summary["matched_by_sales_order"] += 1
            if len(summary["preview"]) < preview_limit:
                summary["preview"].append({
                    "id": str(document["_id"]),
                    "AUFNR_1": production_order_raw,
                    "VBELN_EX": sales_order_raw,
                    "GSTRS": planned_start,
                    "matched_by": match_type,
                })
            operations.append(
                UpdateOne(
                    {"_id": document["_id"], **missing_text_filter("GSTRS")},
                    {"$set": {"GSTRS": planned_start}},
                )
            )
            if len(operations) >= batch_size:
                matched, modified = flush_operations(bom, operations, apply=apply)
                summary["write_attempted"] += len(operations) if apply else 0
                summary["write_matched"] += matched
                summary["write_modified"] += modified
                operations.clear()
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    matched, modified = flush_operations(bom, operations, apply=apply)
    summary["write_attempted"] += len(operations) if apply else 0
    summary["write_matched"] += matched
    summary["write_modified"] += modified
    progress.report("回填完成", total, total, force=True)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 sales_orders_sap 按生产订单/销售订单补充 BOM 明细 GSTRS"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="只统计和预览，不写入数据库（默认）")
    action.add_argument("--apply", action="store_true", help="执行数据库回填")
    parser.add_argument("--batch-size", type=int, default=int(env("SYNC_BATCH_SIZE", "1000")))
    parser.add_argument("--preview-limit", type=int, default=20, help="输出预览条数")
    parser.add_argument("--bom-collection", default=env("BOM_COLLECTION", "order_bom_postings_sap"))
    parser.add_argument("--source-collection", default=env("TARGET_COLLECTION", "sales_orders_sap"))
    parser.add_argument("--run-id", help="由 syncctl 传入，用于写入可查询的执行进度")
    return parser


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args()
    if args.batch_size < 1:
        print(json.dumps({"success": False, "error": "--batch-size 必须大于 0"}, ensure_ascii=False))
        return 1
    if args.preview_limit < 0:
        print(json.dumps({"success": False, "error": "--preview-limit 不能为负数"}, ensure_ascii=False))
        return 1

    client = None
    try:
        client = MongoClient(mongo_uri(), **mongo_client_options())
        db = client[env("MONGODB_DATABASE")]
        lock_path = env("BOM_PLANNED_START_LOCK_PATH", "/tmp/order-bom-planned-start.lock")
        lock_name = env("BOM_PLANNED_START_LOCK_NAME", "order-bom-planned-start")
        with process_lock(lock_path), mongo_lease_lock(db, lock_name):
            result = backfill(
                db,
                bom_collection_name=args.bom_collection,
                sales_collection_name=args.source_collection,
                batch_size=args.batch_size,
                preview_limit=args.preview_limit,
                apply=args.apply,
                run_id=args.run_id,
            )
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
