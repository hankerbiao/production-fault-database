#!/usr/bin/env python3
"""Synchronize and clean repair records, including their sales-order dependency."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from pymongo import MongoClient

from scripts.maintenance import repair_cleanup
from scripts.sources.hana.hana_view_sync import (
    env,
    load_dotenv,
    mongo_client_options,
    mongo_lease_lock,
    mongo_uri,
    mongo_write_concern_summary,
    process_lock,
)
from scripts.sources.sap_http.sync_sales_orders import finalize_repair_run, sync_repair, sync_sales


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步维修故障明细，并自动补全订单和计划开始时间")
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--start-date", type=parse_date, help="全量同步起始日期")
    parser.add_argument("--end-date", type=parse_date, default=date.today(), help="结束日期，默认今天")
    parser.add_argument("--lookback-days", type=int, default=int(env("SYNC_LOOKBACK_DAYS", "7")))
    parser.add_argument("--batch-size", type=int, default=int(env("SYNC_BATCH_SIZE", "1000")))
    parser.add_argument("--limit", type=int, help="清洗时最多处理记录数")
    parser.add_argument("--sales-only", action="store_true", help="仅刷新维修和工位清洗依赖的销售订单表")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"))
    parser.add_argument("--log-file", default=env("LOG_FILE", ""))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--apply", action="store_true")
    return parser


def validate(args: argparse.Namespace) -> None:
    if args.mode == "full" and not args.start_date:
        raise ValueError("全量同步必须提供 --start-date YYYY-MM-DD")
    if args.start_date and args.start_date > args.end_date:
        raise ValueError("--start-date 不能晚于 --end-date")
    if args.batch_size < 1 or args.lookback_days < 0:
        raise ValueError("--batch-size 必须大于 0，--lookback-days 不能为负数")


def cleanup_args(args: argparse.Namespace, run_id: str, sync_result: dict[str, Any]) -> SimpleNamespace:
    sync_range = sync_result.get("range", {})
    return SimpleNamespace(
        repair_collection=env("REPAIR_COLLECTION", "repair_records_sap"),
        station_collection=env("STATION_COLLECTION", "station_records_sap"),
        order_collection=env("TARGET_COLLECTION", "sales_orders_sap"),
        apply=args.apply,
        batch_size=args.batch_size,
        retries=int(env("SN_RETRIES", "2")),
        retry_delay=float(env("SN_RETRY_DELAY", "1")),
        timeout=float(env("HTTP_TIMEOUT", "120")),
        from_date=None if args.apply else date.fromisoformat(sync_range["start"]),
        to_date=None if args.apply else date.fromisoformat(sync_range["end"]),
        limit=args.limit,
        missing_sales_only=False,
        all_source_views=False,
        no_progress=args.no_progress,
        sync_run_id=None if args.mode == "full" or not args.apply else run_id,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    validate(args)
    load_dotenv()
    repair_cleanup.configure_logging(args.log_level, args.log_file or None)
    dry_run = not args.apply
    full = args.mode == "full"
    run_id = uuid4().hex
    started_at = datetime.now(timezone.utc)
    options = mongo_client_options()
    result: dict[str, Any] = {
        "success": False,
        "run_id": run_id,
        "mode": args.mode,
        "dry_run": dry_run,
        "started_at": started_at,
        "sales_sync": {},
        "sync": {},
        "cleanup": {},
    }
    client = MongoClient(mongo_uri(), **options)
    try:
        db = client[env("MONGODB_DATABASE")]
        with process_lock(env("SYNC_LOCK_PATH", "/tmp/line-fault-table-sync.lock")):
            with mongo_lease_lock(db, env("SYNC_DISTRIBUTED_LOCK_NAME", "repair-records-pipeline")):
                sales_args = SimpleNamespace(
                    dataset="sales", full=full, start_date=args.start_date, end_date=args.end_date,
                    lookback_days=args.lookback_days, dry_run=dry_run, all_prodh=False, prodh_list=None,
                )
                result["sales_sync"] = sync_sales(sales_args)
                if not result["sales_sync"].get("success"):
                    raise RuntimeError("销售订单同步失败，停止维修记录处理")
                if args.sales_only:
                    result["success"] = True
                    return result
                sync_result = sync_repair(
                    db, env("REPAIR_COLLECTION", "repair_records_sap"),
                    env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints"), args.mode,
                    args.start_date, args.end_date, args.lookback_days, args.batch_size,
                    dry_run, run_id, defer_finalize=args.apply,
                )
                result["sync"] = sync_result
                result["cleanup"] = repair_cleanup.repair_workflow(
                    db, cleanup_args(args, run_id, sync_result), run_id=run_id,
                )
                if args.apply:
                    result["commit"] = finalize_repair_run(
                        db, env("REPAIR_COLLECTION", "repair_records_sap"),
                        env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints"), sync_result, run_id,
                    )
        result["success"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
    finally:
        result["finished_at"] = datetime.now(timezone.utc)
        result["mongo_write_concern"] = mongo_write_concern_summary(options)
        try:
            client[env("MONGODB_DATABASE")][env("SYNC_RUN_COLLECTION", "sync_runs")].insert_one(
                {**result, "status": "success" if result.get("success") else "failed"}
            )
        except Exception:
            pass
        client.close()


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run(args)
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
