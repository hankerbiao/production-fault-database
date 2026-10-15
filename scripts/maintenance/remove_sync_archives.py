#!/usr/bin/env python3
"""Remove retired source-copy archives without touching business or audit data."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymongo import MongoClient

from scripts.sources.hana.hana_view_sync import (
    env, load_dotenv, mongo_client_options, mongo_uri,
)
from scripts.sync_runtime import check_leases, print_failure, resource_lock

DATASETS = {
    "STATION_COLLECTION": "station_records_sap",
    "BOM_COLLECTION": "order_bom_postings_sap",
    "SERIAL_COLLECTION": "serial_bindings_sap",
    "TARGET_COLLECTION": "sales_orders_sap",
    "REPAIR_COLLECTION": "repair_records_sap",
    "SCS_DOA_COLLECTION": "scs_doa_records",
    "SCS_CHANGE_COLLECTION": "scs_change_records",
    "SALES_ORDER_DELIVERY_COLLECTION": "sales_order_delivery_records_sap",
}


def archive_sources() -> dict[str, str]:
    sources = set(DATASETS.values()) | {
        env(variable, default) for variable, default in DATASETS.items()
    }
    archives = {source + "_archive": source for source in sources}
    if sources.intersection(archives):
        raise RuntimeError("Archive collection overlaps an active dataset")
    return archives


def remove_archives(db: Any, *, apply: bool = False) -> dict[str, Any]:
    archives = archive_sources()
    result: dict[str, Any] = {
        "success": False, "mode": "apply" if apply else "dry-run",
        "database": db.name, "collections": {}, "deleted_documents": 0,
    }
    with ExitStack() as stack:
        if apply:
            for source in sorted(set(archives.values())):
                stack.enter_context(resource_lock(db, source))
        targets = sorted(set(db.list_collection_names()).intersection(archives))
        result["collections"] = {
            name: db[name].estimated_document_count() for name in targets
        }
        if apply:
            for name in targets:
                check_leases(verify=True)
                db.drop_collection(name)
                result["deleted_documents"] += result["collections"][name]
            if set(db.list_collection_names()).intersection(archives):
                raise RuntimeError("Source archives still exist after cleanup")
    result["success"] = True
    return result


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if not env("MONGODB_DATABASE"):
            raise RuntimeError("MONGODB_DATABASE is required")
        with MongoClient(mongo_uri(), **mongo_client_options()) as client:
            result = remove_archives(client[env("MONGODB_DATABASE")], apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        return print_failure(exc)


if __name__ == "__main__":
    raise SystemExit(main())
