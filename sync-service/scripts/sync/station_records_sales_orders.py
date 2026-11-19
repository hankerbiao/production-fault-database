#!/usr/bin/env python3
"""按销售订单盘中的生产订单同步工位过站记录到独立对比表。"""
from __future__ import annotations
import sys
import hashlib
import json
import time
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from pymongo import MongoClient, UpdateOne
from scripts.sources.hana import hana_view_sync as hana
from scripts.table_pipeline import build_table_parser, print_result
from scripts.sync_runtime import check_leases, resource_lock
from contextlib import nullcontext

def load_production_orders(collection: Any) -> list[str]:
    result: set[str] = set()
    cursor = collection.find({}, {"data.AUFNR": 1, "aufnr": 1})
    try:
        for row in cursor:
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            value = str(data.get("AUFNR") or row.get("aufnr") or "").strip()
            if value: result.add(value)
    finally:
        if hasattr(cursor, "close"): cursor.close()
    return sorted(result)


FIELDS = ("HISTROYID", "SPEC", "SPEC_TIME", "PCODE", "AUFNR")
CHECKPOINT_ID = "station_sales_orders:cursor:v2"
KEY_FIELD = "_station_key_v2"
ORDER_BATCH = 100


def values_for(row: dict[str, Any]) -> tuple[str, ...]:
    values = tuple(str(row.get(field) or "").strip() for field in FIELDS)
    if any(not value for field, value in zip(FIELDS, values) if field != "PCODE"):
        raise ValueError("工位记录缺少必填游标字段")
    return values


def key_for(row: dict[str, Any]) -> str:
    payload = json.dumps(values_for(row), ensure_ascii=False, separators=(",", ":"))
    return "station-v2:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expression(field: str) -> str:
    return f'TRIM(COALESCE("{field}", \'\'))'


def boundary(values: tuple[str, ...], *, greater: bool, inclusive: bool = False) -> tuple[str, list[str]]:
    if len(values) != len(FIELDS):
        raise ValueError("无效工位游标")
    parts, args = [], []
    for i, field in enumerate(FIELDS):
        equal = [f"{expression(f)} = ?" for f in FIELDS[:i]]
        op = (">" if greater else "<") + ("=" if inclusive and i == len(FIELDS) - 1 else "")
        parts.append("(" + " AND ".join(equal + [f"{expression(field)} {op} ?"]) + ")")
        args.extend(values[:i + 1])
    return "(" + " OR ".join(parts) + ")", args


def range_clause(start: tuple[str, ...], end: tuple[str, ...]) -> tuple[str, list[str]]:
    low, low_args = boundary(start, greater=True)
    high, high_args = boundary(end, greater=False, inclusive=True)
    return low + " AND " + high, low_args + high_args


def read_batches(cursor: Any, size: int):
    while rows := cursor.fetchmany(size):
        check_leases()
        yield rows


