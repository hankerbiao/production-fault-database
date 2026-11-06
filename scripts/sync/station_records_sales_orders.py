#!/usr/bin/env python3
"""按销售订单盘中的生产订单同步工位过站记录到独立对比表。"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from pymongo import MongoClient
from scripts.sources.hana import hana_view_sync as hana
from scripts.table_pipeline import build_table_parser, print_result

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

def main() -> int:
    args = build_table_parser("按销售订单盘中的生产订单同步工位过站记录").parse_args()
    hana.load_dotenv(); client = None
    try:
        client = MongoClient(hana.mongo_uri(), **hana.mongo_client_options())
        db = client[hana.env("MONGODB_DATABASE")]
        orders = load_production_orders(db[hana.env("TARGET_COLLECTION", "sales_orders_sap")])
        result = hana.sync_view(hana.configured_spec("Z_V_ZMES_T_001_SALES_ORDERS"), mode=args.mode,
            start_date=args.start_date.strftime("%Y%m%d") if args.start_date else None,
            end_date=args.end_date.strftime("%Y%m%d"), batch_size=args.batch_size,
            lookback_days=args.lookback_days, dry_run=not args.apply, production_orders=orders,
            planned_start_from="20250101", planned_start_to="20261231")
        result["sales_order_production_orders"] = len(orders)
        return print_result(result)
    except Exception as exc:
        return print_result({"success": False, "error": str(exc)})
    finally:
        if client is not None: client.close()

if __name__ == "__main__": raise SystemExit(main())
