#!/usr/bin/env python3
"""Synchronize SAP sales orders into an independent MongoDB database.

The default mode is incremental.  Each SAP source has its own date watermark;
the watermark is advanced only after that source has fetched and written
successfully. Use ``--full --start-date YYYY-MM-DD`` for the initial load.
Incremental runs recheck historical statuses before committing scoped watermarks.
Full runs update validated lines; absent records require independent deletion evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import tempfile
import time
from collections import OrderedDict
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
from typing import Any, Iterator, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))

try:
    import httpx
    from pymongo import ASCENDING, MongoClient, UpdateOne
    from bson import BSON
except ImportError as exc:  # pragma: no cover - exercised by deployment, not unit tests
    print(json.dumps({"success": False, "error": f"missing dependency: {exc.name}; install httpx and pymongo"}, ensure_ascii=False))
    raise SystemExit(2) from exc

from scripts.sources.hana.hana_view_sync import (
    env,
    load_dotenv,
    mongo_client_options,
    mongo_write_concern_summary,
    bson_value,
    hana_connection,
    mongo_uri,
    install_graceful_shutdown,
)
from scripts.sync_runtime import (
    Snapshot, batches, check_leases, error_payload, print_failure, resource_lock,
    required_prodh_list, validate_prodh, sales_scope_checkpoint_id, source_hash, save_scope_pending,
    validate_scope_environment,
    verified_bulk_write,
    clear_scope_pending,
    order_key,
)
from scripts.sales_order_reference import SalesReferences, host_scopes, sales_lines, sales_product_scope, trusted_sales_lines
from scripts.sources.sap_http.client import headers as sap_headers, records as validate_records


ROOT = PROJECT_ROOT
SOURCES = OrderedDict((
    ("SG", "http://10.2.101.37:8000/sap/ZHTTP_SIMS?sap-client=800"),
    ("KK", "http://10.8.100.11:8001/sap/ZHTTP_SIMS?sap-client=600"),
))
METHOD = "ZSIMS_CL_INBOUND_MO_PSINFO"
DEFAULT_TIMEOUT = 120.0
REPAIR_VIEW = '"_SYS_BIC"."BW_LOCAL.PP/ZSGV_ZZT_WLJL"'
REPAIR_COLUMNS = tuple(
    "MANDT PCODE ZWXDT ZMCOD1 ZRCOD1 MATNR ZJXMC ZNGGZ ZNGWD ZWXWD ZBJ ZZRFL ZCCLH "
    "ZGZMS MAKTX ZMCOD2 ZRCOD2 ZDATE ZTIME ZUSER ZSOURCE ZDATE_WX REJUDGE RET RPDESC "
    "RNOTE SECFLG FACTORY ZWXWD1 ZWXWD2 ZWXWD3 U_FIX FIX_REMARKS TESTID ZNGSPEC T_FIND "
    "ZNGWD1 ZNGWD2 ZNGWD3 ERROR_CODE ERROR_MSG RETEST_STATION TEST_LOG_NAME SECOND_PART_NO "
    "RECORD01REPAIRM SLOT AUFNR VBELN POSNR U_FIND U_RMA_NAME RMA_RESULT RMA_TYPE2".split()
)
REPAIR_KEY_FIELDS = ("MANDT", "PCODE", "ZMCOD1", "ZDATE_WX", "ZTIME")
REPAIR_ID_FIELDS = ("MANDT", "PCODE", "ZWXDT", "ZMCOD1", "ZMCOD2", "ZRCOD1", "ZRCOD2")
REPAIR_VIEW_ID = "ZSGV_ZZT_WLJL"
# The repair view is only in scope from this date onward. This lower bound is
# enforced for both full and incremental runs, even when callers omit a start.
REPAIR_START_DATE = date(2025, 1, 1)
SALES_SCOPE_START = date(2026, 1, 1)
STATUS_LINE_FIELDS = ("MANDT", "AUFNR", "VBELN", "POSNR", "ETENR", "MATNR")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD: {value}") from exc


def parse_prodh(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def request_headers() -> dict[str, str]:
    return sap_headers(METHOD)


def current_date() -> date:
    return date.today()


def number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def source_key(row: dict[str, Any], fields: tuple[str, ...]) -> str:
    values = [str(row.get(field) or "").strip() for field in fields]
    if not all(values):
        payload = json.dumps(sorted(row.items()), ensure_ascii=False, default=str, separators=(",", ":"))
        return "full_row:" + hashlib.sha256(payload.encode()).hexdigest()
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def aggregate(source: str, records: list[dict[str, Any]], synced_at: datetime) -> tuple[list[dict[str, Any]], dict[str, int]]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    skipped = 0
    for row in records:
        aufnr = str(row.get("AUFNR") or "").strip()
        if not aufnr:
            skipped += 1
            continue
        grouped.setdefault(aufnr, []).append(row)

    documents: list[dict[str, Any]] = []
    for aufnr, rows in grouped.items():
        # Keep every returned line while exposing the last row as the convenient
        # order-level view, matching the source's stable response ordering.
        latest = rows[-1]
        documents.append({
            "_id": f"{source}:{aufnr}",
            "source_aufnr": f"{source}:{aufnr}",
            "source": source,
            "aufnr": aufnr,
            "data": latest,
            "records": rows,
            "record_count": len(rows),
            "order_quantity": sum(number(row.get("GAMNG")) for row in rows),
            "storage_quantity": sum(number(row.get("WMENG")) for row in rows),
            "gstrs_date": str(latest.get("GSTRS") or "")[:10] or None,
            "last_synced_at": synced_at,
        })
    return documents, {"fetched": len(records), "unique": len(documents), "skipped": skipped}


def repair_query(start_date: date, end_date: date) -> tuple[str, list[Any]]:
    params = [start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")]
    selected = ", ".join(f'"{field}"' for field in REPAIR_COLUMNS)
    where = ' WHERE "ZDATE_WX" >= ? AND "ZDATE_WX" <= ?'
    order = ", ".join(f'"{field}"' for field in REPAIR_ID_FIELDS)
    return f"SELECT {selected} FROM {REPAIR_VIEW}{where} ORDER BY {order}", params


def repair_scope_filter(start_date: date, end_date: date) -> dict[str, Any]:
    return {
        "_source_view": REPAIR_VIEW_ID,
        "ZDATE_WX": {"$gte": start_date.strftime("%Y%m%d"), "$lte": end_date.strftime("%Y%m%d")},
    }


def repair_cleanup_filter(run_id: str, start_date: date, end_date: date) -> dict[str, Any]:
    return {**repair_scope_filter(start_date, end_date), "_scope_run_id": {"$ne": run_id}}


def repair_window_start(end_date: date) -> date:
    month = end_date.year * 12 + end_date.month - 3
    return max(date(month // 12, month % 12 + 1, 1), REPAIR_START_DATE)


def repair_identity(row: Mapping[str, Any]) -> str:
    values = [str(row.get(field) or "").strip() for field in REPAIR_ID_FIELDS]
    if not all(values[:3]):
        raise ValueError("维修记录缺少稳定身份字段 MANDT/PCODE/ZWXDT，拒绝提交快照")
    try:
        values[2] = normalize_repair_datetime(values[2])
    except ValueError as exc:
        raise ValueError("维修记录 ZWXDT 必须为有效的 14 位维修日期时间") from exc
    # Empty part serials are valid; never fall back to a mutable whole-row hash.
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return "repair:v3:" + hashlib.sha256(payload.encode()).hexdigest()


def repair_content_hash(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(BSON.encode({field: bson_value(row.get(field)) for field in REPAIR_COLUMNS})).hexdigest()


def normalize_repair_datetime(value: Any) -> str:
    """Normalize the repair view's compact and slash-separated timestamps."""
    raw = str(value or "").strip()
    formats = (
        (r"[0-9]{14}", "%Y%m%d%H%M%S"),
        (r"[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{6}", "%Y/%m/%d %H%M%S"),
    )
    for pattern, date_format in formats:
        if re.fullmatch(pattern, raw):
            return datetime.strptime(raw, date_format).strftime("%Y%m%d%H%M%S")
    raise ValueError("维修记录 ZWXDT 必须为有效的 14 位维修日期时间")


