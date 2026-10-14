"""Shared resource leases, guarded cleanup, and synchronization contracts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from uuid import uuid4

from bson import BSON
from pymongo import ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError


_active_leases: set["Lease"] = set()
_lease_guard = threading.Lock()


def order_key(value: Any) -> str:
    value = "" if value is None else str(value).strip()
    return value.lstrip("0") or "0" if value.isdigit() else value


class Lease:
    def __init__(
        self, db: Any, name: str, owner: str | None = None, ttl_seconds: int = 120,
        heartbeat: Callable[[], None] | None = None, collection: str = "sync_locks",
    ) -> None:
        self.collection = db[collection]
        self.name = name
        self.owner = owner or uuid4().hex
        self.ttl_seconds = max(30, ttl_seconds)
        self.heartbeat = heartbeat
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def acquire(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            document = self.collection.find_one_and_update(
                {"_id": self.name, "$or": [{"expires_at": {"$lte": now}}, {"owner": self.owner}]},
                {"$set": {"owner": self.owner, "acquired_at": now,
                          "expires_at": now + timedelta(seconds=self.ttl_seconds)}},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError as exc:
            raise RuntimeError(f"已有同步任务运行中: {self.name}") from exc
        if not document or document.get("owner") != self.owner:
            raise RuntimeError(f"已有同步任务运行中: {self.name}")
        with _lease_guard:
            _active_leases.add(self)
        self._thread = threading.Thread(target=self._renew, daemon=True)
        self._thread.start()

    def _renew(self) -> None:
        while not self._stop.wait(min(10, self.ttl_seconds / 3)):
            try:
                now = datetime.now(timezone.utc)
                result = self.collection.update_one(
                    {"_id": self.name, "owner": self.owner, "expires_at": {"$gt": now}},
                    {"$set": {"expires_at": now + timedelta(seconds=self.ttl_seconds)}},
                )
                if result.matched_count != 1:
                    self._lost.set()
                    return
                if self.heartbeat:
                    self.heartbeat()
            except Exception:
                self._lost.set()
                return

    def assert_owned(self, *, verify: bool = False) -> None:
        if verify and not self.lost:
            try:
                if not self.collection.find_one({
                    "_id": self.name, "owner": self.owner,
                    "expires_at": {"$gt": datetime.now(timezone.utc)},
                }):
                    self._lost.set()
            except Exception:
                self._lost.set()
        if self.lost:
            raise RuntimeError(f"同步租约已丢失: {self.name}")

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with _lease_guard:
            _active_leases.discard(self)
        try:
            self.collection.delete_one({"_id": self.name, "owner": self.owner})
        except Exception:
            # An unavailable database cannot prevent process shutdown; the lease expires.
            pass


def check_leases(*, verify: bool = False) -> None:
    with _lease_guard:
        leases = tuple(_active_leases)
    for lease in leases:
        lease.assert_owned(verify=verify)


@contextmanager
def resource_lock(db: Any, collection: str, *, ttl_seconds: int | None = None) -> Iterator[Lease]:
    ttl_seconds = ttl_seconds if ttl_seconds is not None else int(os.getenv("SYNC_MONGO_LOCK_TTL_SECONDS", "120"))
    if ttl_seconds < 1:
        raise ValueError("SYNC_MONGO_LOCK_TTL_SECONDS 必须大于 0")
    lease = Lease(db, f"dataset:{collection}", ttl_seconds=ttl_seconds,
                  collection=os.getenv("SYNC_LOCK_COLLECTION", "sync_locks"))
    lease.acquire()
    try:
        yield lease
    finally:
        lease.close()


def validate_cleanup(existing: int, stale: int, source_rows: int, allow: bool = False) -> None:
    if allow:
        return
    if existing and not source_rows:
        raise RuntimeError("Empty source snapshot; refusing cleanup")
    if stale > 100 and stale > existing / 2:
        raise RuntimeError("Cleanup exceeds 100 rows and half the local scope; refusing cleanup")


def archive_cleanup(
    db: Any, collection: str, scope: dict[str, Any], stale_query: dict[str, Any],
    run_id: str, source_rows: int, *, allow_large_cleanup: bool = False,
    batch_size: int = 1000, reason: str = "not_in_validated_snapshot",
) -> int:
    target = db[collection]
    validate_cleanup(
        target.count_documents(scope), target.count_documents(stale_query),
        source_rows, allow_large_cleanup,
    )
    archive = db[collection + "_archive"]
    deleted = 0
    documents: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal deleted
        if not documents:
            return
        check_leases(verify=True)
        archive.bulk_write([
            UpdateOne({"_id": f"{run_id}:{doc['_id']}"}, {"$setOnInsert": {
                "document": doc, "run_id": run_id,
                "archived_at": datetime.now(timezone.utc), "scope": scope,
                "reason": reason,
            }}, upsert=True) for doc in documents
        ], ordered=False)
        check_leases(verify=True)
        deleted += target.delete_many({
            **stale_query, "_id": {"$in": [doc["_id"] for doc in documents]},
        }).deleted_count
        documents.clear()

    cursor = target.find(stale_query)
    try:
        for document in cursor:
            documents.append(document)
            if len(documents) >= batch_size:
                flush()
        flush()
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    return deleted


def error_payload(exc: Exception) -> dict[str, Any]:
    if hasattr(exc, "payload") and callable(exc.payload):
        return exc.payload()
    message = str(exc)
    retryable = any(word in message.lower() for word in (
        "timeout", "timed out", "connection", "temporar", "503", "502", "429",
    ))
    return {"code": type(exc).__name__, "message": message, "retryable": retryable}


def print_failure(exc: Exception) -> int:
    print(json.dumps({"success": False, "error": error_payload(exc)}, ensure_ascii=False))
    return 1


def publish_version(db: Any, dataset: str, run_id: str) -> None:
    check_leases(verify=True)
    db["sync_dataset_versions"].update_one(
        {"_id": dataset},
        {"$set": {"run_id": run_id, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


class Snapshot:
    """Validate keyed source rows on disk before changing the target dataset."""
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="sync-snapshot-")
        self.connection = sqlite3.connect(self.directory.name + "/rows.sqlite")
        self.connection.execute("CREATE TABLE rows (key TEXT PRIMARY KEY, hash TEXT, payload BLOB)")
        self.duplicates = 0
        self.count = 0

    def add(self, key: str, document: dict[str, Any]) -> None:
        payload = BSON.encode(document)
        digest = hashlib.sha256(payload).hexdigest()
        existing = self.connection.execute("SELECT hash FROM rows WHERE key = ?", (key,)).fetchone()
        if existing:
            self.duplicates += 1
            if existing[0] != digest:
                raise RuntimeError(f"Conflicting source key: {key}")
            return
        self.connection.execute("INSERT INTO rows VALUES (?, ?, ?)", (key, digest, payload))
        self.count += 1

    def contains(self, key: str) -> bool:
        return self.connection.execute("SELECT 1 FROM rows WHERE key = ?", (key,)).fetchone() is not None

    def __iter__(self) -> Iterator[tuple[str, dict[str, Any]]]:
        self.connection.commit()
        for key, payload in self.connection.execute("SELECT key, payload FROM rows ORDER BY rowid"):
            yield key, BSON(payload).decode()

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.connection.close()
        self.directory.cleanup()

    def find(self, query: dict[str, Any] | None = None, projection: Any = None):
        """Read-only collection interface for planning cleanup on staged rows."""
        for _key, document in self:
            if matches_snapshot(document, query or {}):
                yield document

    def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for _document in self.find(query))


def matches_snapshot(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for field, condition in query.items():
        if field in {"$and", "$or"}:
            matched = [matches_snapshot(document, part) for part in condition]
            if not (all(matched) if field == "$and" else any(matched)):
                return False
            continue
        actual = document.get(field)
        if not isinstance(condition, dict):
            if actual != condition:
                return False
            continue
        for operator, expected in condition.items():
            if operator == "$exists":
                valid = (field in document) == expected
            elif operator == "$regex":
                valid = isinstance(actual, str) and re.search(expected, actual)
            elif operator == "$ne":
                valid = actual != expected
            elif operator == "$in":
                valid = actual in expected
            elif operator in {"$gte", "$lte", "$lt"}:
                valid = actual is not None and (
                    actual >= expected if operator == "$gte" else
                    actual <= expected if operator == "$lte" else actual < expected)
            else:
                raise ValueError(f"Unsupported snapshot predicate: {operator}")
            if not valid:
                return False
    return True


def validate_snapshot_cleanup(target: Any, scope: dict[str, Any], snapshot: Snapshot, *, allow: bool = False) -> None:
    existing = stale = 0
    cursor = target.find(scope, {"_source_key": 1})
    try:
        for document in cursor:
            existing += 1
            stale += not snapshot.contains(str(document.get("_source_key") or ""))
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    validate_cleanup(existing, stale, snapshot.count, allow)
