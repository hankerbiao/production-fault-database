#!/usr/bin/env python3
"""Backfill station order normalization, safely retrying rows changed by sync."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymongo import MongoClient, UpdateOne
from scripts.sources.hana import hana_view_sync as hana


def backfill(collection, batch_size: int) -> tuple[int, int]:
    last_id = None
    scanned = modified = 0
    while True:
        query = {"AUFNR_NORMALIZED": {"$exists": False}}
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        rows = list(collection.find(query, {"AUFNR": 1}).sort("_id", 1).limit(batch_size))
        if not rows:
            break
        operations = []
        for row in rows:
            condition = {"_id": row["_id"], "AUFNR_NORMALIZED": {"$exists": False}}
            condition["AUFNR"] = row["AUFNR"] if "AUFNR" in row else {"$exists": False}
            operations.append(UpdateOne(condition, {"$set": {"AUFNR_NORMALIZED": hana.normalized_station_order(row.get("AUFNR"))}}))
        result = collection.bulk_write(operations, ordered=False)
        scanned += len(rows)
        modified += result.modified_count
        last_id = rows[-1]["_id"]
        print(f"scanned={scanned} updated={modified}", flush=True)
    return scanned, modified


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write normalized fields and create index")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    hana.load_dotenv()
    with MongoClient(hana.mongo_uri(), **hana.mongo_client_options()) as client:
        collection = client[hana.env("MONGODB_DATABASE")][hana.configured_spec("Z_V_ZMES_T_001_SALES_ORDERS").collection]
        if not args.apply:
            print("Dry run: no changes; pass --apply to backfill and create the index.")
            return
        while True:
            scanned, updated = backfill(collection, args.batch_size)
            if not scanned:
                break
            if not updated:
                raise RuntimeError("backfill made no progress; retry after checking concurrent writes")
        collection.create_index([("AUFNR_NORMALIZED", 1), ("GSTRS", 1)], name="station_order_normalized_gstrs")
        print("Backfill and index complete. Confirm the updated sync is running before enabling the query flag.")


if __name__ == "__main__":
    main()
