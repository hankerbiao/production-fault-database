"""Internal orchestration helpers for the four table maintenance commands."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo import MongoClient

from scripts.maintenance import clean_order_boards as boards
from scripts.sources.hana.hana_view_sync import (
    VIEW_SPECS,
    env,
    finalize_view_run,
    load_dotenv,
    mongo_client_options,
    mongo_lease_lock,
    mongo_uri,
    mongo_write_concern_summary,
    process_lock,
    sync_view,
)


SERIAL_KEY_FIELDS = ("ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM", "PRODH")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD") from exc


def build_table_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--start-date", type=parse_date, help="全量起始日期；增量时可限制预览范围")
    parser.add_argument("--end-date", type=parse_date, default=date.today(), help="同步结束日期，默认今天")
    parser.add_argument("--lookback-days", type=int, default=int(env("SYNC_LOOKBACK_DAYS", "7")))
    parser.add_argument("--batch-size", type=int, default=int(env("SYNC_BATCH_SIZE", "1000")))
    parser.add_argument("--limit", type=int, help="清洗时最多扫描的记录数")
    parser.add_argument("--no-progress", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="只请求并预览，默认")
    action.add_argument("--apply", action="store_true", help="执行同步、字段补全和删除")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.batch_size < 1 or args.lookback_days < 0:
        raise ValueError("--batch-size 必须大于 0，--lookback-days 不能为负数")
    if args.start_date and args.start_date > args.end_date:
        raise ValueError("--start-date 不能晚于 --end-date")
    if args.mode == "full" and not args.start_date:
        raise ValueError("全量同步必须提供 --start-date YYYY-MM-DD")


def serial_cleanup(db: Any, spec: Any, *, apply: bool, full: bool, run_id: str, batch_size: int) -> dict[str, Any]:
    query: dict[str, Any] = {"_source_view": spec.view_id}
    if not full:
        query["_sync_run_id"] = run_id
    collection = db[spec.collection]
    grouped: dict[tuple[str, ...], list[Any]] = {}
    incomplete = scanned = 0
    cursor = collection.find(query, {"_id": 1, **{field: 1 for field in SERIAL_KEY_FIELDS}})
    try:
        for row in cursor:
            scanned += 1
            values = tuple(str(row.get(field) or "").strip() for field in SERIAL_KEY_FIELDS)
            if not all(values):
                incomplete += 1
                continue
            grouped.setdefault(values, []).append(row["_id"])
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    duplicate_ids = [item for ids in grouped.values() for item in ids[1:]]
    deleted = 0
    if apply:
        for start in range(0, len(duplicate_ids), batch_size):
            deleted += collection.delete_many({"_id": {"$in": duplicate_ids[start:start + batch_size]}}).deleted_count
    return {
        "success": True,
        "scanned": scanned,
        "complete_business_keys": len(grouped),
        "incomplete_business_key_rows": incomplete,
        "duplicate_candidates": len(duplicate_ids),
        "deleted": deleted,
    }


def run_hana_table(spec_id: str, cleanup_kind: str, args: argparse.Namespace) -> dict[str, Any]:
    """Synchronize one HANA view, clean it, then commit its watermark and scope."""
    validate_args(args)
    spec = VIEW_SPECS[spec_id]
    load_dotenv()
    run_started = datetime.now(timezone.utc)
    options = mongo_client_options()
    mode = args.mode
    result: dict[str, Any] = {
        "success": False,
        "table": spec.collection,
        "view_id": spec.view_id,
        "mode": mode,
        "dry_run": not args.apply,
        "started_at": run_started,
        "sync": {},
        "cleanup": {},
    }
    client = MongoClient(mongo_uri(), **options)
    try:
        db = client[env("MONGODB_DATABASE")]
        lock_name = f"table-pipeline:{spec.view_id}"
        with process_lock(env("SYNC_LOCK_PATH", "/tmp/line-fault-table-sync.lock")):
            with mongo_lease_lock(db, env("SYNC_DISTRIBUTED_LOCK_NAME", lock_name)):
                sync_result = sync_view(
                    spec,
                    mode=mode,
                    start_date=args.start_date.strftime("%Y%m%d") if args.start_date else None,
                    end_date=args.end_date.strftime("%Y%m%d"),
                    batch_size=args.batch_size,
                    lookback_days=args.lookback_days,
                    dry_run=not args.apply,
                    defer_finalize=args.apply,
                )
                result["run_id"] = sync_result["run_id"]
                result["sync"] = sync_result
                if cleanup_kind == "serial":
                    cleanup = serial_cleanup(
                        db, spec, apply=args.apply, full=mode == "full" or not args.apply,
                        run_id=sync_result["run_id"], batch_size=args.batch_size,
                    )
                else:
                    cleanup = boards.process_board(
                        db,
                        boards.SPECS[cleanup_kind],
                        spec.collection,
                        env("TARGET_COLLECTION", "sales_orders_sap"),
                        apply=args.apply,
                        batch_size=args.batch_size,
                        limit=args.limit,
                        from_date=args.start_date if not args.apply else None,
                        to_date=args.end_date if not args.apply and args.start_date else None,
                        sync_run_id=None if mode == "full" or not args.apply else sync_result["run_id"],
                        progress=not args.no_progress,
                    )
                result["cleanup"] = cleanup
                if args.apply:
                    result["commit"] = finalize_view_run(spec, sync_result)
        result["success"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
    finally:
        result["finished_at"] = datetime.now(timezone.utc)
        result["mongo_write_concern"] = mongo_write_concern_summary(options)
        try:
            client[env("MONGODB_DATABASE")][env("TABLE_PIPELINE_RUN_COLLECTION", "sync_runs")].insert_one(
                {**result, "status": "success" if result.get("success") else "failed"}
            )
        except Exception:
            pass
        client.close()


def print_result(result: dict[str, Any]) -> int:
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0 if result.get("success") else 1
