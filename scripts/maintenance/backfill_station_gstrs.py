#!/usr/bin/env python3
"""Restore missing station records for an exact model, operation and planned-date range."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymongo import MongoClient, UpdateOne

from scripts.sources.hana.hana_view_sync import (
    bson_value, configured_spec, env, hana_connection, load_dotenv,
    mongo_client_options, mongo_uri, process_lock, source_key,
)
from scripts.sync_runtime import Snapshot, batches, print_failure, resource_locks, verified_bulk_write


def stage_source(snapshot, spec, model: str, operation: str, start: date, end: date, batch_size: int) -> int:
    columns = ", ".join(f'"{name}"' for name in spec.columns)
    sql = (
        f"SELECT {columns} FROM {spec.qualified_name} "
        'WHERE "MAKTX_TH" = ? AND "SPEC" = ? '
        'AND "GSTRS" >= TO_DATE(?, \'YYYY-MM-DD\') '
        'AND "GSTRS" < TO_DATE(?, \'YYYY-MM-DD\')'
    )
    count = 0
    with hana_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, (model, operation, start.isoformat(), (end + timedelta(days=1)).isoformat()))
            while rows := cursor.fetchmany(batch_size):
                for values in rows:
                    row = dict(zip(spec.columns, values, strict=True))
                    snapshot.add(source_key(row, spec), {field: bson_value(value) for field, value in row.items()})
                    count += 1
        finally:
            cursor.close()
    return count


def insert_missing(snapshot, collection, view_id: str, *, apply: bool, batch_size: int, run_id: str) -> dict:
    result = {"source_unique_rows": snapshot.count, "already_present": 0, "missing": 0,
              "inserted": 0, "matched_on_write": 0}
    for batch in batches(snapshot, batch_size):
        existing = {doc["_source_key"] for doc in collection.find(
            {"_source_key": {"$in": [key for key, _ in batch]}}, {"_source_key": 1},
        )}
        result["already_present"] += len(existing)
        missing = [(key, row) for key, row in batch if key not in existing]
        result["missing"] += len(missing)
        if not apply or not missing:
            continue
        now = datetime.now(timezone.utc)
        operations = [UpdateOne(
            {"_source_key": key},
            {"$setOnInsert": {**row, "_id": hashlib.sha256(key.encode("utf-8")).hexdigest(),
                              "_source_view": view_id, "_source_key": key,
                              "_sync_run_id": run_id, "_synced_at": now}},
            upsert=True,
        ) for key, row in missing]
        written = verified_bulk_write(collection, operations)
        result["inserted"] += written.upserted_count
        result["matched_on_write"] += written.matched_count
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="按机型、工序及 GSTRS 日期补齐缺失工位记录")
    parser.add_argument("--model", required=True)
    parser.add_argument("--station", required=True)
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--to-date", type=date.fromisoformat, required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--apply", action="store_true", help="写入缺失记录；默认只读预览")
    args = parser.parse_args()
    if not args.model.strip() or not args.station.strip() or args.from_date > args.to_date or args.batch_size < 1:
        parser.error("机型、工序及日期范围必须有效，批量大小必须大于零")
    load_dotenv(ROOT / ".env")
    try:
        spec = configured_spec("Z_V_ZMES_T_001")
        client = MongoClient(mongo_uri(), **mongo_client_options())
        try:
            db = client[env("MONGODB_DATABASE")]
            collection = db[spec.collection]
            with ExitStack() as stack:
                if args.apply:
                    stack.enter_context(process_lock(env("SYNC_LOCK_PATH", "/tmp/line-fault-table-sync.lock")))
                    stack.enter_context(resource_locks(db, spec.collection))
                    indexes = collection.index_information()
                    if not any(info.get("unique") and info.get("key") == [("_source_key", 1)] for info in indexes.values()):
                        raise RuntimeError("工位集合缺少唯一源记录键索引，拒绝补数")
                snapshot = stack.enter_context(Snapshot())
                source_rows = stage_source(snapshot, spec, args.model.strip(), args.station.strip(),
                                           args.from_date, args.to_date, args.batch_size)
                run_id = "station-backfill-" + uuid4().hex
                result = insert_missing(snapshot, collection, spec.view_id, apply=args.apply,
                                        batch_size=args.batch_size, run_id=run_id)
                print(json.dumps({"success": True, "dry_run": not args.apply, "run_id": run_id, "source_rows": source_rows,
                                  "duplicate_source_rows": snapshot.duplicates, **result}, ensure_ascii=False))
                return 0
        finally:
            client.close()
    except Exception as exc:
        return print_failure(exc)


if __name__ == "__main__":
    raise SystemExit(main())