@contextmanager
def repair_snapshot(sql: str, params: list[Any], batch_size: int, stats: dict[str, Any]) -> Iterator[Any]:
    """Validate a complete source snapshot on disk before any MongoDB mutation."""
    with tempfile.TemporaryDirectory(prefix="repair-snapshot-") as directory:
        snapshot = sqlite3.connect(str(Path(directory) / "snapshot.sqlite"))
        try:
            snapshot.execute("CREATE TABLE repairs (identity TEXT PRIMARY KEY, payload BLOB, hash TEXT, legacy_key TEXT)")
            with hana_connection() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute(sql, tuple(params))
                    while rows := cursor.fetchmany(batch_size):
                        check_leases()
                        stats["batches"] += 1
                        for values in rows:
                            row = dict(zip(REPAIR_COLUMNS, values, strict=True))
                            row["ZWXDT"] = normalize_repair_datetime(row.get("ZWXDT"))
                            identity = repair_identity(row)
                            repair_date = str(row.get("ZDATE_WX") or "").strip()
                            if not params[0] <= repair_date <= params[1]:
                                raise ValueError("维修记录的 ZDATE_WX 不在本次快照范围内")
                            payload = BSON.encode({field: bson_value(row[field]) for field in REPAIR_COLUMNS})
                            content_hash = hashlib.sha256(payload).hexdigest()
                            existing = snapshot.execute("SELECT hash FROM repairs WHERE identity = ?", (identity,)).fetchone()
                            if existing:
                                if existing[0] != content_hash:
                                    raise RuntimeError(f"维修明细身份冲突，拒绝提交快照: {identity}")
                                stats["duplicate_source_rows"] += 1
                            else:
                                snapshot.execute("INSERT INTO repairs VALUES (?, ?, ?, ?)", (
                                    identity, payload, content_hash, source_key(row, REPAIR_KEY_FIELDS),
                                ))
                            stats["source_rows"] += 1
                finally:
                    cursor.close()
            snapshot.commit()
            stats["unique_rows"] = snapshot.execute("SELECT COUNT(*) FROM repairs").fetchone()[0]
            stats["snapshot_complete"] = True
            yield snapshot
        finally:
            snapshot.close()


def is_complete_month(start_date: date, end_date: date) -> bool:
    next_month = date(start_date.year + (start_date.month == 12), start_date.month % 12 + 1, 1)
    return start_date.day == 1 and end_date + timedelta(days=1) == next_month


