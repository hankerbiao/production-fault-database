#!/usr/bin/env python3
"""Synchronize and clean SAP order BOM postings."""
from scripts.table_pipeline import build_table_parser, print_result, run_hana_table


def main() -> int:
    parser = build_table_parser("同步订单 BOM 过账明细，并仅保留 CPX=5000公司 数据")
    args = parser.parse_args()
    try:
        return print_result(run_hana_table("ZSGV_ZSD124", "bom", args))
    except Exception as exc:
        return print_result({"success": False, "error": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main())
