#!/usr/bin/env python3
"""Preview or permanently remove repair records with unknown sales orders.

The sales-order collection is the source of truth.  Preview is the default;
deletion requires both ``--apply`` and the explicit confirmation token.
"""
from __future__ import annotations

import argparse
import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

try:
    from pymongo import MongoClient
except ImportError as exc:  # pragma: no cover - deployment error
    raise SystemExit(f"missing dependency: {exc.name}; install pymongo") from exc

from hana_view_sync import (
    mongo_client_options,
    mongo_lease_lock,
    mongo_write_concern_summary,
    stream_nonempty_field_values,
)
from sync_sales_orders import env, load_dotenv, mongo_uri, process_lock


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_VIEW = "ZSGV_ZZT_WLJL"
CONFIRMATION = "DELETE-ORPHAN-REPAIRS"
DEFAULT_BATCH_SIZE = 500
DEFAULT_CLEANUP_LOCK = "/tmp/line-fault-orphan-repair-cleanup.lock"
logger = logging.getLogger(__name__)


class CleanupError(RuntimeError):
    """Failure carrying the progress accumulated before the failed batch."""

    def __init__(self, message: str, stats: dict[str, Any]):
        super().__init__(message)
        self.stats = stats


def normalize_order(value: Any) -> str:
    """Convert an order value to the exact comparison representation."""
    return "" if value is None else str(value).strip()


def sales_order_vbelns(collection: Any) -> set[str]:
    return stream_nonempty_field_values(collection, "data.VBELN")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD: {value}") from exc


def build_filter(source_view: str | None, all_source_views: bool, from_date: date | None, to_date: date | None) -> dict[str, Any]:
    conditions: dict[str, Any] = {}
    if not all_source_views:
        conditions["_source_view"] = source_view or DEFAULT_SOURCE_VIEW
    if from_date or to_date:
        date_filter: dict[str, str] = {}
        if from_date:
            date_filter["$gte"] = from_date.strftime("%Y%m%d")
        if to_date:
            date_filter["$lte"] = to_date.strftime("%Y%m%d")
        conditions["ZDATE_WX"] = date_filter
    return conditions


@contextmanager
def cleanup_locks(cleanup_path: str, sync_path: str) -> Iterator[None]:
    """Serialize cleaners and coordinate with the existing sync process lock."""
    if Path(cleanup_path).expanduser().resolve() == Path(sync_path).expanduser().resolve():
        raise ValueError("CLEANUP_LOCK_PATH 不能与 SYNC_LOCK_PATH 相同")
    with process_lock(cleanup_path):
        with process_lock(sync_path):
            yield