def sync_repair(
    db: Any, collection: str, *, mode: str, start_date: date | None,
    end_date: date, batch_size: int, dry_run: bool, run_id: str,
    defer_finalize: bool = False, allow_large_cleanup: bool = False,
    preview_snapshot: Snapshot | None = None,
    sales_collection: str | None = None, station_collection: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    validate_scope_environment()
    if mode not in {"incremental", "full", "monthly"}:
        raise ValueError("未知维修同步模式")
    if batch_size < 1 or not run_id:
        raise ValueError("维修同步必须提供有效 batch_size 和 run_id")
    if end_date < REPAIR_START_DATE:
        raise ValueError("维修数据仅同步 2025-01-01 及之后的数据")
    if mode == "monthly" and (start_date is None or not is_complete_month(start_date, end_date)):
        raise ValueError("按月同步必须提供完整自然月的起止日期")
    if mode == "incremental":
        start_date = repair_window_start(end_date)
    start_date = max(start_date or REPAIR_START_DATE, REPAIR_START_DATE)
    if start_date > end_date:
        raise ValueError("维修同步起始日期不能晚于结束日期")
    refs = SalesReferences.load(db[sales_collection or env("TARGET_COLLECTION", "sales_orders_sap")])
    if not refs.rows:
        raise RuntimeError("销售订单表为空，无法判断公司归属")
    allowed_vbelns = {row["sales"] for row in refs.rows if row["sales"]}
    sql, params = repair_query(start_date, end_date)
    stats: dict[str, Any] = {
        "success": False, "mode": mode, "strategy": "rolling_snapshot" if mode == "incremental" else "snapshot",
        "run_id": run_id, "snapshot_complete": False, "source_rows": 0, "unique_rows": 0,
        "duplicate_source_rows": 0, "matched_sales_orders": 0, "empty_sales_orders_retained": 0,
        "unmatched_sales_orders_retained": 0, "batches": 0,
        "inserted": 0, "updated": 0, "unchanged": 0, "relinked": 0, "would_insert": 0, "would_update": 0,
        "sales_order_vbelns": len(allowed_vbelns), "allow_large_cleanup": allow_large_cleanup,
        "range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
    }
    target = db[collection]
    host_orders: dict[tuple[str, str], set[tuple[str, str]]] = {}
    host_scopes(db[station_collection or env("STATION_COLLECTION", "station_records_sap")], refs,
                orders=host_orders)
    stats.update(prodh_list=required_prodh_list(), in_scope=0, out_of_scope=0, scope_pending=0,
                 scope_rule_version=1, cleanup="disabled_unverified_coverage", coverage_verified=False)
    with repair_snapshot(sql, params, batch_size, stats) as snapshot:
        snapshot.execute("CREATE TABLE targets (identity TEXT, payload BLOB)")
        snapshot.execute("CREATE INDEX targets_identity ON targets(identity)")
        snapshot.execute("CREATE TABLE admitted (identity TEXT PRIMARY KEY)")
        snapshot.execute("CREATE TABLE resolved (identity TEXT PRIMARY KEY)")
        snapshot.execute("CREATE TABLE host_links (identity TEXT PRIMARY KEY, production TEXT, sales TEXT)")
        dataset = "repair_records"
        retry_limit = int(env("REPAIR_SCOPE_RETRY_LIMIT", "100"))
        if retry_limit < 1:
            raise ValueError("REPAIR_SCOPE_RETRY_LIMIT 必须大于 0")
        pending_cursor = db["sync_scope_pending"].find(
            {"dataset": dataset}, sort=[("updated_at", 1)], limit=retry_limit,
        )
        try:
            for index, pending in enumerate(pending_cursor):
                if index >= retry_limit:
                    break
                row = pending["payload"]
                identity = repair_identity(row)
                repair_date = str(row.get("ZDATE_WX") or "")
                if REPAIR_START_DATE.strftime("%Y%m%d") <= repair_date <= params[1] and not snapshot.execute(
                    "SELECT 1 FROM repairs WHERE identity = ?", (identity,),
                ).fetchone():
                    payload = BSON.encode(row)
                    snapshot.execute("INSERT INTO repairs VALUES (?, ?, ?, ?)", (
                        identity, payload, hashlib.sha256(payload).hexdigest(), source_key(row, REPAIR_KEY_FIELDS),
                    ))
        finally:
            if hasattr(pending_cursor, "close"):
                pending_cursor.close()
        stats["source_unique_rows"] = stats["unique_rows"]
        target_start = start_date
        for identity, payload in snapshot.execute("SELECT identity, payload FROM repairs"):
            row = BSON(payload).decode()
            scope = refs.company_scope(row)
            if scope == "pending" and not row.get("AUFNR") and not row.get("VBELN"):
                pairs = host_orders.get((str(row.get("MANDT") or ""), str(row.get("PCODE") or "")), set())
                if len(pairs) == 1:
                    production, sales = next(iter(pairs))
                    if refs.company_scope({"AUFNR": production, "VBELN": sales}) == "in_scope":
                        scope = "in_scope"
                        snapshot.execute("INSERT INTO host_links VALUES (?, ?, ?)", (identity, production, sales))
            stats["scope_pending" if scope == "pending" else scope] += 1
            if scope == "in_scope":
                snapshot.execute("INSERT INTO admitted VALUES (?)", (identity,))
                target_start = min(target_start, datetime.strptime(row["ZDATE_WX"], "%Y%m%d").date())
            elif scope == "pending" and not dry_run:
                save_scope_pending(db, dataset, identity, row)
            if scope != "pending":
                snapshot.execute("INSERT INTO resolved VALUES (?)", (identity,))
        stats["unique_rows"] = stats["in_scope"]
        stats["target_range"] = {"start": target_start.isoformat(), "end": end_date.isoformat()}
        existing_count = 0
        cursor = target.find(repair_scope_filter(target_start, end_date))
        try:
            for document in cursor:
                existing_count += 1
                try:
                    identity = repair_identity(document)
                except ValueError:
                    continue
                snapshot.execute("INSERT INTO targets VALUES (?, ?)", (identity, BSON.encode(document)))
                for alias in document.get("_legacy_ids") or []:
                    if (isinstance(alias, str) and alias.startswith("repair:v3:") and alias != identity
                            and alias not in (document.get("_merged_source_ids") or [])):
                        snapshot.execute("INSERT INTO targets VALUES (?, ?)", (alias, BSON.encode(document)))
        finally:
            if hasattr(cursor, "close"):
                cursor.close()
        reused = snapshot.execute(
            "SELECT COUNT(*) FROM admitted WHERE EXISTS "
            "(SELECT 1 FROM targets WHERE targets.identity = admitted.identity)",
        ).fetchone()[0]
        stats["stale_candidates"] = existing_count - reused
        if not dry_run:
            target.create_index("_repair_id", unique=True, name="repair_identity_unique",
                                partialFilterExpression={"_repair_id": {"$type": "string"}})
            target.create_index("_source_key", unique=True, name="source_key_unique")
            target.create_index([("_source_view", ASCENDING), ("ZDATE_WX", ASCENDING), ("_scope_run_id", ASCENDING)],
                                name="repair_snapshot_cleanup")
            target.create_index("_legacy_ids", name="repair_legacy_ids")
            pending = (identity for identity, in snapshot.execute(
                "SELECT identity FROM repairs WHERE identity NOT IN (SELECT identity FROM admitted)"
            ))
            for keys in batches(pending, batch_size):
                check_leases(verify=True)
                target.update_many({"$or": [
                    {"_repair_id": {"$in": keys}}, {"_source_key": {"$in": keys}},
                    {"_legacy_ids": {"$in": keys}},
                ]}, {"$unset": {"_prodh_scope": "", "_scope_rule_version": ""}})
        operations = []
        for identity, payload, content_hash, legacy_key in snapshot.execute(
            "SELECT repairs.* FROM repairs JOIN admitted USING(identity) ORDER BY identity",
        ):
            row = BSON(payload).decode()
            normalized_vbeln = str(row.get("VBELN") or "").strip()
            if not normalized_vbeln:
                stats["empty_sales_orders_retained"] += 1
            elif normalized_vbeln in allowed_vbelns:
                stats["matched_sales_orders"] += 1
            else:
                stats["unmatched_sales_orders_retained"] += 1
            previous_rows = snapshot.execute(
                "SELECT payload FROM targets WHERE identity = ? LIMIT 10001", (identity,),
            ).fetchall()
            if len(previous_rows) > 10000:
                raise RuntimeError(f"维修同身份目标超过 10000 条，拒绝处理: {identity}")
            previous = [BSON(values[0]).decode() for values in previous_rows]
            canonical = max(previous, key=lambda doc: (
                doc.get("_repair_id") == identity, str(doc.get("_synced_at") or ""), str(doc["_id"]),
            )) if previous else None
            if canonical and canonical.get("_repair_id") != identity:
                for field in REPAIR_COLUMNS:
                    if is_empty_repair_value(row.get(field)) and not is_empty_repair_value(canonical.get(field)):
                        row[field] = canonical[field]
                identity = repair_identity(row)
                content_hash = repair_content_hash(row)
            source_orders = {field: row[field] for field in ("AUFNR", "VBELN")}
            host_link = snapshot.execute("SELECT production, sales FROM host_links WHERE identity = ?", (identity,)).fetchone()
            if host_link:
                row["AUFNR"], row["VBELN"] = host_link
            if canonical:
                previous_orders = canonical.get("_source_orders") or {}
                for field in source_orders:
                    if not row[field] and not previous_orders.get(field):
                        row[field] = canonical.get(field, "")
                inherited = any(row[field] != source_orders[field] for field in source_orders)
                host_pairs = host_orders.get((str(row.get("MANDT") or ""), str(row.get("PCODE") or "")), set())
                host_matches = any(
                    production == order_key(row["AUFNR"]) and (not sales or sales == order_key(row["VBELN"]))
                    for production, sales in host_pairs
                )
                if inherited and (not refs.unique_company("production", production=row["AUFNR"], sales=row["VBELN"], client=row.get("MANDT")) or (
                    not any(source_orders.values()) and not host_matches
                )):
                    row.update(source_orders)
                if any(row[field] != canonical.get(field, "") for field in source_orders):
                    row["GSTRS"] = ""
            changed = (canonical is None or canonical.get("_source_hash") != content_hash
                       or any(row[field] != canonical.get(field, "") for field in source_orders))
            relinked = canonical is not None and canonical.get("_repair_id") != identity
            stats["would_insert"] += canonical is None
            stats["would_update"] += canonical is not None and changed
            stats["unchanged"] += canonical is not None and not changed
            stats["relinked"] += relinked
            now = datetime.now(timezone.utc)
            aliases = {legacy_key}
            for document in previous:
                aliases.update(document.get("_legacy_ids") or [])
                aliases.add(str(document["_id"]))
                if document.get("_repair_id"):
                    aliases.add(document["_repair_id"])
                if document.get("_source_key"):
                    aliases.add(document["_source_key"])
            metadata: dict[str, Any] = {
                "_repair_id": identity, "_source_key": identity, "_source_view": REPAIR_VIEW_ID,
                "_source_identity_version": 3, "_source_hash": content_hash,
                "_scope_run_id": run_id, "_sync_run_id": run_id, "_last_seen_at": now,
                "_legacy_ids": sorted(aliases),
                "_prodh_scope": "00100", "_scope_rule_version": 2,
            }
            if changed:
                metadata.update(row)
                metadata.update({"_source_orders": source_orders, "_synced_at": now})
            if canonical and "_first_seen_at" not in canonical:
                metadata["_first_seen_at"] = canonical.get("_synced_at") or now
            if dry_run:
                if preview_snapshot is not None:
                    preview_snapshot.add(identity, {**(canonical or {}), **metadata,
                        "_id": canonical["_id"] if canonical else hashlib.sha256(identity.encode()).hexdigest()})
                continue
            query = {"_id": canonical["_id"]} if canonical else {"_repair_id": identity}
            update: dict[str, Any] = {"$set": metadata}
            if canonical is None:
                update["$setOnInsert"] = {"_id": hashlib.sha256(identity.encode()).hexdigest(), "_first_seen_at": now}
            operations.append(UpdateOne(query, update, upsert=canonical is None))
            stats["inserted"] += canonical is None
            stats["updated"] += canonical is not None and changed
            if len(operations) >= batch_size:
                verified_bulk_write(target, operations)
                operations.clear()
        if operations:
            verified_bulk_write(target, operations)
        if not dry_run:
            clear_scope_pending(db, dataset, (identity for identity, in snapshot.execute("SELECT identity FROM resolved")))
        stats["sqlite_bytes"] = (snapshot.execute("PRAGMA page_count").fetchone()[0]
                                 * snapshot.execute("PRAGMA page_size").fetchone()[0])
    stats["success"] = True
    if not dry_run and not defer_finalize:
        stats.update(finalize_repair_run(db, collection, stats, run_id))
    stats.update(elapsed_seconds=round(time.monotonic() - started, 3),
                 scope_backlog=db["sync_scope_pending"].count_documents({"dataset": dataset}))
    return stats


def finalize_repair_run(db: Any, collection: str, sync_result: Mapping[str, Any], run_id: str) -> dict[str, int]:
    """Verify the snapshot, then merge compatible duplicate repair records."""
    if not sync_result.get("success") or not sync_result.get("snapshot_complete") or sync_result.get("run_id") != run_id:
        raise ValueError("不能提交未完整校验或 run_id 不匹配的维修快照")
    sync_range = sync_result.get("target_range", sync_result["range"])
    start_date = date.fromisoformat(sync_range["start"])
    end_date = date.fromisoformat(sync_range["end"])
    target = db[collection]
    covered_count = target.count_documents({
        **repair_scope_filter(start_date, end_date), "_scope_run_id": run_id,
    })
    if covered_count != sync_result["unique_rows"]:
        raise RuntimeError("本次维修快照尚未完整写入，拒绝清理旧记录")
    return cleanup_repair_duplicates(target, repair_scope_filter(start_date, end_date), run_id=run_id)


def cleanup_repair_duplicates(target: Any, query: dict[str, Any], *, run_id: str | None = None,
                              apply: bool = True, strict_guard: bool = False) -> dict[str, int]:
    """Merge compatible records for a verified run, or for an explicit full-table cleanup."""
    stats = {
        "scanned": 0,
        "deleted_out_of_scope": 0, "merged_duplicate_groups": 0,
        "deleted_duplicates": 0, "conflict_groups": 0, "multiple_current_groups": 0,
        "identity_change_groups": 0, "invalid_key_rows": 0,
    }
    with tempfile.TemporaryDirectory(prefix="repair-cleanup-") as directory:
        with closing(sqlite3.connect(str(Path(directory) / "groups.sqlite"))) as groups:
            groups.execute("CREATE TABLE rows (group_key TEXT, payload BLOB)")
            groups.execute("CREATE INDEX rows_key ON rows(group_key)")
            cursor = target.find(query)
            try:
                for document in cursor:
                    check_leases()
                    stats["scanned"] += 1
                    key = repair_merge_key(document)
                    if key is None:
                        stats["invalid_key_rows"] += 1
                        continue
                    groups.execute("INSERT INTO rows VALUES (?, ?)", (json.dumps(key, ensure_ascii=False), BSON.encode(document)))
            finally:
                if hasattr(cursor, "close"):
                    cursor.close()
            group_keys = groups.execute(
                "SELECT group_key, COUNT(*) FROM rows GROUP BY group_key HAVING COUNT(*) > 1"
            )
            for group_key, count in group_keys:
                if count > 10000:
                    raise RuntimeError("维修重复组超过 10000 条，拒绝清理")
                rows = [BSON(payload).decode() for payload, in groups.execute(
                    "SELECT payload FROM rows WHERE group_key = ?", (group_key,),
                )]
                current_rows = rows if run_id is None else [row for row in rows if row.get("_scope_run_id") == run_id]
                if not current_rows:
                    continue
                current = current_rows[0]
                merged, conflicts = merge_repair_fields(rows, current)
                if conflicts:
                    stats["conflict_groups"] += 1
                    continue
                try:
                    merged_identity = repair_identity(merged)
                except ValueError:
                    stats["identity_change_groups"] += 1
                    continue
                current = next((row for row in current_rows if row.get("_repair_id") == merged_identity), current)
                collision = target.find_one({
                    "_repair_id": merged_identity, "_id": {"$ne": current["_id"]},
                })
                if collision:
                    stats["identity_change_groups"] += 1
                    continue
                aliases = set(current.get("_legacy_ids") or [])
                for row in rows:
                    aliases.update(row.get("_legacy_ids") or [])
                    aliases.add(str(row["_id"]))
                    if row.get("_repair_id"):
                        aliases.add(row["_repair_id"])
                    if row.get("_source_key"):
                        aliases.add(row["_source_key"])
                merged["_repair_id"] = merged_identity
                merged["_source_key"] = merged_identity
                merged["_source_identity_version"] = 3
                merged["_source_hash"] = repair_content_hash(merged)
                updates = {
                    key: value for key, value in merged.items()
                    if not key.startswith("_") and current.get(key) != value
                }
                updates.update({
                    "_repair_id": merged_identity, "_source_key": merged_identity,
                    "_source_identity_version": 3, "_source_hash": merged["_source_hash"],
                })
                updates["_legacy_ids"] = sorted(aliases)
                updates["_merged_source_ids"] = sorted({
                    identity for row in rows
                    for identity in [*(row.get("_merged_source_ids") or []), row.get("_repair_id")]
                    if isinstance(identity, str) and identity.startswith("repair:v3:") and identity != merged_identity
                })
                if not apply:
                    stats["merged_duplicate_groups"] += 1
                    stats["deleted_duplicates"] += len(rows) - 1
                    continue
                def guard(row: Mapping[str, Any]) -> dict[str, Any]:
                    if strict_guard:
                        return {"_id": row["_id"], "$expr": {"$eq": ["$$ROOT", {"$literal": row}]}}
                    return repair_unchanged_filter(row)
                check_leases(verify=True)
                result = target.update_one(guard(current), {"$set": updates})
                if result.matched_count != 1:
                    raise RuntimeError("维修合并记录已变更，拒绝清理")
                for row in rows:
                    if row["_id"] == current["_id"]:
                        continue
                    check_leases(verify=True)
                    result = target.delete_many(guard(row))
                    if result.deleted_count != 1:
                        raise RuntimeError("维修重复记录已变更，停止清理")
                    stats["deleted_duplicates"] += 1
                stats["merged_duplicate_groups"] += 1
    return stats


def repair_merge_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str] | None:
    pcode = str(row.get("PCODE") or "").strip()
    if not pcode:
        return None
    try:
        timestamp = normalize_repair_datetime(row.get("ZWXDT"))
    except ValueError:
        return None
    return (pcode, timestamp, *(str(row.get(field) or "").strip() for field in ("ZMCOD1", "ZRCOD1", "MATNR")))


