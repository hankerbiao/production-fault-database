#!/usr/bin/env python3
"""Backfill BOM posting planned start time from synchronized sales orders.

The sales-order collection is the authoritative local source for GSTRS. A BOM
row is matched by its production/sales order pair and client. Numeric order
keys are compared after removing SAP left padding zeros. Existing values are
corrected only when a unique authoritative date is found; rows without both
order numbers are ignored.

The default mode is a read-only preview. Cron should pass ``--apply``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient, UpdateOne

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sources.hana.hana_view_sync import (
    env,
    load_dotenv,
    mongo_client_options,
    mongo_uri,
    process_lock,
)
from scripts.sync_runtime import order_key, print_failure, resource_locks, verified_bulk_write
from scripts.sales_order_reference import SalesReferences


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


def normalize_planned_date(value: Any) -> str:
    """Return a canonical ISO date without accepting malformed source values."""
    raw = text(value)
    if not raw:
        return ""
    for layout in (
        "%Y-%m-%d",
        "%Y%m%d",
        "%Y/%m/%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y%m%d%H%M%S",
    ):
        try:
            return datetime.strptime(raw, layout).date().isoformat()
        except ValueError:
            continue
    return ""


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


def flush_operations(collection: Any, operations: list[UpdateOne], *, apply: bool) -> tuple[int, int]:
    if not operations or not apply:
        return 0, 0
    result = verified_bulk_write(collection, operations)
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
    progress = ProgressReporter(db, run_id if apply else None)
    refs = SalesReferences.load(sales_orders)
    summary: dict[str, Any] = {
        "success": True,
        "dry_run": not apply,
        "bom_collection": bom_collection_name,
        "source_collection": sales_collection_name,
        "source_rows_scanned": sales_orders.count_documents({}),
        "source_rows_with_planned_start": sum(bool(row["planned"]) for row in refs.rows),
        "source_rows_without_planned_start": sum(not row["planned"] for row in refs.rows),
        "bom_rows_scanned": 0,
        "skipped_empty_orders": 0,
        "matched_by_production_order": 0,
        "matched_by_sales_order": 0,
        "unmatched_orders": 0,
        "ambiguous_orders": 0,
        "missing_gstrs": 0,
        "unchanged": 0,
        "normalized": 0,
        "corrected": 0,
        "write_attempted": 0,
        "write_matched": 0,
        "write_modified": 0,
        "preview": [],
    }
    operations: list[UpdateOne] = []

    def enqueue(document_id: Any, fields: dict[str, str]) -> None:
        operations.append(UpdateOne({"_id": document_id}, {"$set": fields}))
        if len(operations) >= batch_size:
            matched, modified = flush_operations(bom, operations, apply=apply)
            summary["write_attempted"] += len(operations) if apply else 0
            summary["write_matched"] += matched
            summary["write_modified"] += modified
            operations.clear()

    total = bom.count_documents({})
    progress.report("扫描并规范化 BOM 计划开始时间", 0, total, force=True)
    cursor = bom.find(
        {},
        {"_id": 1, "AUFNR_1": 1, "VBELN_EX": 1, "MANDT": 1, "GSTRS": 1, "GSTRS_DATE": 1},
    )
    try:
        for document in cursor:
            summary["bom_rows_scanned"] += 1
            progress.report("扫描并规范化 BOM 计划开始时间", summary["bom_rows_scanned"], total)
            production_order_raw = text(document.get("AUFNR_1"))
            sales_order_raw = text(document.get("VBELN_EX"))
            production_order = order_key(production_order_raw)
            sales_order = order_key(sales_order_raw)
            existing_raw = text(document.get("GSTRS"))
            existing_date = normalize_planned_date(document.get("GSTRS_DATE"))
            if not existing_date:
                existing_date = normalize_planned_date(existing_raw)
            if not existing_raw:
                summary["missing_gstrs"] += 1
            if not production_order and not sales_order:
                summary["skipped_empty_orders"] += 1
                if existing_date and existing_date != text(document.get("GSTRS_DATE")):
                    enqueue(document["_id"], {"GSTRS_DATE": existing_date})
                    summary["normalized"] += 1
                continue

            keys = {"production": production_order_raw, "sales": sales_order_raw, "client": document.get("MANDT")}
            candidates = refs.candidates(**keys)
            dates = {normalize_planned_date(row["planned"]) for row in candidates}
            identities = {(row["source"], row["client"], row["production"]) for row in candidates}
            sales_values = {row["sales"] for row in candidates}
            valid_clients = all(row["client"] == {"SG": "800", "KK": "600"}.get(row["source"], row["client"]) for row in candidates)
            planned_start = next(iter(dates)) if len(identities) == 1 and len(sales_values) == 1 and valid_clients and len(dates) == 1 and "" not in dates else ""
            match_type = "production_order" if production_order else "sales_order"

            if not planned_start:
                if candidates:
                    summary["ambiguous_orders"] += 1
                else:
                    summary["unmatched_orders"] += 1
                if existing_date and existing_date != text(document.get("GSTRS_DATE")):
                    enqueue(document["_id"], {"GSTRS_DATE": existing_date})
                    summary["normalized"] += 1
                continue

            if match_type == "production_order":
                summary["matched_by_production_order"] += 1
            else:
                summary["matched_by_sales_order"] += 1
            if existing_raw and normalize_planned_date(existing_raw) != planned_start:
                status = "corrected"
                summary["corrected"] += 1
            elif not existing_raw:
                status = "missing"
            elif text(document.get("GSTRS_DATE")) != planned_start:
                status = "normalized"
                summary["normalized"] += 1
            else:
                status = "unchanged"
                summary["unchanged"] += 1
            if len(summary["preview"]) < preview_limit:
                summary["preview"].append({
                    "id": str(document["_id"]),
                    "AUFNR_1": production_order_raw,
                    "VBELN_EX": sales_order_raw,
                    "before_GSTRS": existing_raw,
                    "GSTRS": planned_start,
                    "GSTRS_DATE": planned_start,
                    "matched_by": match_type,
                    "status": status,
                })
            fields = {}
            if text(document.get("GSTRS_DATE")) != planned_start:
                fields["GSTRS_DATE"] = planned_start
            if existing_raw != planned_start:
                fields["GSTRS"] = planned_start
            if fields:
                enqueue(document["_id"], fields)
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
        with process_lock(lock_path) if args.apply else nullcontext(), \
                resource_locks(db, args.bom_collection, args.source_collection) if args.apply else nullcontext():
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
        return print_failure(exc)
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