def _cursor_batches(cursor: Any, batch_size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for document in cursor:
        batch.append(document)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def clean_records(
    db: Any,
    repair_collection: str,
    order_collection: str,
    audit_collection: str,
    *,
    apply: bool,
    batch_size: int,
    limit: int | None,
    source_view: str | None,
    all_source_views: bool,
    from_date: date | None,
    to_date: date | None,
    run_id: str,
) -> dict[str, Any]:
    if batch_size < 1:
        raise ValueError("--batch-size 必须大于 0")
    if limit is not None and limit < 1:
        raise ValueError("--limit 必须大于 0")
    if from_date and to_date and from_date > to_date:
        raise ValueError("--from-date 不能晚于 --to-date")

    orders = sales_order_vbelns(db[order_collection])
    if not orders:
        raise RuntimeError(f"{order_collection} 中没有有效 VBELN，拒绝清理")

    repair_filter = build_filter(source_view, all_source_views, from_date, to_date)
    stats: dict[str, Any] = {
        "run_id": run_id,
        "mode": "apply" if apply else "dry-run",
        "repair_collection": repair_collection,
        "order_collection": order_collection,
        "audit_collection": audit_collection,
        "source_view": None if all_source_views else (source_view or DEFAULT_SOURCE_VIEW),
        "all_source_views": all_source_views,
        "from_date": from_date.isoformat() if from_date else None,
        "to_date": to_date.isoformat() if to_date else None,
        "batch_size": batch_size,
        "limit": limit,
        "sales_order_vbelns": len(orders),
        "scanned": 0,
        "empty_sales_order": 0,
        "unmatched_sales_order": 0,
        "orphan_records": 0,
        "deleted": 0,
        "batches": 0,
    }
    projection = {"_id": 1, "VBELN": 1}
    cursor = db[repair_collection].find(repair_filter, projection)
    candidates: list[dict[str, Any]] = []
    try:
        if hasattr(cursor, "batch_size"):
            cursor = cursor.batch_size(batch_size)
        if hasattr(cursor, "sort"):
            cursor = cursor.sort("_id", 1)
        for documents in _cursor_batches(cursor, batch_size):
            for document in documents:
                if limit is not None and stats["orphan_records"] >= limit:
                    break
                stats["scanned"] += 1
                order = normalize_order(document.get("VBELN"))
                if not order:
                    stats["empty_sales_order"] += 1
                    stats["orphan_records"] += 1
                elif order not in orders:
                    stats["unmatched_sales_order"] += 1
                    stats["orphan_records"] += 1
                else:
                    continue
                candidates.append(document)
            if limit is not None and stats["orphan_records"] >= limit:
                break
    except Exception as exc:
        raise CleanupError(str(exc), stats) from exc
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    try:
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset : offset + batch_size]
            stats["batches"] += 1
            if not apply:
                continue
            # Refresh the whitelist before each destructive batch so an order
            # added while scanning cannot make its repair rows eligible for delete.
            current_orders = sales_order_vbelns(db[order_collection])
            if not current_orders:
                raise CleanupError(f"{order_collection} 中没有有效 VBELN，拒绝继续清理", stats)
            delete_filter = {
                "$or": [
                    {"_id": document.get("_id"), "VBELN": document.get("VBELN")}
                    for document in batch
                    if normalize_order(document.get("VBELN")) not in current_orders
                ]
            }
            if delete_filter["$or"]:
                result = db[repair_collection].delete_many(delete_filter)
                stats["deleted"] += result.deleted_count
    except CleanupError:
        raise
    except Exception as exc:
        raise CleanupError(str(exc), stats) from exc
    stats["success"] = True
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="清理销售订单看板中不存在的维修故障记录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只统计，不删除（默认）")
    mode.add_argument("--apply", action="store_true", help="执行永久删除")
    parser.add_argument("--confirm", help=f"删除确认字符串：{CONFIRMATION}")
    parser.add_argument("--batch-size", type=int, default=int(env("CLEANUP_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))))
    parser.add_argument("--limit", type=int, help="最多处理的未匹配记录数")
    parser.add_argument("--from-date", type=parse_date, help="维修日期起始 YYYY-MM-DD")
    parser.add_argument("--to-date", type=parse_date, help="维修日期结束 YYYY-MM-DD")
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--source-view", default=DEFAULT_SOURCE_VIEW, help="限定来源视图")
    view.add_argument("--all-source-views", action="store_true", help="包含没有 _source_view 的历史记录")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.apply and args.confirm != CONFIRMATION:
        raise RuntimeError(f"永久删除必须提供 --confirm {CONFIRMATION}")
    if not args.apply and args.confirm:
        raise RuntimeError("--confirm 只能与 --apply 一起使用")
    database = env("MONGODB_DATABASE")
    if not database:
        raise RuntimeError("MONGODB_DATABASE 未配置")
    repair_collection = env("REPAIR_COLLECTION", "repair_records_sap")
    order_collection = env("TARGET_COLLECTION", "sales_orders_sap")
    audit_collection = env("CLEANUP_RUN_COLLECTION", "cleanup_runs")
    cleanup_path = env("CLEANUP_LOCK_PATH", DEFAULT_CLEANUP_LOCK)
    sync_path = env("SYNC_LOCK_PATH", str(ROOT / ".sales_orders_sync.lock"))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid4().hex[:8]
    started_at = datetime.now(timezone.utc)
    client_options = mongo_client_options()
    client = MongoClient(mongo_uri(), **client_options)
    db = client[database]
    mode = "apply" if args.apply else "dry-run"
    audit = {
        "run_id": run_id,
        "started_at": started_at,
        "status": "running",
        "mode": mode,
        "apply": args.apply,
        "repair_collection": repair_collection,
        "order_collection": order_collection,
        "source_view": None if args.all_source_views else args.source_view,
        "all_source_views": args.all_source_views,
        "from_date": args.from_date.isoformat() if args.from_date else None,
        "to_date": args.to_date.isoformat() if args.to_date else None,
    }
    result: dict[str, Any] = dict(audit)
    try:
        db[audit_collection].insert_one(audit)
        with cleanup_locks(cleanup_path, sync_path):
            with mongo_lease_lock(
                db, env("SYNC_DISTRIBUTED_LOCK_NAME", "sales_repair_sync")
            ):
                result = clean_records(db, repair_collection, order_collection, audit_collection, apply=args.apply, batch_size=args.batch_size, limit=args.limit, source_view=args.source_view, all_source_views=args.all_source_views, from_date=args.from_date, to_date=args.to_date, run_id=run_id)
        result.update({"started_at": started_at, "finished_at": datetime.now(timezone.utc), "success": True, "mongo_write_concern": mongo_write_concern_summary(client_options)})
        return result
    except Exception as exc:
        if isinstance(exc, CleanupError):
            result = dict(exc.stats)
        result.update({"finished_at": datetime.now(timezone.utc), "success": False, "error": str(exc)})
        raise
    finally:
        finished_at = datetime.now(timezone.utc)
        result.setdefault("finished_at", finished_at)
        result.setdefault("success", False)
        try:
            db[audit_collection].update_one(
                {"run_id": run_id},
                {"$set": {**result, "status": "success" if result["success"] else "failed"}},
                upsert=False,
            )
        except Exception:
            # Never mask the primary cleanup result with an audit persistence failure.
            logger.exception("写入清理审计记录失败: %s", run_id)
        client.close()


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