def is_empty_repair_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def merge_repair_fields(
    rows: list[dict[str, Any]], current: dict[str, Any],
) -> tuple[dict[str, Any], set[str]]:
    merged = dict(current)
    conflicts: set[str] = set()
    for row in rows:
        for field, value in row.items():
            if field.startswith("_") or is_empty_repair_value(value):
                continue
            existing = merged.get(field)
            if is_empty_repair_value(existing):
                merged[field] = value
                continue
            if field in ("PCODE", "ZWXDT", "ZMCOD1", "ZRCOD1", "MATNR"):
                if repair_merge_key(merged) != repair_merge_key(row):
                    conflicts.add(field)
            elif existing != value:
                conflicts.add(field)
    return merged, conflicts


def repair_unchanged_filter(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"_id": row["_id"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SAP SG/KK 销售订单同步到独立 MongoDB")
    parser.add_argument("--full", action="store_true", help="执行全量日期范围同步；必须同时提供 --start-date")
    parser.add_argument("--start-date", type=parse_date, help="全量起始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", type=parse_date, default=date.today(), help="结束日期，默认今天")
    parser.add_argument("--lookback-days", type=int, default=int(env("SYNC_LOOKBACK_DAYS", "7")), help="增量回看天数，默认 .env 中的值")
    parser.add_argument("--status-start-date", type=parse_date, default=SALES_SCOPE_START, help="增量同步后历史状态核对的起始日期，默认 2026-01-01")
    parser.add_argument("--skip-status-refresh", action="store_true", help="跳过增量同步后的历史状态核对")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prodh-list", nargs="+", help="产品层次，仅接受 00100")
    group.add_argument("--all-prodh", action="store_true", help="已禁用：产品层次必须限定 00100")
    parser.add_argument("--dry-run", action="store_true", help="只请求和统计，不写入订单及水位线")
    parser.add_argument("--replace-collection", action="store_true", help="已禁用：尚无来源覆盖证明，不能替换集合")
    parser.add_argument("--confirm-delete", action="store_true", help="兼容旧命令，不解除产品范围或来源覆盖校验")
    return parser


def configured_prodh(args: argparse.Namespace) -> list[str] | None:
    if getattr(args, "all_prodh", False):
        raise ValueError("--all-prodh 已禁用，必须限定 00100")
    validate_prodh(parse_prodh(env("SOURCE_PRODH_LIST", "00100")))
    values = getattr(args, "prodh_list", None)
    return validate_prodh(values if values is not None else required_prodh_list())


def payload(start: date, end: date, prodh: list[str] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"CHDAT": start.isoformat(), "CHDAT_TO": end.isoformat()}
    body["PRODH_LIST"] = validate_prodh(prodh)
    return body


def date_windows(start: date, end: date, days: int) -> list[tuple[date, date]]:
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=days - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def status_line_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(field) or "").strip() for field in STATUS_LINE_FIELDS)


