#!/usr/bin/env python3
"""Backfill SCS DOA GSTRS after SCS and sales order synchronization."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymongo import MongoClient
from scripts.sales_order_reference import SalesReferences
from scripts.scs_doa_gstrs import backfill
from scripts.sources.hana.hana_view_sync import env, load_dotenv, mongo_client_options, mongo_uri
from scripts.sync_runtime import print_failure, resource_locks, validate_scope_environment


def main() -> int:
    parser = argparse.ArgumentParser(description='按生产订单回填 SCS DOA 计划开始日期')
    parser.add_argument('--apply', action='store_true', help='执行回填；默认只读预览')
    args = parser.parse_args()
    load_dotenv(ROOT / '.env')
    try:
        validate_scope_environment()
        client = MongoClient(mongo_uri(), **mongo_client_options())
        try:
            db = client[env('MONGODB_DATABASE')]
            orders = env('TARGET_COLLECTION', 'sales_orders_sap')
            doa = env('SCS_DOA_COLLECTION', 'scs_doa_records')
            if args.apply:
                with resource_locks(db, orders, doa):
                    result = backfill(db[doa], SalesReferences.load(db[orders]), apply=True)
            else:
                result = backfill(db[doa], SalesReferences.load(db[orders]))
            print(json.dumps({'success': True, 'dry_run': not args.apply, **result}, ensure_ascii=False))
            return 0
        finally:
            client.close()
    except Exception as exc:
        return print_failure(exc)


if __name__ == '__main__':
    raise SystemExit(main())
