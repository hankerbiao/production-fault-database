#!/usr/bin/env python3
from hana_view_sync import VIEW_SPECS, run_cli


if __name__ == "__main__":
    raise SystemExit(run_cli(VIEW_SPECS["ZSGV_ZPP_SERNOLIST"]))
