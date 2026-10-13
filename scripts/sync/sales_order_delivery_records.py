#!/usr/bin/env python3
"""Synchronize the fixed 2026 scope of the SAP sales-order delivery view."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymongo import MongoClient

from scripts.sources.hana.hana_view_sync import (
    VIEW_SPECS,
    env,
    install_graceful_shutdown,
    load_dotenv,
    mongo_client_options,
    mongo_lease_lock,
    mongo_uri,
    process_lock,
    sync_view,
)


VIEW_ID = "ZSGV_ZSD46N_SO_KK"
START_DATE = "20260101"
END_DATE = "20261231"


def main() -> int:
    load_dotenv()
    install_graceful_shutdown()
    parser = argparse.ArgumentParser(description=f"同步 {VIEW_ID} 的 2026 年数据")
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--dry-run", action="store_true", help="仅读取 HANA 并校验数据，不写入 MongoDB")
    parser.add_argument("--apply", action="store_true", help="写入 MongoDB")
    parser.add_argument("--batch-size", type=int, default=int(env("SYNC_BATCH_SIZE", "1000")))
    parser.add_argument("--lookback-days", type=int, default=int(env("SYNC_LOOKBACK_DAYS", "7")))
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error("--dry-run 和 --apply 不能同时指定")
    if args.batch_size < 1 or args.lookback_days < 0:
        parser.error("--batch-size 必须大于 0，--lookback-days 不能为负数")

    lock_client = None
    try:
        with process_lock(env("SYNC_LOCK_PATH", "/tmp/line-fault-sales-order-delivery-sync.lock")):
            if args.apply:
                options = mongo_client_options()
                lock_client = MongoClient(mongo_uri(), **options)
                lock = env("SYNC_DISTRIBUTED_LOCK_NAME", "sales_order_delivery_records")
                with mongo_lease_lock(lock_client[env("MONGODB_DATABASE")], lock):
                    if args.mode == "full":
                        preflight = sync_view(
                            VIEW_SPECS[VIEW_ID],
                            mode="full",
                            start_date=START_DATE,
                            end_date=END_DATE,
                            batch_size=args.batch_size,
                            lookback_days=args.lookback_days,
                            dry_run=True,
                            validate_source_keys=True,
                        )
                        if not preflight.get("success"):
                            print(json.dumps({"success": False, "view_id": VIEW_ID, "preflight": preflight}, ensure_ascii=False, default=str, sort_keys=True))
                            return 1
                    result = sync_view(
                        VIEW_SPECS[VIEW_ID],
                        mode=args.mode,
                        start_date=START_DATE,
                        end_date=END_DATE,
                        batch_size=args.batch_size,
                        lookback_days=args.lookback_days,
                        dry_run=False,
                        validate_source_keys=False,
                    )
            else:
                result = sync_view(
                    VIEW_SPECS[VIEW_ID],
                    mode=args.mode,
                    start_date=START_DATE,
                    end_date=END_DATE,
                    batch_size=args.batch_size,
                    lookback_days=args.lookback_days,
                    dry_run=True,
                    validate_source_keys=True,
                )
        print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
        return 0 if result.get("success") else 1
    except Exception as exc:
        print(json.dumps({"success": False, "view_id": VIEW_ID, "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if lock_client is not None:
            lock_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