def sync_incremental(db: Any, spec: Any, *, batch_size: int, dry_run: bool, load_orders: Any) -> dict[str, Any]:
    if batch_size < 1:
        raise ValueError("batch-size 必须大于 0")
    order_batch_size = int(hana.env("STATION_ORDER_BATCH_SIZE", str(ORDER_BATCH)))
    if order_batch_size < 1:
        raise ValueError("STATION_ORDER_BATCH_SIZE 必须大于 0")
    collection = db[spec.collection]
    checkpoints = db[hana.env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints")]
    checkpoint = checkpoints.find_one({"_id": CHECKPOINT_ID})
    start = tuple(checkpoint["cursor"]) if checkpoint and checkpoint.get("cursor") else None
    initialized = start is None
    if initialized:
        latest = collection.find_one({}, {field: 1 for field in FIELDS},
            sort=[(field, -1) for field in FIELDS])
        if not latest:
            raise RuntimeError("工位记录表为空，无法从最新记录确定增量起点")
        start = values_for(latest)
        if not dry_run:
            check_leases(verify=True)
            checkpoints.update_one({"_id": CHECKPOINT_ID}, {"$setOnInsert": {
                "cursor": list(start), "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    result: dict[str, Any] = {"success": False, "mode": "incremental", "run_id": uuid4().hex,
        "range": {"start": start, "end": None}, "candidate_orders": 0, "matched_orders": 0,
        "source_rows": 0, "upserted": 0, "matched": 0, "modified": 0, "initialized": initialized,
        "dry_run": dry_run}
    last_synced = start
    order = ", ".join(expression(f) + " DESC" for f in FIELDS)
    selected = ", ".join(expression(f) for f in FIELDS)
    with hana.hana_connection() as connection:
        cursor = connection.cursor()
        try:
            identity_where = " AND ".join(f"{expression(field)} = ?" for field in FIELDS)
            cursor.execute(f"SELECT {selected} FROM {spec.qualified_name} WHERE {identity_where} LIMIT 2", start)
            anchor_rows = cursor.fetchmany(2)
            if len(anchor_rows) != 1:
                raise RuntimeError(f"HANA 游标起点不存在或不唯一: 找到 {len(anchor_rows)} 条")
            cursor.execute(f"SELECT {selected} FROM {spec.qualified_name} ORDER BY {order} LIMIT 1")
            rows = cursor.fetchmany(1)
            end = tuple(str(v or "").strip() for v in rows[0]) if rows else None
            if end is not None:
                values_for(dict(zip(FIELDS, end)))
            result["range"]["end"] = end
            if start is not None and end is not None and start < end:
                where, params = range_clause(start, end)
                phase_started = time.monotonic()
                cursor.execute(f'SELECT DISTINCT "AUFNR" FROM {spec.qualified_name} WHERE {where}', tuple(params))
                candidates = set()
                for batch in read_batches(cursor, batch_size):
                    candidates.update(str(row[0] or "").strip() for row in batch if str(row[0] or "").strip())
                result["candidate_orders"] = len(candidates)
                orders = set(load_orders(db[hana.env("TARGET_COLLECTION", "sales_orders_sap")]))
                matched = sorted(candidates & orders)
                result["matched_orders"] = len(matched)
                hana.progress_log(f"[station] 候选订单 {len(candidates):,} 个，匹配 {len(matched):,} 个，耗时 {time.monotonic() - phase_started:.1f} 秒")
                columns = ", ".join('"' + f + '"' for f in spec.columns)
                for pos in range(0, len(matched), order_batch_size):
                    batch_orders = matched[pos:pos + order_batch_size]
                    batch_started = time.monotonic()
                    rows_before = result["source_rows"]
                    hana.progress_log(f"[station] 明细批次 {pos // order_batch_size + 1} 开始，订单 {len(batch_orders)} 个")
                    placeholders = ", ".join("?" for _ in batch_orders)
                    ascending = ", ".join(expression(f) for f in FIELDS)
                    cursor.execute(f'SELECT {columns} FROM {spec.qualified_name} WHERE {where} AND {expression("AUFNR")} IN ({placeholders}) ORDER BY {ascending}', tuple(params + batch_orders))
                    previous = None
                    for rows in read_batches(cursor, batch_size):
                        operations = []
                        for values in rows:
                            document = {field: hana.bson_value(value) for field, value in zip(spec.columns, values, strict=True)}
                            identity = values_for(document)
                            if identity == previous:
                                raise RuntimeError(f"HANA 工位记录业务键重复: {identity!r}")
                            previous = identity
                            key = key_for(document)
                            last_synced = max(last_synced, identity)
                            result["source_rows"] += 1
                            if not dry_run:
                                raw_identity_filter = {field: document.get(field) for field in FIELDS}
                                if not raw_identity_filter["PCODE"]:
                                    raw_identity_filter["PCODE"] = {"$in": ["", None]}
                                # HANA text fields are compared with TRIM in SQL. Persist
                                # the same trimmed identity, and fall back to the deterministic
                                # id so rows written by older versions remain retryable.
                                for field in FIELDS:
                                    if isinstance(document.get(field), str):
                                        document[field] = document[field].strip()
                                identity = values_for(document)
                                document.update({KEY_FIELD: key, "_source_view": spec.view_id,
                                    "_sync_run_id": result["run_id"], "_synced_at": datetime.now(timezone.utc),
                                    "AUFNR_NORMALIZED": hana.normalized_station_order(document.get("AUFNR"))})
                                identity_filter = {field: document.get(field) for field in FIELDS}
                                if not identity_filter["PCODE"]:
                                    identity_filter["PCODE"] = {"$in": ["", None]}
                                operations.append(UpdateOne({"$or": [identity_filter, raw_identity_filter, {"_id": hashlib.sha256(key.encode()).hexdigest()}]}, {"$set": document,
                                    "$setOnInsert": {"_source_key": key, "_id": hashlib.sha256(key.encode()).hexdigest()}}, upsert=True))
                        if operations:
                            check_leases(verify=True)
                            written = collection.bulk_write(operations, ordered=False)
                            result["upserted"] += written.upserted_count
                            result["matched"] += written.matched_count
                            result["modified"] += written.modified_count
                        if result["source_rows"] and result["source_rows"] % (batch_size * 100) == 0:
                            hana.progress_log(f"[station] 已读取 {result['source_rows']:,} 条")
                    hana.progress_log(f"[station] 明细批次 {pos // order_batch_size + 1} 完成，读取 {result['source_rows'] - rows_before:,} 条，耗时 {time.monotonic() - batch_started:.1f} 秒")
        finally:
            cursor.close()
    # Never move backwards or replace an established cursor when source is empty.
    new_cursor = last_synced
    if not dry_run and new_cursor is not None:
        check_leases(verify=True)
        checkpoints.update_one({"_id": CHECKPOINT_ID}, {"$set": {"cursor": list(new_cursor),
            "updated_at": datetime.now(timezone.utc), "run_id": result["run_id"]}}, upsert=True)
    result["success"] = True
    return result


def main() -> int:
    args = build_table_parser("按销售订单盘中的生产订单同步工位过站记录").parse_args()
    hana.load_dotenv(); client = None
    try:
        client = MongoClient(hana.mongo_uri(), **hana.mongo_client_options())
        db = client[hana.env("MONGODB_DATABASE")]
        if args.mode != "incremental":
            raise ValueError("此入口仅支持 incremental，避免旧全量流程与新业务键冲突")
        spec = hana.configured_spec("Z_V_ZMES_T_001_SALES_ORDERS")
        with resource_lock(db, spec.collection) if args.apply else nullcontext():
            result = sync_incremental(db, spec, batch_size=args.batch_size, dry_run=not args.apply,
                                      load_orders=load_production_orders)
        return print_result(result)
    except Exception as exc:
        return print_result({"success": False, "error": str(exc)})
    finally:
        if client is not None: client.close()

if __name__ == "__main__": raise SystemExit(main())
