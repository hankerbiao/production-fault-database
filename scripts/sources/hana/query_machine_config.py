#!/usr/bin/env python3
"""Fetch a machine's station and BOM rows directly from HANA."""
import json
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.sources.hana import hana_view_sync as hana

STATION_SQL = '''SELECT "AUFNR", "GSTRS" FROM "_SYS_BIC"."BW_LOCAL.ZTRRI/Z_V_ZMES_T_001" WHERE "PCODE" = ? AND "AUFNR" IS NOT NULL AND TRIM("AUFNR") <> '' ORDER BY "GSTRS" DESC'''
BOM_SQL = '''SELECT "MATNR", "MATNR_BI", "MAKTX", "MATKL", "WGBEZ", "LGORT", "PSMNG", "MENGE", "MENGE_A", "BWART" FROM "_SYS_BIC"."BW_LOCAL.BI/ZSGV_ZSD124_BI" WHERE "AUFNR_1" = ?'''


def stop_on_termination(_signum, _frame):
    raise RuntimeError("HANA machine config query cancelled")


def main():
    signal.signal(signal.SIGTERM, stop_on_termination)
    hana.load_dotenv()
    request = json.load(sys.stdin)
    sn = request['sn']
    with hana.hana_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(STATION_SQL, (sn,))
            order = ''
            for row in cursor.fetchall():
                candidate = str(row[0] or '').strip().lstrip('0')
                if candidate:
                    order = candidate
                    break
        finally:
            cursor.close()
    rows = []
    if order:
        with hana.hana_connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(BOM_SQL, (order.zfill(12),))
                columns = [item[0] for item in cursor.description]
                for row in cursor.fetchall():
                    rows.append({key: hana.normalize_value(value) for key, value in zip(columns, row)})
            finally:
                cursor.close()
    json.dump({'order': order, 'rows': rows}, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
