#!/usr/bin/env python3
"""Remove the retired delivery dataset and its derived collections."""
from __future__ import annotations

import argparse
import json
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymongo import MongoClient

from scripts.sources.hana.hana_view_sync import (
    env, load_dotenv, mongo_client_options, mongo_uri, process_lock,
)
from scripts.sync_runtime import check_leases, print_failure, resource_lock

VIEW_ID = "ZSGV_ZSD46N_SO_KK"
SOURCE_COLLECTION = "sales_order_delivery_records_sap"
SUMMARY_COLLECTION = "sales_order_delivery_orders_summary_sap"
META_COLLECTION = "sales_order_delivery_summary_meta"


def collection_names(names: list[str], source: str) -> list[str]:
    exact = {source, source + "_archive", SUMMARY_COLLECTION, META_COLLECTION}
    build = re.compile(re.escape(SUMMARY_COLLECTION) + r"_build_\d+\Z")
    return sorted(name for name in names if name in exact or build.fullmatch(name))


def remove_dataset(db: Any, *, apply: bool = False) -> dict[str, Any]:
    source = env("SALES_ORDER_DELIVERY_COLLECTION", SOURCE_COLLECTION)
    if source != SOURCE_COLLECTION:
        raise RuntimeError("Custom delivery collection requires manual ownership verification")
    metadata = {
        env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints"): {
            "_id": "hana_views:" + VIEW_ID,
        },
        env("SYNC_RUN_COLLECTION", "sync_runs"): {"view_id": VIEW_ID},
        "sync_dataset_versions": {"_id": VIEW_ID},
    }
    result: dict[str, Any] = {
        "success": False, "mode": "apply" if apply else "dry-run",
        "database": db.name, "collections": {}, "metadata": {},
    }
    with ExitStack() as stack:
        if apply:
            stack.enter_context(process_lock(env(
                "SYNC_LOCK_PATH", "/tmp/line-fault-sales-order-delivery-sync.lock",
            )))
            stack.enter_context(resource_lock(db, source))
        names = db.list_collection_names()
        targets = collection_names(names, source)
        result["collections"] = {
            name: db[name].estimated_document_count() for name in targets
        }
        for name, query in metadata.items():
            if name in names:
                result["metadata"][name] = db[name].count_documents(query)
        if apply:
            for name in targets:
                check_leases(verify=True)
                db.drop_collection(name)
            for name, query in metadata.items():
                if name in names:
                    check_leases(verify=True)
                    db[name].delete_many(query)
            remaining = collection_names(db.list_collection_names(), source)
            if remaining or any(
                db[name].count_documents(query)
                for name, query in metadata.items() if name in names
            ):
                raise RuntimeError("Retired delivery dataset still exists after cleanup")
    result["success"] = True
    return result


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not env("MONGODB_DATABASE"):
        return print_failure(RuntimeError("MONGODB_DATABASE is required"))
    try:
        with MongoClient(mongo_uri(), **mongo_client_options()) as client:
            result = remove_dataset(client[env("MONGODB_DATABASE")], apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        return print_failure(exc)


if __name__ == "__main__":
    raise SystemExit(main())