def order_status_changes(
    snapshot: Snapshot, source: str, document: Mapping[str, Any],
) -> tuple[dict[str, str], bool, bool, int]:
    changes: dict[str, str] = {}
    missing_status = False
    missing_order_type = False
    unmatched_details = 0
    rows = chain(
        (("data", document.get("data") or {}),),
        ((f"records.{index}", row) for index, row in enumerate(document.get("records") or [])),
    )
    for path, row in rows:
        stored = snapshot.get(json.dumps([source, *status_line_key(row)]))
        if stored is None:
            if path == "data":
                missing_status = True
            else:
                unmatched_details += 1
        else:
            if stored["status"] is None:
                missing_status = True
            elif row.get("ZSTAT") != stored["status"]:
                changes[f"{path}.ZSTAT"] = stored["status"]
            if stored["order_type"] is None:
                missing_order_type = True
            elif row.get("AUART") != stored["order_type"]:
                changes[f"{path}.AUART"] = stored["order_type"]
    return changes, missing_status, missing_order_type, unmatched_details


def status_partition(
    db: Any, source: str, start: date, end: date, window_days: int,
) -> tuple[list[tuple[date, date]], str | None, int]:
    recent_days = int(env("STATUS_RECENT_DAYS", "0"))
    if recent_days == 0:
        return date_windows(start, end, window_days), None, 0
    budget = int(env("STATUS_HISTORY_WINDOWS", "0"))
    interval = int(env("STATUS_HISTORY_MAX_INTERVAL_DAYS", "0"))
    runs = int(env("SYNC_RUNS_PER_DAY", "0"))
    if min(recent_days, budget, interval, runs, window_days) < 1:
        raise ValueError("状态分片必须配置正数的近期天数、历史预算、最大间隔及每日运行次数")
    recent_start = max(start, end - timedelta(days=recent_days - 1))
    history = date_windows(start, recent_start - timedelta(days=1), window_days)
    if math.ceil(len(history) / budget) / runs > interval:
        raise ValueError("历史窗口预算不足以满足最大核对间隔")
    identity = sales_scope_checkpoint_id("sales_status", source) + ":" + source_hash({
        "start": start, "recent_days": recent_days, "window_days": window_days,
        "method": METHOD, "url": SOURCES[source],
    })[:16]
    previous = db[env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints")].find_one({"_id": identity}) or {}
    index = int(previous.get("next_index", 0)) % max(1, len(history))
    selected = [history[(index + offset) % len(history)] for offset in range(min(budget, len(history)))]
    return selected + date_windows(recent_start, end, window_days), identity, (index + len(selected)) % max(1, len(history))


def refresh_order_statuses(
    db: Any,
    http: Any,
    *,
    start: date,
    end: date,
    prodh: list[str] | None,
    apply: bool,
    run_id: str,
) -> dict[str, Any]:
    """Patch fully matched existing orders within the returned SAP snapshot."""
    validate_prodh(prodh)
    window_days = int(env("FULL_WINDOW_DAYS", "7"))
    batch_size = int(env("SYNC_BATCH_SIZE", "1000"))
    if start > end or window_days < 1 or batch_size < 1:
        raise ValueError("状态刷新日期范围、FULL_WINDOW_DAYS 或 SYNC_BATCH_SIZE 无效")
    collection = db[env("TARGET_COLLECTION", "sales_orders_sap")]
    result: dict[str, Any] = {
        "success": True, "partial": False, "dry_run": not apply, "run_id": run_id,
        "scope": "returned_orders",
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "existing": 0, "matched": 0, "not_returned": 0,
        "missing_status": 0, "missing_order_type": 0, "unmatched_details": 0,
        "unchanged": 0, "would_update": 0, "updated": 0, "checked": 0,
        "sources": {}, "transitions": {}, "order_type_transitions": {},
        "not_returned_samples": [], "prodh_list": required_prodh_list(),
        "out_of_scope": 0, "scope_pending": 0, "skipped_orders": 0,
        "skipped_samples": [],
    }
    partitions = {}
    # Validate the entire read before any target writes, including the second source.
    with Snapshot() as snapshot:
        for source, url in SOURCES.items():
            windows, identity, next_index = status_partition(db, source, start, end, window_days)
            partitions[source] = (windows, identity, next_index)
            stats = {"windows": len(windows), "fetched": 0, "http_requests": 0}
            result["sources"][source] = stats
            for index, (window_start, window_end) in enumerate(windows, 1):
                check_leases()
                response = http.post(
                    url, json=payload(window_start, window_end, prodh),
                    headers=request_headers(),
                )
                stats["http_requests"] += 1
                response.raise_for_status()
                rows = validate_records(response.json(), source)
                for row in rows:
                    key = status_line_key(row)
                    if not key[1]:
                        raise RuntimeError(f"{source}: 状态快照缺少 AUFNR，拒绝写入")
                    scope = sales_source_scope(row)
                    if scope == "out_of_scope":
                        result["out_of_scope"] += 1
                        continue
                    if scope == "pending":
                        raise RuntimeError(f"{source}: 状态来源未验证 00100 产品归属")
                    status = row.get("ZSTAT")
                    if status is not None and not isinstance(status, str):
                        raise RuntimeError(f"{source}: ZSTAT 必须为字符串或空值")
                    order_type = row.get("AUART")
                    if order_type is not None and not isinstance(order_type, str):
                        raise RuntimeError(f"{source}: AUART 必须为字符串或空值")
                    snapshot.add(
                        json.dumps([source, *key]),
                        {"status": status, "order_type": order_type},
                        group_key=json.dumps([source, key[1]]),
                    )
                stats["fetched"] += len(rows)
                if index == 1 or index % 10 == 0 or index == len(windows):
                    print(json.dumps({
                        "stage": "sales_status_fetch", "source": source,
                        "window": index, "windows": len(windows),
                        "fetched": stats["fetched"],
                    }), file=sys.stderr, flush=True)

        checked_at = datetime.now(timezone.utc)
        operations: list[Any] = []
        changed_in_batch = 0

        def flush() -> None:
            nonlocal changed_in_batch
            if not operations:
                return
            check_leases(verify=True)
            written = collection.bulk_write(operations, ordered=False)
            if written.matched_count != len(operations):
                raise RuntimeError("状态刷新时已有订单发生变化或消失，请重新核对")
            result["checked"] += written.matched_count
            result["updated"] += changed_in_batch
            operations.clear()
            changed_in_batch = 0

        query = {"source": {"$in": list(SOURCES)}}
        cursor = collection.find(query, {"source": 1, "aufnr": 1, "data": 1, "records": 1})
        try:
            for document in cursor:
                result["existing"] += 1
                source = document["source"]
                aufnr = str(document.get("aufnr") or "").strip()
                if not snapshot.contains_group(json.dumps([source, aufnr])):
                    result["not_returned"] += 1
                    if len(result["not_returned_samples"]) < 20:
                        result["not_returned_samples"].append(str(document["_id"]))
                    continue
                result["matched"] += 1
                if any(sales_product_scope(row) != "in_scope" for row in sales_lines(document)):
                    result["scope_pending"] += 1
                    result["skipped_orders"] += 1
                    if len(result["skipped_samples"]) < 20:
                        result["skipped_samples"].append({
                            "source": source, "aufnr": aufnr, "reason": "scope_pending",
                        })
                    continue
                changes, missing_status, missing_order_type, unmatched_details = order_status_changes(
                    snapshot, source, document,
                )
                result["missing_status"] += int(missing_status)
                result["missing_order_type"] += int(missing_order_type)
                result["unmatched_details"] += unmatched_details
                if missing_status or missing_order_type or unmatched_details:
                    result["skipped_orders"] += 1
                    if len(result["skipped_samples"]) < 20:
                        result["skipped_samples"].append({
                            "source": source,
                            "aufnr": aufnr,
                            "reason": "status_detail_mismatch",
                            "missing_status": bool(missing_status),
                            "missing_order_type": bool(missing_order_type),
                            "unmatched_details": unmatched_details,
                        })
                    continue
                if "data.ZSTAT" in changes:
                    transition = f"{(document.get('data') or {}).get('ZSTAT')!s} -> {changes['data.ZSTAT']}"
                    result["transitions"][transition] = result["transitions"].get(transition, 0) + 1
                if "data.AUART" in changes:
                    transition = f"{(document.get('data') or {}).get('AUART')!s} -> {changes['data.AUART']}"
                    result["order_type_transitions"][transition] = result["order_type_transitions"].get(transition, 0) + 1
                if changes:
                    result["would_update"] += 1
                else:
                    result["unchanged"] += 1
                if apply:
                    update = {**changes, "_status_checked_at": checked_at, "_status_run_id": run_id}
                    if changes:
                        update["_status_changed_at"] = checked_at
                    operations.append(UpdateOne(
                        {"_id": document["_id"]}, {"$set": update}, upsert=False,
                    ))
                    changed_in_batch += bool(changes)
                    if len(operations) >= batch_size:
                        flush()
        finally:
            if hasattr(cursor, "close"):
                cursor.close()
        # A single order may contain stale or incomplete status lines in SAP.
        # Keep that order unchanged and continue the batch; transport, schema,
        # lease, and snapshot errors still raise above and fail the task.
        result["partial"] = result["skipped_orders"] > 0
        result["success"] = True
        if apply:
            flush()
            if result["success"]:
                checkpoints = None
                for source, (windows, identity, next_index) in partitions.items():
                    if not identity:
                        continue
                    if checkpoints is None:
                        checkpoints = db[env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints")]
                    for lower, upper in windows:
                        check_leases(verify=True)
                        checkpoints.update_one({"_id": f"{identity}:{lower}:{upper}"}, {"$set": {
                            "source": source, "start": lower.isoformat(), "end": upper.isoformat(),
                            "last_checked_at": checked_at, "prodh_list": required_prodh_list(),
                        }}, upsert=True)
                    check_leases(verify=True)
                    checkpoints.update_one({"_id": identity}, {"$set": {
                        "next_index": next_index, "last_checked_at": checked_at,
                    }}, upsert=True)
    return result


def out_of_scope_query(run_id: str) -> dict[str, Any]:
    return {"source": {"$in": list(SOURCES)}, "_scope_run_id": {"$ne": run_id}}


def cleanup_sales_orders(
    db: Any,
    collection: str,
    *,
    run_id: str,
    start: date,
    end: date,
    replace_collection: bool,
    confirm_delete: bool,
) -> dict[str, Any]:
    query = out_of_scope_query(run_id)
    would_delete = db[collection].count_documents(query)
    written = db[collection].count_documents({"_scope_run_id": run_id})
    result: dict[str, Any] = {
        "deleted_out_of_scope": 0,
        "would_delete_out_of_scope": would_delete,
        "written_in_scope": written,
        "cleanup": "disabled_unverified_coverage",
        "coverage_verified": False,
    }
    return result


def read_watermark(db: Any, collection: str, source: str) -> date | None:
    doc = db[collection].find_one({"_id": sales_checkpoint(source)})
    value = doc.get("last_success_date") if doc else None
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def save_watermark(db: Any, collection: str, source: str, value: date, run_id: str) -> None:
    check_leases(verify=True)
    previous = read_watermark(db, collection, source)
    value = max(value, previous) if previous else value
    db[collection].update_one(
        {"_id": sales_checkpoint(source)},
        {"$set": {"source": "sales_orders", "source_name": source, "prodh_list": required_prodh_list(),
                  "method": METHOD, "last_success_date": value.isoformat(), "updated_at": datetime.now(timezone.utc), "run_id": run_id}},
        upsert=True,
    )


def sales_checkpoint(source: str) -> str:
    return sales_scope_checkpoint_id("sales_orders", source) + ":" + source_hash({
        "url": SOURCES[source], "method": METHOD, "schema": 1,
    })[:16]


def upsert(db: Any, collection: str, documents: list[dict[str, Any]], synced_at: datetime) -> tuple[int, int]:
    if not documents:
        return 0, 0
    coll = db[collection]
    inserted = updated = 0
    batch_size = max(1, int(env("SYNC_BATCH_SIZE", "1000")))
    for start in range(0, len(documents), batch_size):
        batch = documents[start:start + batch_size]
        written = verified_bulk_write(coll, [UpdateOne({"_id": doc["_id"]},
            {"$set": {key: value for key, value in doc.items() if key != "_id"},
             "$setOnInsert": {"first_seen_at": synced_at}, "$inc": {"sync_count": 1}}, upsert=True) for doc in batch])
        inserted += written.upserted_count
        updated += written.modified_count
    return inserted, updated


def sales_source_scope(row: dict[str, Any]) -> str:
    return sales_product_scope(row)


def stage_sales(
    db: Any, http: Any, source: str, url: str, windows: list[tuple[date, date]],
    *, full: bool, dry_run: bool, run_id: str, synced_at: datetime,
) -> dict[str, Any]:
    target = db[env("TARGET_COLLECTION", "sales_orders_sap")]
    dataset = f"sales:{source}"
    stats: dict[str, Any] = {
        "range": {"start": windows[0][0].isoformat(), "end": windows[-1][1].isoformat()},
        "windows": len(windows), "fetched": 0, "unique": 0, "skipped": 0,
        "inserted": 0, "updated": 0, "unchanged": 0, "in_scope": 0, "replaced_existing": 0,
        "out_of_scope": 0, "scope_pending": 0, "read_complete": False,
        "coverage_verified": False, "cleanup_authorized": False, "write_verified": False,
        "http_requests": 0,
    }
    batch_size = int(env("SYNC_BATCH_SIZE", "1000"))
    with Snapshot() as raw, Snapshot() as rows, Snapshot() as planned:
        def admit(row: dict[str, Any]) -> None:
            key = json.dumps(status_line_key(row))
            if not str(row.get("AUFNR") or "").strip():
                raise RuntimeError(f"{source}: 缺少 AUFNR，拒绝写入")
            scope = sales_source_scope(row)
            stats[scope if scope != "pending" else "scope_pending"] += 1
            if scope == "in_scope":
                rows.add(key, row, group_key=str(row["AUFNR"]).strip())

        for start, end in windows:
            check_leases()
            response = http.post(url, json=payload(start, end, required_prodh_list()), headers=request_headers())
            stats["http_requests"] += 1
            response.raise_for_status()
            fetched = validate_records(response.json(), source)
            stats["fetched"] += len(fetched)
            for row in fetched:
                row_key = json.dumps(status_line_key(row))
                raw.add(row_key, row)
        if full and not stats["fetched"] and target.count_documents({"source": source}):
            raise RuntimeError(f"{source}: 空来源且本地已有订单，保留目标和检查点")
        for _, row in raw:
            admit(row)
        stats["read_complete"] = True
        stats["duplicate_source_rows"] = raw.duplicates
        identifiers: list[str] = []

        def plan_batch() -> None:
            existing = {doc["aufnr"]: doc for doc in target.find({
                "_id": {"$in": [f"{source}:{key}" for key in identifiers]},
            })}
            for key in identifiers:
                previous = existing.get(key)
                source_rows = rows.group(key)
                if full:
                    merged = {}
                    if previous:
                        stats["replaced_existing"] += 1
                else:
                    merged = {status_line_key(row): row for row in trusted_sales_lines(previous or {})}
                for row in source_rows:
                    identity = status_line_key(row)
                    merged[identity] = row
                document = aggregate(source, list(merged.values()), synced_at)[0][0]
                digest = source_hash(sorted(merged.values(), key=status_line_key))
                stats["unique"] += 1
                if not full and previous and source_hash(sorted(
                    trusted_sales_lines(previous), key=status_line_key,
                )) == digest:
                    stats["unchanged"] += 1
                    continue
                if full:
                    document["_scope_run_id"] = run_id
                planned.add(key, document)
            identifiers.clear()

        for key in rows.groups():
            identifiers.append(key)
            if len(identifiers) >= batch_size:
                plan_batch()
        if identifiers:
            plan_batch()
        if not dry_run:
            documents = []
            for _, document in planned:
                documents.append(document)
                if len(documents) >= batch_size:
                    inserted, updated = upsert(db, env("TARGET_COLLECTION", "sales_orders_sap"), documents, synced_at)
                    stats["inserted"] += inserted
                    stats["updated"] += updated
                    documents.clear()
            if documents:
                inserted, updated = upsert(db, env("TARGET_COLLECTION", "sales_orders_sap"), documents, synced_at)
                stats["inserted"] += inserted
                stats["updated"] += updated
            clear_scope_pending(db, dataset, (key for key, row in raw if sales_source_scope(row) != "pending"))
            stats["write_verified"] = True
    stats["success"] = True
    stats["scope_backlog"] = db["sync_scope_pending"].count_documents({"dataset": dataset})
    return stats


def sync_sales(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    prodh = configured_prodh(args)
    if args.lookback_days < 0:
        raise ValueError("--lookback-days 不能为负数")
    if int(env("FULL_WINDOW_DAYS", "7")) < 1 or int(env("SYNC_BATCH_SIZE", "1000")) < 1:
        raise ValueError("FULL_WINDOW_DAYS 和 SYNC_BATCH_SIZE 必须大于 0")
    if args.start_date and args.start_date > args.end_date:
        raise ValueError("start-date 不能晚于 end-date")
    if args.full and not args.start_date:
        raise ValueError("--full 必须同时提供 --start-date YYYY-MM-DD")
    if not args.full and args.start_date:
        raise ValueError("增量模式不接受 --start-date；全量请使用 --full --start-date")
    replace_collection = bool(getattr(args, "replace_collection", False))
    confirm_delete = bool(getattr(args, "confirm_delete", False))
    if (replace_collection or confirm_delete) and not args.full:
        raise ValueError("--replace-collection / --confirm-delete 仅用于全量同步")
    if replace_collection and not confirm_delete:
        raise ValueError("--replace-collection 必须同时提供 --confirm-delete")
    if replace_collection:
        raise ValueError("来源未提供完整覆盖证明，不能使用 --replace-collection")

    database_name = env("MONGODB_DATABASE")
    if not database_name:
        raise RuntimeError("MONGODB_DATABASE 未配置")
    target_collection = env("TARGET_COLLECTION", "sales_orders_sap")
    checkpoint_collection = env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    synced_at = datetime.now(timezone.utc)
    client_options = mongo_client_options()
    client = MongoClient(mongo_uri(), **client_options)
    db = client[database_name]
    if not args.dry_run:
        db[target_collection].create_index([("source", ASCENDING), ("aufnr", ASCENDING)], unique=True, name="uniq_source_aufnr")
        db[checkpoint_collection].create_index([("source", ASCENDING)], name="sync_checkpoint_source")

    summary: dict[str, Any] = {"success": True, "run_id": run_id, "dataset": "sales", "mode": "full" if args.full else "incremental", "dry_run": args.dry_run, "target_collection": target_collection, "prodh_list": prodh, "sources": {}, "mongo_write_concern": mongo_write_concern_summary(client_options)}
    try:
        missing: list[str] = []
        ranges: dict[str, tuple[date, date]] = {}
        for source in SOURCES:
            if args.full:
                ranges[source] = (args.start_date, args.end_date)
            else:
                watermark = read_watermark(db, checkpoint_collection, source)
                if watermark is None:
                    missing.append(source)
                else:
                    ranges[source] = (watermark - timedelta(days=args.lookback_days), args.end_date)
        if missing:
            raise RuntimeError(f"缺少来源水位线: {', '.join(missing)}；首次运行请使用 --full --start-date YYYY-MM-DD")

        with httpx.Client(timeout=float(env("HTTP_TIMEOUT", str(DEFAULT_TIMEOUT))), trust_env=False) as http:
            for source, url in SOURCES.items():
                start, end = ranges[source]
                windows = date_windows(start, end, int(env("FULL_WINDOW_DAYS", "7"))) if args.full else [(start, end)]
                try:
                    item = stage_sales(db, http, source, url, windows, full=args.full,
                                       dry_run=args.dry_run, run_id=run_id, synced_at=synced_at)
                except Exception as exc:
                    item = {"success": False, "error": str(exc)}
                    summary["success"] = False
                summary["sources"][source] = item
            if not args.full and summary["success"] and not getattr(args, "skip_status_refresh", False):
                try:
                    summary["status_refresh"] = refresh_order_statuses(
                        db, http,
                        start=getattr(args, "status_start_date", SALES_SCOPE_START),
                        end=args.end_date, prodh=prodh, apply=not args.dry_run, run_id=run_id,
                    )
                    summary["success"] = summary["status_refresh"]["success"]
                    if not summary["success"]:
                        summary["error"] = {
                            "code": "incomplete_status_refresh",
                            "message": "历史订单状态核对未完成，请检查 status_refresh 中的缺失统计",
                            "retryable": False,
                        }
                except Exception as exc:
                    summary["status_refresh"] = {"success": False, "error": str(exc)}
                    summary["error"] = error_payload(exc)
                    summary["success"] = False
        if not args.full and not args.dry_run and summary["success"]:
            for source in SOURCES:
                save_watermark(db, checkpoint_collection, source, args.end_date, run_id)
        if args.full and not args.dry_run and summary["success"]:
            cleanup = cleanup_sales_orders(
                db, target_collection,
                run_id=run_id, start=args.start_date, end=args.end_date,
                replace_collection=replace_collection, confirm_delete=confirm_delete,
            )
            summary.update(cleanup)
            if cleanup.get("error"):
                summary["success"] = False
            if summary["success"]:
                for source in SOURCES:
                    save_watermark(db, checkpoint_collection, source, args.end_date, run_id)
        if not args.dry_run:
            summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
            db[env("SYNC_RUN_COLLECTION", "sync_runs")].insert_one({**summary, "created_at": synced_at})
        return summary
    finally:
        client.close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return sync_sales(args)
    client = MongoClient(mongo_uri(), **mongo_client_options())
    try:
        with resource_lock(client[env("MONGODB_DATABASE")], env("TARGET_COLLECTION", "sales_orders_sap")):
            return sync_sales(args)
    finally:
        client.close()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    install_graceful_shutdown()
    args = build_parser().parse_args()
    try:
        summary = run(args)
        print(json.dumps(summary, ensure_ascii=False, default=str, sort_keys=True))
        return 0 if summary.get("success") is not False else 1
    except Exception as exc:
        return print_failure(exc)


if __name__ == "__main__":
    raise SystemExit(main())
