#!/usr/bin/env python3
"""清理订单 BOM 过账，并补全工位记录明细中的订单字段.

订单 BOM 过账只保留 ``CPX=5000公司`` 的数据。工位记录以
``sales_orders_sap`` 的确定性订单映射补全字段；无法映射的原始记录会保留。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pymongo import MongoClient, UpdateOne

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sources.hana.hana_view_sync import (  # noqa: E402
    mongo_client_options,
    mongo_lease_lock,
    mongo_uri,
    mongo_write_concern_summary,
    process_lock,
)
from scripts.sync.sync_sales_orders import env, load_dotenv  # noqa: E402


CONFIRMATION = "DELETE-ORDER-BOARD-INVALID"
DEFAULT_BATCH_SIZE = 500
DEFAULT_LOCK = "/tmp/line-fault-order-boards-cleanup.lock"
BOM_COMPANY = "5000公司"


def log(message: str) -> None:
    """Write operational progress to stderr so stdout remains machine-readable JSON."""
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class BoardSpec:
    name: str
    collection_env: str
    default_collection: str
    source_view: str
    production_field: str
    sales_field: str
    date_field: str


SPECS = {
    "bom": BoardSpec("bom", "BOM_COLLECTION", "order_bom_postings_sap", "ZSGV_ZSD124", "AUFNR_1", "VBELN_EX", "BUDAT_MKPF"),
    "station": BoardSpec("station", "STATION_COLLECTION", "station_records_sap", "Z_V_ZMES_T_001", "AUFNR", "KDAUF", "ACTUAL_START_TIME"),
}


def normalize(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_order(value: Any) -> str:
    value = normalize(value)
    return value.lstrip("0") or "0" if value else ""


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD: {value}") from exc


def base_filter(spec: BoardSpec, from_date: date | None, to_date: date | None, sync_run_id: str | None = None) -> dict[str, Any]:
    query: dict[str, Any] = {"_source_view": spec.source_view}
    if sync_run_id:
        query["_sync_run_id"] = sync_run_id
    if from_date or to_date:
        window: dict[str, str] = {}
        if from_date:
            window["$gte"] = from_date.isoformat()
        if to_date:
            window["$lt"] = (to_date + timedelta(days=1)).isoformat()
        query[spec.date_field] = window
    return query


def sales_order_index(
    collection: Any,
) -> tuple[set[str], dict[str, str], dict[str, str], set[str], set[str]]:
    """Build order-board lookups, retaining only unambiguous mappings for backfill."""
    productions: set[str] = set()
    sales_orders: set[str] = set()
    production_candidates: dict[str, set[str]] = {}
    sales_candidates: dict[str, set[str]] = {}
    log("开始建立销售订单看板索引")
    cursor = collection.find({}, {"data.AUFNR": 1, "data.VBELN": 1, "aufnr": 1})
    scanned = 0
    try:
        for document in cursor:
            scanned += 1
            data = document.get("data") if isinstance(document.get("data"), dict) else {}
            production = normalize_order(data.get("AUFNR") or document.get("aufnr"))
            sales = normalize(data.get("VBELN"))
            if not production:
                continue
            productions.add(production)
            if sales:
                sales_key = normalize_order(sales)
                source_production = normalize(data.get("AUFNR") or document.get("aufnr"))
                sales_orders.add(sales_key)
                production_candidates.setdefault(production, set()).add(sales)
                sales_candidates.setdefault(sales_key, set()).add(source_production)
            if scanned % 100000 == 0:
                log(f"销售订单看板索引已扫描 {scanned:,} 条")
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    by_production = {
        production: next(iter(candidates))
        for production, candidates in production_candidates.items()
        if len(candidates) == 1
    }
    sales_to_production = {
        sales: next(iter(candidates))
        for sales, candidates in sales_candidates.items()
        if len(candidates) == 1
    }
    ambiguous_productions = set(production_candidates) - set(by_production)
    ambiguous_sales = set(sales_candidates) - set(sales_to_production)
    log(
        f"销售订单看板索引完成：扫描 {scanned:,} 条，生产订单 {len(productions):,} 个，"
        f"销售订单 {len(sales_orders):,} 个，生产订单多映射 {len(ambiguous_productions):,} 个，"
        f"销售订单多映射 {len(ambiguous_sales):,} 个"
    )
    return sales_orders, by_production, sales_to_production, ambiguous_productions, ambiguous_sales


def process_bom_cpx_cleanup(
    db: Any,
    spec: BoardSpec,
    collection_name: str,
    order_collection_name: str,
    *,
    apply: bool,
    batch_size: int,
    limit: int | None,
    from_date: date | None,
    to_date: date | None,
    sync_run_id: str | None,
    progress: bool,
) -> dict[str, Any]:
    """Keep only BOM postings whose CPX value identifies the 5000 company."""
    query = base_filter(spec, from_date, to_date, sync_run_id)
    collection = db[collection_name]
    total = collection.count_documents(query) if hasattr(collection, "count_documents") else None
    log(f"[bom] CPX 清理扫描范围：{total:,} 条" if total is not None else "[bom] CPX 清理扫描范围：未知")
    stats: dict[str, Any] = {
        "success": False, "board": spec.name, "collection": collection_name,
        "order_collection": order_collection_name, "source_view": spec.source_view,
        "cpx_target": BOM_COMPANY, "cpx_5000_company": 0, "non_5000_cpx": 0,
        "empty_cpx": 0, "scanned": 0, "delete_candidates": 0,
        "updated": 0, "deleted": 0,
    }
    deletes: list[dict[str, Any]] = []
    cursor = collection.find(query, {"_id": 1, "CPX": 1})
    try:
        for row in cursor:
            if limit is not None and stats["scanned"] >= limit:
                break
            stats["scanned"] += 1
            cpx = normalize(row.get("CPX"))
            if cpx == BOM_COMPANY:
                stats["cpx_5000_company"] += 1
            else:
                if not cpx:
                    stats["empty_cpx"] += 1
                stats["non_5000_cpx"] += 1
                stats["delete_candidates"] += 1
                deletes.append(row)
            if progress and (stats["scanned"] == 1 or stats["scanned"] % 100 == 0):
                suffix = f"/{total}" if total else ""
                print(f"\r处理bom: {stats['scanned']}{suffix}", end="", file=sys.stderr, flush=True)
            if stats["scanned"] % 100000 == 0:
                log(
                    f"[bom] 已扫描 {stats['scanned']:,} 条，保留 5000公司 {stats['cpx_5000_company']:,} 条，"
                    f"删除候选 {stats['delete_candidates']:,} 条"
                )
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    if progress:
        print(file=sys.stderr, flush=True)

    log(
        f"[bom] CPX 扫描完成：扫描 {stats['scanned']:,} 条，保留 5000公司 {stats['cpx_5000_company']:,} 条，"
        f"空 CPX {stats['empty_cpx']:,} 条，非 5000 CPX 删除候选 {stats['delete_candidates']:,} 条"
    )
    if apply:
        log(f"[bom] 开始执行 CPX 清理，共 {len(deletes):,} 个删除动作，批大小 {batch_size}")
        for start in range(0, len(deletes), batch_size):
            batch = deletes[start:start + batch_size]
            identities = [
                {"_id": row.get("_id"), "_source_view": spec.source_view, "CPX": row.get("CPX")}
                for row in batch
            ]
            stats["deleted"] += collection.delete_many({"$or": identities}).deleted_count
            log(f"[bom] 已执行 {min(start + len(batch), len(deletes)):,}/{len(deletes):,} 个删除动作（删除 {stats['deleted']:,}）")
        log(f"[bom] CPX 清理完成：删除 {stats['deleted']:,} 条")
    else:
        log("[bom] dry-run，不执行数据库变更")
    stats["success"] = True
    return stats


def process_board(
    db: Any,
    spec: BoardSpec,
    collection_name: str,
    order_collection_name: str,
    *,
    apply: bool,
    batch_size: int,
    limit: int | None,
    from_date: date | None,
    to_date: date | None,
    sync_run_id: str | None = None,
    progress: bool = True,
) -> dict[str, Any]:
    if batch_size < 1:
        raise ValueError("--batch-size 必须大于 0")
    if limit is not None and limit < 1:
        raise ValueError("--limit 必须大于 0")
    if from_date and to_date and from_date > to_date:
        raise ValueError("--from-date 不能晚于 --to-date")

    log(f"[{spec.name}] 开始处理集合 {collection_name}，来源视图 {spec.source_view}")
    if spec.name == "bom":
        return process_bom_cpx_cleanup(
            db, spec, collection_name, order_collection_name, apply=apply,
            batch_size=batch_size, limit=limit, from_date=from_date, to_date=to_date,
            progress=progress, sync_run_id=sync_run_id,
        )
    board_sales, by_production, sales_to_production, ambiguous_productions, ambiguous_sales = sales_order_index(db[order_collection_name])
    if not board_sales:
        raise RuntimeError(f"{order_collection_name} 中没有有效销售订单，拒绝处理")
    query = base_filter(spec, from_date, to_date, sync_run_id)
    total = db[collection_name].count_documents(query) if hasattr(db[collection_name], "count_documents") else None
    log(f"[{spec.name}] 候选扫描范围：{total:,} 条" if total is not None else f"[{spec.name}] 候选扫描范围：未知")
    stats: dict[str, Any] = {
        "success": False, "board": spec.name, "collection": collection_name,
        "order_collection": order_collection_name, "source_view": spec.source_view,
        "scanned": 0, "empty_production_order": 0, "empty_sales_order": 0,
        "both_empty": 0, "filled_production_order": 0, "filled_sales_order": 0,
        "matched_sales_orders": 0, "missing_board_sales": 0,
        "unresolved_rows": 0, "unresolved_production_orders": 0,
        "ambiguous_production_mappings": 0, "ambiguous_sales_mappings": 0,
        "delete_candidates": 0, "updated": 0, "deleted": 0,
    }
    actions: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []
    unresolved_productions: set[str] = set()
    update_candidates = 0
    cursor = db[collection_name].find(query, {"_id": 1, spec.production_field: 1, spec.sales_field: 1})
    try:
        for row in cursor:
            if limit is not None and stats["scanned"] >= limit:
                break
            stats["scanned"] += 1
            production_raw = normalize(row.get(spec.production_field))
            sales_raw = normalize(row.get(spec.sales_field))
            production = normalize_order(production_raw)
            sales_key = normalize_order(sales_raw)
            fields: dict[str, str] = {}
            if not production:
                stats["empty_production_order"] += 1
            if not sales_raw:
                stats["empty_sales_order"] += 1
            if not production and not sales_raw:
                stats["both_empty"] += 1

            if not production and sales_key:
                if sales_key in sales_to_production:
                    fields[spec.production_field] = sales_to_production[sales_key]
                    production = normalize_order(fields[spec.production_field])
                    stats["filled_production_order"] += 1
                elif sales_key in ambiguous_sales:
                    stats["ambiguous_sales_mappings"] += 1
            if not sales_raw and production:
                if production in by_production:
                    fields[spec.sales_field] = by_production[production]
                    sales_raw = by_production[production]
                    sales_key = normalize_order(sales_raw)
                    stats["filled_sales_order"] += 1
                else:
                    stats["unresolved_rows"] += 1
                    unresolved_productions.add(production)
                    if production in ambiguous_productions:
                        stats["ambiguous_production_mappings"] += 1

            if sales_raw:
                if sales_key not in board_sales:
                    stats["delete_candidates"] += 1
                    actions.append(("delete", row, None))
                    continue
                stats["matched_sales_orders"] += 1
                if fields:
                    actions.append(("update", row, fields))
                    update_candidates += 1
            elif fields:
                actions.append(("update", row, fields))
                update_candidates += 1
            else:
                stats["missing_board_sales"] += 1
            if progress and (stats["scanned"] == 1 or stats["scanned"] % 100 == 0):
                suffix = f"/{total}" if total else ""
                print(f"\r处理{spec.name}: {stats['scanned']}{suffix}", end="", file=sys.stderr, flush=True)
            if stats["scanned"] % 100000 == 0:
                log(f"[{spec.name}] 已扫描 {stats['scanned']:,} 条，补全候选 {update_candidates:,} 条，删除候选 {stats['delete_candidates']:,} 条")
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    if progress:
        print(file=sys.stderr, flush=True)

    stats["unresolved_production_orders"] = len(unresolved_productions)
    log(
        f"[{spec.name}] 扫描完成：扫描 {stats['scanned']:,} 条，空生产订单 {stats['empty_production_order']:,} 条，"
        f"空销售订单 {stats['empty_sales_order']:,} 条，补生产订单 {stats['filled_production_order']:,} 条，"
        f"补销售订单 {stats['filled_sales_order']:,} 条，待解析 {stats['unresolved_rows']:,} 条（生产订单 "
        f"{stats['unresolved_production_orders']:,} 个），多映射 {stats['ambiguous_production_mappings']:,} 条，"
        f"更新候选 {update_candidates:,} 条，删除候选 {stats['delete_candidates']:,} 条"
    )

    if apply:
        log(f"[{spec.name}] 开始执行批量变更，共 {len(actions):,} 个动作，批大小 {batch_size}")
        collection = db[collection_name]
        for start in range(0, len(actions), batch_size):
            batch = actions[start:start + batch_size]
            updates: list[Any] = []
            deletes: list[dict[str, Any]] = []
            for kind, row, fields in batch:
                identity = {"_id": row.get("_id"), "_source_view": spec.source_view,
                            spec.production_field: row.get(spec.production_field),
                            spec.sales_field: row.get(spec.sales_field)}
                if kind == "update":
                    updates.append(UpdateOne(identity, {"$set": fields or {}}))
                else:
                    deletes.append(identity)
            if updates:
                stats["updated"] += collection.bulk_write(updates, ordered=False).modified_count
            if deletes:
                stats["deleted"] += collection.delete_many({"$or": deletes}).deleted_count
            log(f"[{spec.name}] 已执行 {min(start + len(batch), len(actions)):,}/{len(actions):,} 个动作（更新 {stats['updated']:,}，删除 {stats['deleted']:,}）")
        log(f"[{spec.name}] 批量变更完成：更新 {stats['updated']:,} 条，删除 {stats['deleted']:,} 条")
    else:
        log(f"[{spec.name}] dry-run，不执行数据库变更")
    stats["success"] = True
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按 CPX 清理订单 BOM 过账，并补全、清理工位记录订单字段")
    parser.add_argument("--board", choices=("all", "bom", "station"), default="all", help="处理看板，默认 all")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只统计，不更新或删除（默认）")
    mode.add_argument("--apply", action="store_true", help="执行回填和删除")
    parser.add_argument("--confirm", help=f"删除确认字符串：{CONFIRMATION}")
    parser.add_argument("--batch-size", type=int, default=int(env("ORDER_BOARD_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))))
    parser.add_argument("--limit", type=int, help="每个看板最多扫描记录数")
    parser.add_argument("--from-date", type=parse_date, help="开始日期；不传则不限制日期")
    parser.add_argument("--to-date", type=parse_date, help="结束日期；不传则不限制日期")
    parser.add_argument("--no-progress", action="store_true")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.apply and not getattr(args, "internal", False) and args.confirm != CONFIRMATION:
        raise RuntimeError(f"永久变更必须提供 --confirm {CONFIRMATION}")
    if not args.apply and args.confirm:
        raise RuntimeError("--confirm 只能与 --apply 一起使用")
    database = env("MONGODB_DATABASE")
    if not database:
        raise RuntimeError("MONGODB_DATABASE 未配置")
    order_collection = env("TARGET_COLLECTION", "sales_orders_sap")
    boards = list(SPECS) if args.board == "all" else [args.board]
    options = mongo_client_options()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid4().hex[:8]
    client = MongoClient(mongo_uri(), **options)
    result: dict[str, Any] = {"success": False, "run_id": run_id, "mode": "apply" if args.apply else "dry-run", "boards": {}}
    audit_collection = env("ORDER_BOARD_CLEANUP_RUN_COLLECTION", "cleanup_runs")
    lock_path = env("ORDER_BOARD_CLEANUP_LOCK_PATH", DEFAULT_LOCK)
    sync_path = env("SYNC_LOCK_PATH", str(ROOT / ".sales_orders_sync.lock"))
    log(f"任务启动：run_id={run_id}，模式={result['mode']}，看板={','.join(boards)}，批大小={args.batch_size}，limit={args.limit}")
    try:
        db = client[database]
        with process_lock(lock_path):
            with process_lock(sync_path):
                with mongo_lease_lock(db, env("SYNC_DISTRIBUTED_LOCK_NAME", "sales_repair_sync")):
                    for board in boards:
                        spec = SPECS[board]
                        collection = env(spec.collection_env, spec.default_collection)
                        result["boards"][board] = process_board(db, spec, collection, order_collection, apply=args.apply, batch_size=args.batch_size, limit=args.limit, from_date=args.from_date, to_date=args.to_date, sync_run_id=getattr(args, "sync_run_id", None), progress=not args.no_progress)
        result.update({"success": True, "finished_at": datetime.now(timezone.utc), "mongo_write_concern": mongo_write_concern_summary(options)})
        log(f"任务完成：run_id={run_id}，处理看板={','.join(boards)}")
        return result
    finally:
        try:
            client[database][audit_collection].insert_one({**result, "status": "success" if result.get("success") else "failed", "created_at": datetime.now(timezone.utc)})
        except Exception:
            pass
        client.close()
