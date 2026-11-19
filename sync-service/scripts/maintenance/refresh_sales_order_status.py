#!/usr/bin/env python3
"""Refresh statuses of existing sales orders without importing or deleting orders."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sync import sync_sales_orders as sales


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 SAP 重新读取并刷新已有销售订单的 ZSTAT")
    parser.add_argument("--start-date", type=sales.parse_date, default=sales.SALES_SCOPE_START)
    parser.add_argument("--end-date", type=sales.parse_date, default=sales.current_date())
    parser.add_argument("--apply", action="store_true", help="写入变更；默认仅预览")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prodh-list", nargs="+")
    group.add_argument("--all-prodh", action="store_true")
    return parser


def main() -> int:
    sales.load_dotenv(PROJECT_ROOT / ".env")
    sales.install_graceful_shutdown()
    args = build_parser().parse_args()
    if args.start_date > args.end_date:
        raise SystemExit("--start-date 不能晚于 --end-date")
    if not sales.env("MONGODB_DATABASE"):
        raise SystemExit("MONGODB_DATABASE 未配置")
    run_id = "sales-status:" + datetime.now(timezone.utc).isoformat()
    try:
        with sales.MongoClient(sales.mongo_uri(), **sales.mongo_client_options()) as client:
            db = client[sales.env("MONGODB_DATABASE")]
            lock = sales.resource_lock(
                db, sales.env("TARGET_COLLECTION", "sales_orders_sap"),
            ) if args.apply else nullcontext()
            with lock, sales.httpx.Client(
                timeout=float(sales.env("HTTP_TIMEOUT", str(sales.DEFAULT_TIMEOUT))),
                trust_env=False,
            ) as http:
                result = sales.refresh_order_statuses(
                    db, http, start=args.start_date, end=args.end_date,
                    prodh=sales.configured_prodh(args), apply=args.apply, run_id=run_id,
                )
                if args.apply:
                    sales.check_leases(verify=True)
                    db[sales.env("SYNC_RUN_COLLECTION", "sync_runs")].insert_one({
                        **result, "dataset": "sales_status", "mode": "status_refresh",
                        "created_at": datetime.now(timezone.utc),
                    })
            print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
            return 0 if result["success"] else 1
    except Exception as exc:
        return sales.print_failure(exc)


if __name__ == "__main__":
    raise SystemExit(main())
