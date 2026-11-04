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
from itertools import islice
from uuid import uuid4

from bson import BSON
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


_active_leases: set["Lease"] = set()
_lease_guard = threading.Lock()
REQUIRED_PRODH = ("00100",)
SCOPE_VERSION = 1


def required_prodh_list() -> list[str]:
    return list(REQUIRED_PRODH)


def validate_prodh(values: Any) -> list[str]:
    if values != required_prodh_list():
        raise ValueError('所有同步数据必须限定 PRODH_LIST=["00100"]')
    return required_prodh_list()


def validate_scope_environment() -> None:
    raw = os.getenv("SOURCE_PRODH_LIST", "00100")
    validate_prodh([part.strip() for part in raw.replace("，", ",").split(",") if part.strip()])


def product_scope(row: Any) -> str:
    value = str(row.get("PRODH") or "").strip()
    if value:
        return "in_scope" if value == "00100" else "out_of_scope"
    return "in_scope" if row.get("_prodh_scope") == "00100" else "pending"


def source_hash(document: Any) -> str:
    return hashlib.sha256(json.dumps(
        document, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"),
    ).encode()).hexdigest()


def batches(rows: Any, size: int) -> Iterator[list[Any]]:
    if size < 1:
        raise ValueError("batch size must be greater than zero")
    iterator = iter(rows)
    while batch := list(islice(iterator, size)):
        yield batch


def verified_bulk_write(collection: Any, operations: list[Any]) -> Any:
    check_leases(verify=True)
    result = collection.bulk_write(operations, ordered=False)
    if result.matched_count + result.upserted_count != len(operations):
        raise RuntimeError("批量写入数量不足，拒绝推进检查点")
    return result


def checkpoint_id(dataset: str, source: str) -> str:
    return f"{dataset}:{source}"


def sales_scope_checkpoint_id(dataset: str, source: str) -> str:
    return f"{checkpoint_id(dataset, source)}:prodh-00100:v{SCOPE_VERSION}"


def save_scope_pending(db: Any, dataset: str, key: str, row: dict[str, Any]) -> None:
    check_leases(verify=True)
    db["sync_scope_pending"].update_one(
        {"_id": f"{dataset}:{key}"},
        {"$set": {"dataset": dataset, "key": key, "payload": row,
                  "updated_at": datetime.now(timezone.utc), "prodh_list": required_prodh_list()}},
        upsert=True,
    )


def clear_scope_pending(db: Any, dataset: str, keys: Any) -> None:
    for batch in batches(keys, 1000):
        check_leases(verify=True)
        db["sync_scope_pending"].delete_many({"_id": {"$in": [f"{dataset}:{key}" for key in batch]}})


@contextmanager
def resource_locks(db: Any, *collections: str) -> Iterator[None]:
    from contextlib import ExitStack

    with ExitStack() as stack:
        for name in sorted(set(collections)):
            stack.enter_context(resource_lock(db, name))
        yield


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


def cleanup_records(
    db: Any, collection: str, scope: dict[str, Any], stale_query: dict[str, Any],
    source_rows: int, *, allow_large_cleanup: bool = False,
    batch_size: int = 1000,
) -> int:
    """Delete validated stale records in batches without retaining source copies."""
    if batch_size < 1:
        raise ValueError("batch_size must be greater than zero")
    target = db[collection]
    validate_cleanup(
        target.count_documents(scope), target.count_documents(stale_query),
        source_rows, allow_large_cleanup,
    )
    deleted = 0
    identifiers: list[Any] = []

    def flush() -> None:
        nonlocal deleted
        if not identifiers:
            return
        check_leases(verify=True)
        deleted += target.delete_many({
            **stale_query, "_id": {"$in": list(identifiers)},
        }).deleted_count
        identifiers.clear()

    cursor = target.find(stale_query, {"_id": 1})
    try:
        for document in cursor:
            identifiers.append(document["_id"])
            if len(identifiers) >= batch_size:
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


class Snapshot:
    """Validate keyed source rows on disk before changing the target dataset."""
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="sync-snapshot-")
        self.connection = sqlite3.connect(self.directory.name + "/rows.sqlite")
        self.connection.execute(
            "CREATE TABLE rows (key TEXT PRIMARY KEY, hash TEXT, payload BLOB, group_key TEXT)"
        )
        self.connection.execute(
            "CREATE INDEX rows_group ON rows (group_key) WHERE group_key IS NOT NULL"
        )
        self.duplicates = 0
        self.count = 0

    def add(self, key: str, document: dict[str, Any], *, group_key: str | None = None) -> None:
        payload = BSON.encode(document)
        digest = source_hash(document)
        existing = self.connection.execute(
            "SELECT hash, group_key FROM rows WHERE key = ?", (key,),
        ).fetchone()
        if existing:
            self.duplicates += 1
            if existing[0] != digest or existing[1] != group_key:
                raise RuntimeError(f"Conflicting source key: {key}")
            return
        self.connection.execute(
            "INSERT INTO rows VALUES (?, ?, ?, ?)", (key, digest, payload, group_key),
        )
        self.count += 1

    def contains(self, key: str) -> bool:
        return self.connection.execute("SELECT 1 FROM rows WHERE key = ?", (key,)).fetchone() is not None

    def contains_group(self, group_key: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM rows WHERE group_key = ? LIMIT 1", (group_key,),
        ).fetchone() is not None

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT payload FROM rows WHERE key = ?", (key,)).fetchone()
        return BSON(row[0]).decode() if row is not None else None

    def groups(self) -> Iterator[str]:
        for (key,) in self.connection.execute(
            "SELECT DISTINCT group_key FROM rows WHERE group_key IS NOT NULL ORDER BY group_key",
        ):
            yield key

    def group(self, key: str, *, limit: int = 10000) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM rows WHERE group_key = ? ORDER BY key LIMIT ?", (key, limit + 1),
        ).fetchall()
        if len(rows) > limit:
            raise RuntimeError(f"Snapshot group exceeds {limit} rows: {key}")
        return [BSON(row[0]).decode() for row in rows]

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
