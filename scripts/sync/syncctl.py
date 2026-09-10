#!/usr/bin/env python3
"""Persistent, dependency-aware orchestration for production data syncs.

This module is deliberately the only pipeline runner.  Both cron and the Go
gateway invoke it, so task ordering, retries and durable run state cannot
drift between operational entry points.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from scripts.sources.hana.hana_view_sync import env, load_dotenv, mongo_client_options, mongo_uri


RUN_COLLECTION = "sync_orchestrator_runs"
LOCK_COLLECTION = "sync_locks"
LOCK_NAME = "sync-orchestrator"
DEFAULT_RETRY_ATTEMPTS = 3


@dataclass(frozen=True)
class Task:
    id: str
    label: str
    script: str
    dependencies: tuple[str, ...] = ()
    priority: int = 0
    full_supported: bool = True

    def command(self, python: str, mode: str, start_date: str | None, end_date: str | None) -> list[str]:
        path = str(ROOT / self.script)
        args = [python, path]
        if self.id == "sales_orders":
            if mode == "full":
                args.extend(["--full", "--start-date", start_date or ""])
                if end_date:
                    args.extend(["--end-date", end_date])
        elif self.id in {"station_records", "order_bom_postings", "serial_bindings"}:
            args.extend(["--mode", mode, "--apply"])
            if mode == "full":
                args.extend(["--start-date", start_date or ""])
            if end_date:
                args.extend(["--end-date", end_date])
        elif self.id == "repair_records":
            args.extend(["--mode", mode, "--apply", "--no-progress", "--log-level", "ERROR"])
            if mode == "full":
                args.extend(["--start-date", start_date or ""])
            if end_date:
                args.extend(["--sync-end-date", end_date])
        else:  # SCS commands share the same flag convention.
            if mode == "full":
                args.extend(["--full", "--start-date", start_date or ""])
            if end_date:
                # SCS endpoints currently derive their end bound server-side.
                # Keep this option out of their command contracts until needed.
                pass
        return args


TASKS: tuple[Task, ...] = (
    Task("sales_orders", "销售订单", "scripts/sync/sync_sales_orders.py", priority=10),
    Task("station_records", "工位记录", "scripts/sync/station_records.py", ("sales_orders",), 20),
    Task("repair_records", "维修故障", "scripts/sync/增量同步和清洗维修故障记录.py", ("sales_orders", "station_records"), 30),
    Task("order_bom_postings", "订单 BOM 过账", "scripts/sync/order_bom_postings.py", ("sales_orders",), 40),
    Task("serial_bindings", "序列号绑定", "scripts/sync/serial_bindings.py", priority=50),
    Task("scs_doa", "SCS DOA", "scripts/sources/scs_doa_sync.py", ("sales_orders",), 60),
    Task("scs_change", "SCS 换上换下", "scripts/sources/scs_change_sync.py", ("scs_doa",), 70),
)
TASK_BY_ID = {task.id: task for task in TASKS}


class SyncError(RuntimeError):
    def __init__(self, message: str, *, code: str = "sync_failed", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


def resolve_tasks(requested: Iterable[str] | None) -> list[Task]:
    """Return the stable, dependency-complete task plan for a request."""
    requested_ids = list(requested or TASK_BY_ID)
    unknown = sorted(set(requested_ids) - TASK_BY_ID.keys())
    if unknown:
        raise SyncError("未知同步任务: " + ", ".join(unknown), code="invalid_task")
    resolved: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in resolved:
            return
        task = TASK_BY_ID[task_id]
        for dependency in task.dependencies:
            visit(dependency)
        resolved.add(task_id)

    for task_id in requested_ids:
        visit(task_id)
    return sorted((TASK_BY_ID[task_id] for task_id in resolved), key=lambda item: item.priority)


def parse_iso_date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise SyncError(f"{name} 必须为 YYYY-MM-DD", code="invalid_request") from exc


def validate_request(mode: str, requested: Iterable[str] | None, start_date: str | None, end_date: str | None) -> tuple[list[Task], str | None, str | None]:
    if mode not in {"incremental", "full"}:
        raise SyncError("mode 必须为 incremental 或 full", code="invalid_request")
    start = parse_iso_date(start_date, "startDate")
    end = parse_iso_date(end_date, "endDate")
    if mode == "full" and not start:
        raise SyncError("全量同步必须提供 startDate", code="invalid_request")
    if mode == "incremental" and start:
        raise SyncError("增量同步不接受 startDate", code="invalid_request")
    if start and end and start > end:
        raise SyncError("startDate 不能晚于 endDate", code="invalid_request")
    tasks = resolve_tasks(requested)
    unsupported = [task.id for task in tasks if mode == "full" and not task.full_supported]
    if unsupported:
        raise SyncError("任务不支持全量同步: " + ", ".join(unsupported), code="unsupported_mode")
    return tasks, start, end or date.today().isoformat()


class Lease:
    """Mongo lease that remains valid across long full synchronization runs."""
    def __init__(self, db: Any, owner: str, ttl_seconds: int = 120):
        self.collection = db[LOCK_COLLECTION]
        self.owner = owner
        self.ttl_seconds = max(ttl_seconds, 30)
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            document = self.collection.find_one_and_update(
            {"_id": LOCK_NAME, "$or": [{"expires_at": {"$lte": now}}, {"owner": self.owner}]},
            {"$set": {"owner": self.owner, "acquired_at": now, "expires_at": datetime.fromtimestamp(now.timestamp() + self.ttl_seconds, timezone.utc)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            document = None
        if not document or document.get("owner") != self.owner:
            raise SyncError("已有同步任务运行中", code="already_running")
        self._thread = threading.Thread(target=self._renew, daemon=True)
        self._thread.start()

    def _renew(self) -> None:
        while not self._stop.wait(self.ttl_seconds / 3):
            now = datetime.now(timezone.utc)
            result = self.collection.update_one(
                {"_id": LOCK_NAME, "owner": self.owner},
                {"$set": {"expires_at": datetime.fromtimestamp(now.timestamp() + self.ttl_seconds, timezone.utc)}},
            )
            if result.matched_count != 1:
                self._lost.set()
                return

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self.collection.delete_one({"_id": LOCK_NAME, "owner": self.owner})


class RunStore:
    def __init__(self, db: Any):
        self.collection = db[RUN_COLLECTION]
        self.collection.create_index([("state", 1), ("started_at", -1)])
        self.collection.create_index("heartbeat_at")

    def abandon_stale(self, cutoff: datetime) -> None:
        self.collection.update_many(
            {"state": "running", "heartbeat_at": {"$lt": cutoff}},
            {"$set": {"state": "abandoned", "finished_at": datetime.now(timezone.utc), "message": "编排器心跳超时"}},
        )

    def create(self, run_id: str, *, mode: str, requested: list[str], tasks: list[Task], start_date: str | None, end_date: str | None, source: str) -> None:
        now = datetime.now(timezone.utc)
        self.collection.insert_one({
            "_id": run_id, "state": "running", "mode": mode, "source": source,
            "requested_task_ids": requested, "resolved_task_ids": [task.id for task in tasks],
            "start_date": start_date, "end_date": end_date, "started_at": now, "heartbeat_at": now,
            "stages": [{"task_id": task.id, "label": task.label, "state": "pending", "attempts": 0} for task in tasks],
        })

    def heartbeat(self, run_id: str) -> None:
        self.collection.update_one({"_id": run_id, "state": "running"}, {"$set": {"heartbeat_at": datetime.now(timezone.utc)}})

    def stage(self, run_id: str, task_id: str, **fields: Any) -> None:
        fields = {f"stages.$.{key}": value for key, value in fields.items()}
        fields["heartbeat_at"] = datetime.now(timezone.utc)
        self.collection.update_one({"_id": run_id, "stages.task_id": task_id}, {"$set": fields})

    def finish(self, run_id: str, state: str, message: str = "") -> None:
        self.collection.update_one({"_id": run_id}, {"$set": {"state": state, "message": message, "finished_at": datetime.now(timezone.utc), "heartbeat_at": datetime.now(timezone.utc)}})

    def run(self, run_id: str) -> dict[str, Any] | None:
        return self.collection.find_one({"_id": run_id})


def json_result(output: str, task: Task, mode: str) -> dict[str, Any]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            value.setdefault("taskId", task.id)
            value.setdefault("mode", mode)
            return value
    raise SyncError(f"{task.id} 输出不是有效 JSON", code="invalid_script_output")


def is_retryable(result: dict[str, Any], returncode: int) -> bool:
    error = result.get("error")
    if isinstance(error, dict):
        return bool(error.get("retryable"))
    text = str(error or "").lower()
    return returncode != 0 and any(token in text for token in ("timeout", "timed out", "connection", "temporar", "503", "502", "429"))


class Orchestrator:
    def __init__(self, db: Any, *, python: str | None = None, attempts: int = DEFAULT_RETRY_ATTEMPTS):
        self.store = RunStore(db)
        self.db = db
        self.python = python or os.getenv("SYNC_PYTHON", sys.executable)
        self.attempts = max(1, attempts)

    def execute(self, *, mode: str, requested: list[str] | None = None, start_date: str | None = None, end_date: str | None = None, source: str = "cli", run_id: str | None = None) -> dict[str, Any]:
        tasks, start, end = validate_request(mode, requested, start_date, end_date)
        run_id = run_id or uuid4().hex
        lease = Lease(self.db, run_id, int(os.getenv("SYNC_ORCHESTRATOR_LEASE_SECONDS", "120")))
        self.store.abandon_stale(datetime.fromtimestamp(time.time() - int(os.getenv("SYNC_ORCHESTRATOR_STALE_SECONDS", "300")), timezone.utc))
        lease.acquire()
        self.store.create(run_id, mode=mode, requested=list(requested or [task.id for task in TASKS]), tasks=tasks, start_date=start, end_date=end, source=source)
        try:
            for task in tasks:
                if lease.lost:
                    raise SyncError("同步租约已丢失", code="lease_lost")
                result = self._execute_task(run_id, task, mode, start, end)
                if not result.get("success"):
                    error = result.get("error") or {"code": "task_failed", "message": f"{task.id} 同步失败", "retryable": False}
                    self.store.finish(run_id, "failed", str(error.get("message", error) if isinstance(error, dict) else error))
                    return self.store.run(run_id) or {"_id": run_id, "state": "failed"}
            self.store.finish(run_id, "success", "同步完成")
            return self.store.run(run_id) or {"_id": run_id, "state": "success"}
        except SyncError as exc:
            self.store.finish(run_id, "failed", str(exc))
            return self.store.run(run_id) or {"_id": run_id, "state": "failed", "error": exc.payload()}
        finally:
            lease.close()

    def _execute_task(self, run_id: str, task: Task, mode: str, start: str | None, end: str | None) -> dict[str, Any]:
        for attempt in range(1, self.attempts + 1):
            self.store.stage(run_id, task.id, state="running", attempts=attempt, started_at=datetime.now(timezone.utc), finished_at=None, error=None)
            command = task.command(self.python, mode, start, end)
            completed = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            try:
                summary = json_result(completed.stdout, task, mode)
            except SyncError as exc:
                summary = {"success": False, "taskId": task.id, "mode": mode, "error": exc.payload()}
            success = completed.returncode == 0 and summary.get("success") is not False
            if success:
                summary["success"] = True
                self.store.stage(run_id, task.id, state="success", attempts=attempt, finished_at=datetime.now(timezone.utc), summary=summary, error=None)
                return summary
            error = summary.get("error") or {"code": "script_failed", "message": completed.stderr.strip() or f"退出码 {completed.returncode}", "retryable": False}
            if not isinstance(error, dict):
                error = {"code": "script_failed", "message": str(error), "retryable": is_retryable(summary, completed.returncode)}
            retryable = bool(error.get("retryable")) or is_retryable(summary, completed.returncode)
            error["retryable"] = retryable
            self.store.stage(run_id, task.id, state="retrying" if retryable and attempt < self.attempts else "failed", attempts=attempt, finished_at=datetime.now(timezone.utc), summary=summary, error=error)
            if not retryable or attempt == self.attempts:
                return {"success": False, "error": error, "summary": summary}
            time.sleep(min(8, 2 ** (attempt - 1)))
        raise AssertionError("unreachable")


def serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    return value


def task_catalog() -> list[dict[str, Any]]:
    return [{"id": task.id, "label": task.label, "dependencies": list(task.dependencies), "fullSupported": task.full_supported} for task in TASKS]


def main() -> int:
    parser = argparse.ArgumentParser(description="产线数据同步编排器")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    run_parser.add_argument("--tasks", nargs="*", help="任务 ID；省略则执行全部任务")
    run_parser.add_argument("--start-date")
    run_parser.add_argument("--end-date")
    run_parser.add_argument("--source", default="cli")
    run_parser.add_argument("--run-id")
    subparsers.add_parser("tasks")
    args = parser.parse_args()
    if args.command == "tasks":
        print(json.dumps({"tasks": task_catalog()}, ensure_ascii=False))
        return 0
    try:
        load_dotenv(ROOT / ".env")
        client = MongoClient(mongo_uri(), **mongo_client_options())
        try:
            result = Orchestrator(client[env("MONGODB_DATABASE")]).execute(mode=args.mode, requested=args.tasks, start_date=args.start_date, end_date=args.end_date, source=args.source, run_id=args.run_id)
        finally:
            client.close()
        print(json.dumps(serialize(result), ensure_ascii=False, default=str))
        return 0 if result.get("state") == "success" else 1
    except SyncError as exc:
        print(json.dumps({"success": False, "error": exc.payload()}, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"success": False, "error": {"code": "orchestrator_error", "message": str(exc), "retryable": False}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
