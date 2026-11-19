#!/usr/bin/env python3
"""Run the local station sync and report a failed scheduled attempt."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sources.hana.hana_view_sync import env, load_dotenv
from scripts.sync.syncctl import notify_sync_failure


def main() -> int:
    load_dotenv()
    run_id = "local-cron-" + uuid4().hex
    error = ""
    try:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/sync/station_records_sales_orders.py"), "--mode", "incremental", "--apply"],
            cwd=ROOT, capture_output=True, text=True,
            timeout=float(env("SYNC_TASK_TIMEOUT_SECONDS", "21600")),
        )
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr, flush=True)
        if completed.stdout:
            print(completed.stdout, end="", flush=True)
        if completed.returncode == 0:
            return 0
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            error = str(result.get("error") or f"退出码 {completed.returncode}")
        except (ValueError, IndexError, AttributeError):
            error = f"退出码 {completed.returncode}: {completed.stderr[-500:]}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    notification = notify_sync_failure(run_id, "工位过站（本机定时）", error)
    print(json.dumps({"success": False, "error": error, "notification": notification}, ensure_ascii=False), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
