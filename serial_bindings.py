#!/usr/bin/env python3
"""Synchronize and deduplicate SAP serial bindings."""
from scripts.table_pipeline import build_table_parser, print_result, run_hana_table


def main() -> int:
    parser = build_table_parser("同步序列号绑定表，并清理完整业务键的精确重复数据")
    args = parser.parse_args()
    try:
        return print_result(run_hana_table("ZSGV_ZPP_SERNOLIST", "serial", args))
    except Exception as exc:
        return print_result({"success": False, "error": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main())
