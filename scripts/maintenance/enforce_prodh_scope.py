#!/usr/bin/env python3
"""Preview or audit and migrate legacy data to the fixed 00100 scope."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pymongo import MongoClient

from scripts.sales_order_reference import SalesReferences, host_scopes, sales_lines
from scripts.scs_classification import enqueue_change, service_order_values
from scripts.sources.scs_doa_sync import extract_sales_order
from scripts.sync.sync_sales_orders import aggregate
from scripts.sources.hana.hana_view_sync import env, load_dotenv, mongo_client_options, mongo_uri
from scripts.sync_runtime import (
    check_leases, clear_scope_pending, product_scope, required_prodh_list, resource_locks,
    save_scope_pending, print_failure,
)


def migrate(db: Any, collections: dict[str, str], *, apply: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"success": True, "dry_run": not apply, "prodh_list": required_prodh_list(),
                              "migration_complete": True}
    run_id = uuid4().hex
    refs = SalesReferences.load(db[collections.get("sales", env("TARGET_COLLECTION", "sales_orders_sap"))])
    hosts = host_scopes(db[collections["station"]], refs) if "station" in collections else {}
    services: dict[str, set[str]] = {}
    if "doa" in collections:
        for document in db[collections["doa"]].find({}):
            scope = refs.resolve(sales=extract_sales_order(document.get("details", {})) or document.get("sales_order"))
            for key in service_order_values(document):
                services.setdefault(key, set()).add(scope)
    for dataset, name in collections.items():
        stats = {"scanned": 0, "would_delete": 0, "would_trim": 0, "scope_pending": 0,
                 "verified": 0, "deleted": 0, "updated": 0}
        result[dataset] = stats
        for document in db[name].find({}):
            stats["scanned"] += 1
            fields: dict[str, Any] = {"_prodh_scope": "00100", "_scope_rule_version": 1}
            if dataset == "sales":
                fields = {}
                lines = list(sales_lines(document))
                scopes = [product_scope({**document, **row}) for row in lines]
                if not scopes or "pending" in scopes:
                    scope = "pending"
                else:
                    kept = [row for row, state in zip(lines, scopes) if state == "in_scope"]
                    scope = "in_scope" if kept else "out_of_scope"
                    if kept:
                        fields.update({key: value for key, value in aggregate(
                            document["source"], kept, document.get("last_synced_at") or datetime.now(timezone.utc),
                        )[0][0].items() if key != "_id"})
                        stats["would_trim"] += len(kept) != len(lines)
            elif dataset == "doa":
                scope = refs.resolve(sales=extract_sales_order(document.get("details", {})) or document.get("sales_order"))
            elif dataset == "changes":
                values = services.get(document.get("so_code"), set())
                scope = next(iter(values)) if len(values) == 1 else "pending"
            else:
                reference = {**document, **(document.get("_source_orders") or {})} if dataset == "repair" else document
                scope = refs.row_scope(reference)
                if dataset == "repair":
                    if scope == "pending" and not reference.get("AUFNR") and not reference.get("VBELN"):
                        values = hosts.get((str(document.get("MANDT") or ""), str(document.get("PCODE") or "")), set())
                        if len(values) == 1:
                            scope = next(iter(values))
                    if scope == "in_scope" and refs.row_scope(document) != "in_scope":
                        fields.update(AUFNR=reference.get("AUFNR", ""), VBELN=reference.get("VBELN", ""), GSTRS="")
            if scope == "pending":
                stats["scope_pending"] += 1
                result["migration_complete"] = False
                if apply:
                    save_scope_pending(db, f"migration:{name}", str(document["_id"]), document)
                continue
            stats["would_delete"] += scope == "out_of_scope"
            stats["verified"] += scope == "in_scope"
            if not apply:
                continue
            check_leases(verify=True)
            db["sync_scope_migrations"].insert_one({
                "_id": f"{run_id}:{name}:{document['_id']}", "run_id": run_id,
                "collection": name, "scope": scope, "before": document,
                "created_at": datetime.now(timezone.utc),
            })
            if dataset == "doa":
                enqueue_change(db, document, {**document, **fields} if scope == "in_scope" else {})
            check_leases(verify=True)
            if scope == "out_of_scope":
                deleted = db[name].delete_many(document).deleted_count
                if deleted != 1:
                    raise RuntimeError("迁移目标发生变化，停止清理")
                stats["deleted"] += deleted
            else:
                written = db[name].update_one(document, {"$set": fields})
                if written.matched_count != 1:
                    raise RuntimeError("迁移目标发生变化，停止写入")
                stats["updated"] += written.modified_count
            clear_scope_pending(db, f"migration:{name}", [str(document["_id"])])
        if apply:
            stats["scope_backlog"] = db["sync_scope_pending"].count_documents({"dataset": f"migration:{name}"})
            result["migration_complete"] &= stats["scope_backlog"] == 0
        result[dataset] = stats
    return result


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="写前备份，并执行已核实的迁移；默认预览")
    args = parser.parse_args()
    collections = {
        "sales": env("TARGET_COLLECTION", "sales_orders_sap"),
        "station": env("STATION_COLLECTION", "station_records_sap"),
        "bom": env("BOM_COLLECTION", "order_bom_postings_sap"),
        "serial": env("SERIAL_COLLECTION", "serial_bindings_sap"),
        "repair": env("REPAIR_COLLECTION", "repair_records_sap"),
        "doa": env("SCS_DOA_COLLECTION", "scs_doa_records"),
        "changes": env("SCS_CHANGE_COLLECTION", "scs_change_records"),
    }
    client = MongoClient(mongo_uri(), **mongo_client_options())
    try:
        db = client[env("MONGODB_DATABASE")]
        if args.apply:
            with resource_locks(db, *collections.values()):
                result = migrate(db, collections, apply=True)
        else:
            result = migrate(db, collections, apply=False)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        return print_failure(exc)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
