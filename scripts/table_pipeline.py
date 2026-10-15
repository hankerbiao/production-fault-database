"""Internal orchestration helpers for the four table maintenance commands."""
from __future__ import annotations

import argparse
import json
from contextlib import nullcontext
from itertools import chain
from datetime import date, datetime, timezone
from typing import Any

from pymongo import MongoClient

from scripts.maintenance import clean_order_boards as boards
from scripts.sources.hana.hana_view_sync import (
    configured_spec,
    env,
    finalize_view_run,
    load_dotenv,
    mongo_client_options,
    mongo_uri,
    mongo_write_concern_summary,
    process_lock,
    sync_view,
)
from scripts.sync_runtime import Snapshot, cleanup_records, check_leases, resource_lock


SERIAL_KEY_FIELDS = ("ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM", "PRODH")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD") from exc


def build_table_parser(description: str) -> argparse.ArgumentParser:
    load_dotenv()
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--start-date", type=parse_date, help="全量起始日期；增量时可限制预览范围")
    parser.add_argument("--end-date", type=parse_date, default=date.today(), help="同步结束日期，默认今天")
    parser.add_argument("--lookback-days", type=int, default=int(env("SYNC_LOOKBACK_DAYS", "7")))
    parser.add_argument("--batch-size", type=int, default=int(env("SYNC_BATCH_SIZE", "1000")))
    parser.add_argument("--limit", type=int, help="清洗时最多扫描的记录数")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--allow-large-cleanup", action="store_true", help="确认源收缩后允许空快照或大量旧记录清理")
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


def serial_cleanup(db: Any, spec: Any, *, apply: bool, run_id: str, batch_size: int) -> dict[str, Any]:
    query: dict[str, Any] = {"_source_view": spec.view_id}
    collection = db[spec.collection]
    incomplete = scanned = duplicate_count = deleted = 0
    duplicate_ids = []
    projection = {"_id": 1, **{field: 1 for field in SERIAL_KEY_FIELDS}}
    cursors = [
        collection.find({**query, "_sync_run_id": run_id}, projection),
        collection.find({**query, "_sync_run_id": {"$ne": run_id}}, projection),
    ]
    seen = Snapshot()

    def flush() -> None:
        nonlocal deleted
        if duplicate_ids and apply:
            selected = {**query, "_id": {"$in": list(duplicate_ids)}}
            deleted += cleanup_records(db, spec.collection, selected, selected, 1,
                                       allow_large_cleanup=True, batch_size=batch_size)
        duplicate_ids.clear()

    try:
        for row in chain(*cursors):
            scanned += 1
            values = tuple(str(row.get(field) or "").strip() for field in SERIAL_KEY_FIELDS)
            if not all(values):
                incomplete += 1
                continue
            key = json.dumps(values, ensure_ascii=False)
            if seen.contains(key):
                duplicate_count += 1
                duplicate_ids.append(row["_id"])
                if len(duplicate_ids) >= batch_size:
                    flush()
            else:
                seen.add(key, {})
        flush()
        complete_keys = seen.count
    finally:
        seen.__exit__()
        for cursor in cursors:
            if hasattr(cursor, "close"):
                cursor.close()
    return {
        "success": True,
        "scanned": scanned,
        "complete_business_keys": complete_keys,
        "incomplete_business_key_rows": incomplete,
        "duplicate_candidates": duplicate_count,
        "deleted": deleted,
    }


def run_hana_table(spec_id: str, cleanup_kind: str, args: argparse.Namespace) -> dict[str, Any]:
    """Synchronize one HANA view, clean it, then commit its watermark and scope."""
    validate_args(args)
    load_dotenv()
    spec = configured_spec(spec_id)
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
    preview = Snapshot() if not args.apply else None
    try:
        db = client[env("MONGODB_DATABASE")]
        with process_lock(env("SYNC_LOCK_PATH", "/tmp/line-fault-table-sync.lock")) if args.apply else nullcontext():
            with resource_lock(db, spec.collection) if args.apply else nullcontext():
                sync_result = sync_view(
                    spec,
                    mode=mode,
                    start_date=args.start_date.strftime("%Y%m%d") if args.start_date else None,
                    end_date=args.end_date.strftime("%Y%m%d"),
                    batch_size=args.batch_size,
                    lookback_days=args.lookback_days,
                    dry_run=not args.apply,
                    defer_finalize=args.apply,
                    preview_snapshot=preview,
                    allow_large_cleanup=getattr(args, "allow_large_cleanup", False),
                )
                result["run_id"] = sync_result["run_id"]
                result["sync"] = sync_result
                if not sync_result.get("success"):
                    raise RuntimeError("源快照校验失败")
                if cleanup_kind == "serial":
                    cleanup = serial_cleanup(
                        {spec.collection: preview} if preview is not None else db,
                        spec, apply=args.apply,
                        run_id=sync_result["run_id"], batch_size=args.batch_size,
                    )
                else:
                    cleanup = boards.process_board(
                        {spec.collection: preview, env("TARGET_COLLECTION", "sales_orders_sap"):
                         db[env("TARGET_COLLECTION", "sales_orders_sap")]} if preview is not None else db,
                        boards.SPECS[cleanup_kind],
                        spec.collection,
                        env("TARGET_COLLECTION", "sales_orders_sap"),
                        apply=args.apply,
                        batch_size=args.batch_size,
                        limit=args.limit,
                        from_date=args.start_date,
                        to_date=args.end_date,
                        sync_run_id=None if mode == "full" or not args.apply else sync_result["run_id"],
                        progress=not args.no_progress,
                    )
                result["cleanup"] = cleanup
                if preview is not None:
                    result["cleanup_basis"] = "validated_source_snapshot"
                if args.apply:
                    check_leases(verify=True)
                    result["commit"] = finalize_view_run(spec, sync_result)
        result["success"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
    finally:
        result["finished_at"] = datetime.now(timezone.utc)
        result["mongo_write_concern"] = mongo_write_concern_summary(options)
        if args.apply:
            try:
                client[env("MONGODB_DATABASE")][env("TABLE_PIPELINE_RUN_COLLECTION", "sync_runs")].insert_one(
                    {**result, "status": "success" if result.get("success") else "failed"}
                )
            except Exception:
                pass
        client.close()
        if preview is not None:
            preview.__exit__()


def print_result(result: dict[str, Any]) -> int:
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0 if result.get("success") else 1
