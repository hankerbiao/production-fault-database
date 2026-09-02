#!/usr/bin/env python3
from hana_view_sync import VIEW_SPECS, run_cli


if __name__ == "__main__":
    raise SystemExit(run_cli(VIEW_SPECS["Z_V_ZMES_T_001"]))
