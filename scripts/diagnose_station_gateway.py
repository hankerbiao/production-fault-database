#!/usr/bin/env python3
"""Compare station gateway connectivity without exposing credentials or records."""
import argparse
import csv
import io
import json
import os
import platform
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter


def request(url, timeout):
    started = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, urllib.error.URLError) as exc:
        return {"error": str(exc), "seconds": round(time.monotonic() - started, 3)}
    with response:
        body = response.read()
        result = {"status": response.status, "contentType": response.headers.get("Content-Type", ""),
                  "bytes": len(body), "seconds": round(time.monotonic() - started, 3)}
        if response.status != 200:
            result["error"] = body[:2000].decode("utf-8", "replace")
        elif "tab-separated-values" in result["contentType"]:
            rows = csv.DictReader(io.StringIO(body.decode("utf-8-sig")), delimiter="\t")
            models = Counter()
            count = 0
            for row in rows:
                count += 1
                models[row.get("MAKTX_TH", "")] += 1
            result.update(rows=count, distinctModels=len(models), emptyModels=models.get("", 0))
        else:
            result["body"] = body[:1000].decode("utf-8", "replace")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="http://127.0.0.1:18080")
    parser.add_argument("--hana-host", default=os.getenv("HANA_ADDRESS", "10.2.101.32"))
    parser.add_argument("--hana-port", type=int, default=int(os.getenv("HANA_PORT", "30015")))
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    result = {"host": socket.gethostname(), "platform": platform.platform(),
              "gateway": args.gateway, "hana": f"{args.hana_host}:{args.hana_port}",
              "dateFrom": args.date_from, "dateTo": args.date_to}
    started = time.monotonic()
    try:
        with socket.create_connection((args.hana_host, args.hana_port), timeout=5) as conn:
            result["tcp"] = {"connected": True, "source": conn.getsockname()[0],
                             "seconds": round(time.monotonic()-started, 3)}
    except OSError as exc:
        result["tcp"] = {"connected": False, "error": str(exc), "seconds": round(time.monotonic()-started, 3)}
    base = args.gateway.rstrip("/")
    result["health"] = request(base + "/api/health", min(args.timeout, 10))
    query = urllib.parse.urlencode({"dateFrom": args.date_from, "dateTo": args.date_to, "timeField": "planned", "sn": ""})
    result["station"] = request(base + "/api/views/Z_V_ZMES_T_001/stream?" + query, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
