#!/usr/bin/env python3
"""Reclassify existing SCS DOA and change records as 5000-company data."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pymongo import MongoClient

from scripts.sources.hana.hana_view_sync import (
    env,
    load_dotenv,
    mongo_client_options,
    mongo_uri,
)
from scripts.sources.scs_change_sync import (
    backfill_company,
    load_5000_service_orders,
    service_order_values,
)
from scripts.sources.scs_doa_sync import (
    backfill_5000_company,
    classify_doa_document,
)
from scripts.sync_runtime import resource_lock


def build_refresh_plan(
    database: Any,
    *,
    doa_collection: str,
    change_collection: str,
) -> dict[str, int]:
    doa = database[doa_collection]
    company_service_orders: set[str] = set()
    doa_would_update = 0

    cursor = doa.find(
        {},
        {
            "sales_order": 1,
            "details.fields": 1,
            "details.service_fields": 1,
            "service_uid": 1,
            "is_5000_company": 1,
        },
    )
    try:
        for document in cursor:
            derived = classify_doa_document(document)
            if (
                document.get("sales_order") != derived["sales_order"]
                or document.get("is_5000_company") != derived["is_5000_company"]
            ):
                doa_would_update += 1
            if derived["is_5000_company"]:
                company_service_orders.update(service_order_values(document))
    finally:
        if hasattr(cursor, "close"):
            cursor.close()

    changes = database[change_collection]
    change_would_update = 0
    cursor = changes.find({}, {"so_code": 1, "is_5000_company": 1})
    try:
        for document in cursor:
            service_order = str(document.get("so_code") or "").strip()
            expected = bool(service_order and service_order in company_service_orders)
            if document.get("is_5000_company") != expected:
                change_would_update += 1
    finally:
        if hasattr(cursor, "close"):
            cursor.close()

    return {
        "doa_would_update": doa_would_update,
        "change_would_update": change_would_update,
    }


def refresh(database: Any, *, apply: bool) -> dict[str, Any]:
    doa_collection = env("SCS_DOA_COLLECTION", "scs_doa_records")
    change_collection = env("SCS_CHANGE_COLLECTION", "scs_change_records")

    def run() -> dict[str, Any]:
        plan = build_refresh_plan(
            database,
            doa_collection=doa_collection,
            change_collection=change_collection,
        )
        result: dict[str, Any] = {
            "success": True,
            "dry_run": not apply,
            "classification_rule": "sales_order_prefix_XNC",
            "doa_collection": doa_collection,
            "change_collection": change_collection,
            **plan,
        }
        if not apply:
            return result

        result["doa_updated"] = backfill_5000_company(
            database,
            doa_collection,
        )
        company_service_orders = load_5000_service_orders(database, doa_collection)
        result["change_updated"] = backfill_company(
            database[change_collection],
            company_service_orders,
        )
        result["company_service_orders"] = len(company_service_orders)
        return result

    if not apply:
        return run()

    with ExitStack() as locks:
        for collection in sorted({doa_collection, change_collection}):
            locks.enter_context(resource_lock(database, collection))
        return run()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按 DOA 销售订单号 XNC 前缀重算现有 SCS 5000 公司标记"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="写入更新；默认仅预览将更新的数量",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    client = MongoClient(mongo_uri(), **mongo_client_options())
    try:
        database = client[env("MONGODB_DATABASE")]
        result = refresh(database, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps(
            {"success": False, "error": str(exc)},
            ensure_ascii=False,
            sort_keys=True,
        ))
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
