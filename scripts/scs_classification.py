"""Durable service-order invalidation; callers hold both dataset locks."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo import UpdateOne

from scripts.sync_runtime import batches, check_leases, verified_bulk_write

PENDING = "sync_classification_pending"


def service_order_values(document: dict[str, Any]) -> set[str]:
    fields = (document.get("details") or {}).get("service_fields") or {}
    return {str(value).strip() for value in [
        document.get("service_uid"),
        *(fields.get(key) for key in ("服务单号", "服务订单号", "服务单")),
    ] if value and str(value).strip()}


def enqueue(db: Any, keys: set[str]) -> None:
    for key in sorted(keys):
        check_leases(verify=True)
        db[PENDING].update_one({"_id": key}, {"$set": {
            "generation": uuid4().hex, "updated_at": datetime.now(timezone.utc),
        }}, upsert=True)


def enqueue_change(db: Any, before: dict[str, Any], after: dict[str, Any]) -> None:
    old, new = service_order_values(before), service_order_values(after)
    if (old != new or before.get("is_5000_company") != after.get("is_5000_company")
            or before.get("_prodh_scope") != after.get("_prodh_scope")
            or before.get("service_order_keys") != sorted(new)):
        enqueue(db, old | new)


def related_query(keys: list[str]) -> dict[str, Any]:
    return {"$or": [
        {"service_order_keys": {"$in": keys}}, {"service_uid": {"$in": keys}},
        *({f"details.service_fields.{name}": {"$in": keys}}
          for name in ("服务单号", "服务订单号", "服务单")),
    ]}


def service_states(db: Any, doa_collection: str, keys: list[str]) -> tuple[dict[str, set[str]], set[str]]:
    states: dict[str, set[str]] = {}
    company: set[str] = set()
    for document in db[doa_collection].find(related_query(keys)):
        scope = "in_scope" if document.get("_prodh_scope") == "00100" else "pending"
        for key in service_order_values(document).intersection(keys):
            states.setdefault(key, set()).add(scope)
            if scope == "in_scope" and document.get("is_5000_company"):
                company.add(key)
    return states, company


def consume(db: Any, doa_collection: str, change_collection: str, *, limit: int = 1000) -> int:
    updated = 0
    for batch in batches(db[PENDING].find({}).limit(limit), 100):
        keys = [row["_id"] for row in batch]
        _, company = service_states(db, doa_collection, keys)
        operations = []
        for row in db[change_collection].find({"so_code": {"$in": keys}, "_prodh_scope": "00100"}):
            expected = row["so_code"] in company
            if row.get("is_5000_company") != expected:
                operations.append(UpdateOne({"_id": row["_id"]}, {"$set": {"is_5000_company": expected}}))
            if len(operations) == 500:
                updated += verified_bulk_write(db[change_collection], operations).modified_count
                operations.clear()
        if operations:
            updated += verified_bulk_write(db[change_collection], operations).modified_count
        for row in batch:
            check_leases(verify=True)
            db[PENDING].delete_many({"_id": row["_id"], "generation": row["generation"]})
    return updated
