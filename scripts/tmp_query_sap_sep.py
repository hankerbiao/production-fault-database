#!/usr/bin/env python3
"""One-off SAP sales-order query for September 2026."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sync.sync_sales_orders import SOURCES, METHOD, validate_records


START = "2026-09-01"
END = "2026-09-30"
TARGETS = {"41003251", "30235343"}


def headers() -> dict[str, str]:
    today = date.today().strftime("%Y%m%d")
    signature = hashlib.md5(f"sugon{METHOD}{today}sugon".encode()).hexdigest().upper()
    return {"Content-Type": "application/json", "method": METHOD, "sign": signature, "time": today}


def normalized(value: object) -> str:
    raw = str(value or "").strip()
    return raw.lstrip("0") or "0" if raw else ""


def main() -> int:
    print(json.dumps({"method": METHOD, "date_from": START, "date_to": END, "targets": sorted(TARGETS)}, ensure_ascii=False))
    with httpx.Client(timeout=120, trust_env=False) as client:
        for source, url in SOURCES.items():
            for label, prodh in (("with_prodh_00100", ["00100"]), ("without_prodh", None)):
                payload = {"CHDAT": START, "CHDAT_TO": END}
                if prodh is not None:
                    payload["PRODH_LIST"] = prodh
                try:
                    response = client.post(url, json=payload, headers=headers())
                    response.raise_for_status()
                    body = response.json()
                    rows = validate_records(body, source)
                    hits = []
                    for row in rows:
                        values = {
                            normalized(row.get(key))
                            for key in ("AUFNR", "AUFNR_1", "PRODUCTION_ORDER", "PROD_ORDER", "VBELN")
                        }
                        if values & TARGETS:
                            hits.append(row)
                    print(json.dumps({"source": source, "query": label, "http_status": response.status_code, "row_count": len(rows), "hit_count": len(hits), "hits": hits}, ensure_ascii=False, default=str))
                except Exception as exc:
                    print(json.dumps({"source": source, "query": label, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
