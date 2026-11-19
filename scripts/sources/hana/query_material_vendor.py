#!/usr/bin/env python3
"""Return the HANA vendor values for a material number or its final 8 characters."""
from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sources.hana import hana_view_sync as hana


SQL = '''
SELECT DISTINCT "ZMDM_PART_VENDOR"
FROM "_SYS_BIC"."BW_LOCAL.BI/ZSGV_R15_BI"
WHERE "MATNR" = ? OR RIGHT(TRIM("MATNR"), 8) = ?
'''


def stop_on_termination(_signum: int, _frame: Any) -> None:
    raise RuntimeError("HANA material vendor query cancelled")


def main() -> int:
    signal.signal(signal.SIGTERM, stop_on_termination)
    hana.load_dotenv()
    try:
        request = json.load(sys.stdin)
        material_number = request.get("materialNumber") if isinstance(request, dict) else None
        if not isinstance(material_number, str):
            raise ValueError("materialNumber is required")
        material_number = material_number.strip()
        if not material_number or len(material_number) > 40:
            raise ValueError("materialNumber must contain 1 to 40 characters")
        suffix = material_number[-8:]

        vendors: list[str] = []
        seen: set[str] = set()
        with hana.hana_connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(SQL, (material_number, suffix))
                for row in cursor:
                    value = hana.normalize_value(row[0])
                    if value and value not in seen:
                        seen.add(value)
                        vendors.append(value)
            finally:
                cursor.close()

        json.dump({"vendors": vendors}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        print(f"HANA material vendor query failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
