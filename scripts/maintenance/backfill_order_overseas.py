#!/usr/bin/env python3
"""Backfill the indexed overseas flag on existing sales orders."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymongo import MongoClient, UpdateOne
from scripts.sources.hana import hana_view_sync as hana


def backfill(collection: Any, batch_size: int) -> tuple[int, int]:
    last_id = None
    scanned = modified = 0
    while True:
        query: dict[str, Any] = {"is_overseas": {"$exists": False}}
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        rows = list(collection.find(query, {"data.MAKTX": 1}).sort("_id", 1).limit(batch_size))
        if not rows:
            break
        operations = []
        for row in rows:
            data = row.get("data") or {}
            raw_description = data.get("MAKTX")
            description = str(raw_description or "")
            condition: dict[str, Any] = {"_id": row["_id"], "is_overseas": {"$exists": False}}
            condition["data.MAKTX"] = raw_description if "MAKTX" in data else {"$exists": False}
            operations.append(UpdateOne(condition, {"$set": {"is_overseas": "ww" in description.lower()}}))
        result = collection.bulk_write(operations, ordered=False)
        scanned += len(rows)
        modified += result.modified_count
        last_id = rows[-1]["_id"]
        print(f"scanned={scanned} updated={modified}", flush=True)
    return scanned, modified


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write flags and create the lookup index")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    hana.load_dotenv()
    with MongoClient(hana.mongo_uri(), **hana.mongo_client_options()) as client:
        collection = client[hana.env("MONGODB_DATABASE")][hana.env("TARGET_COLLECTION", "sales_orders_sap")]
        missing = collection.count_documents({"is_overseas": {"$exists": False}})
        print(f"Orders missing is_overseas: {missing}")
        if not args.apply:
            print("Dry run: no changes; pass --apply to backfill and create the index.")
            return
        while True:
            scanned, updated = backfill(collection, args.batch_size)
            if not scanned:
                break
            if not updated:
                remaining = collection.count_documents({"is_overseas": {"$exists": False}})
                if remaining:
                    raise RuntimeError("backfill made no progress; retry after checking concurrent writes")
                break
        hana.ensure_index(collection, "is_overseas", name="order_overseas")
        print("Backfill and index complete. The overseas filter can now use exact indexed matching.")


if __name__ == "__main__":
    main()
