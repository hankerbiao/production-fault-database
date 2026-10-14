#!/usr/bin/env python3
"""Synchronize SAP sales orders into an independent MongoDB database.

The default mode is incremental.  Each SAP source has its own date watermark;
the watermark is advanced only after that source has fetched and written
successfully. Use ``--full --start-date YYYY-MM-DD`` for the initial load.
Collection-wide cleanup only runs when the full window covers 2026-01-01 through
today, or when ``--replace-collection --confirm-delete`` is passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import quote_plus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))

try:
    import httpx
    from pymongo import ASCENDING, MongoClient, UpdateOne
    from bson.decimal128 import Decimal128
    from bson import BSON
except ImportError as exc:  # pragma: no cover - exercised by deployment, not unit tests
    print(json.dumps({"success": False, "error": f"missing dependency: {exc.name}; install httpx and pymongo"}, ensure_ascii=False))
    raise SystemExit(2) from exc

from scripts.sources.hana.hana_view_sync import (
    env,
    load_dotenv,
    mongo_client_options,
    mongo_write_concern_summary,
    stream_nonempty_field_values,
)


ROOT = PROJECT_ROOT
SOURCES = OrderedDict((
    ("SG", "http://10.2.101.37:8000/sap/ZHTTP_SIMS?sap-client=800"),
    ("KK", "http://10.8.100.11:8001/sap/ZHTTP_SIMS?sap-client=600"),
))
METHOD = "ZSIMS_CL_INBOUND_MO_PSINFO"
ERROR_MSGTYS = {"E", "A", "X"}
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
REPAIR_ID_FIELDS = ("MANDT", "PCODE", "ZWXDT", "ZMCOD1", "ZMCOD2")
REPAIR_VIEW_ID = "ZSGV_ZZT_WLJL"
# The repair view is only in scope from this date onward. This lower bound is
# enforced for both full and incremental runs, even when callers omit a start.
REPAIR_START_DATE = date(2026, 1, 1)
SALES_SCOPE_START = date(2026, 1, 1)
CLEANUP_DELETE_RATIO = 2


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD: {value}") from exc


def parse_prodh(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def mongo_uri() -> str:
    hosts = [item.strip() for item in env("MONGODB_HOSTS").split(",") if item.strip()]
    if not hosts:
        raise RuntimeError("MONGODB_HOSTS 未配置")
    username = env("MONGODB_USERNAME")
    password = env("MONGODB_PASSWORD")
    auth = ""
    if username or password:
        if not username or not password:
            raise RuntimeError("MONGODB_USERNAME 和 MONGODB_PASSWORD 必须同时配置")
        auth = f"{quote_plus(username)}:{quote_plus(password)}@"
    options = ["authSource=" + quote_plus(env("MONGODB_AUTH_SOURCE", env("MONGODB_DATABASE")))]
    replica_set = env("MONGODB_REPLICA_SET")
    if replica_set:
        options.append("replicaSet=" + quote_plus(replica_set))
    return f"mongodb://{auth}{','.join(hosts)}/{quote_plus(env('MONGODB_DATABASE'))}?{'&'.join(options)}"


def request_headers() -> dict[str, str]:
    today = datetime.now().strftime("%Y%m%d")
    signature = hashlib.md5(f"sugon{METHOD}{today}sugon".encode("utf-8")).hexdigest().upper()
    return {"Content-Type": "application/json", "method": METHOD, "sign": signature, "time": today}


def current_date() -> date:
    return date.today()


def is_empty_sap_result(body: Mapping[str, Any]) -> bool:
    message = str(body.get("MSGTX") or "").strip().lower()
    return "no data" in message


def covers_sales_full_scope(start: date, end: date, *, today: date | None = None) -> bool:
    today = today or current_date()
    return start <= SALES_SCOPE_START and end >= today


def validate_records(body: Any, source: str) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        raise RuntimeError(f"{source}: 响应不是 JSON 对象")
    msgty = str(body.get("MSGTY") or "").upper()
    if msgty in ERROR_MSGTYS:
        if is_empty_sap_result(body):
            return []
        raise RuntimeError(f"{source}: SAP error ({msgty}): {body.get('MSGTX') or 'Unknown SAP error'}")
    records = body.get("DATA")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise RuntimeError(f"{source}: 响应缺少有效 DATA 数组")
    return records


def number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def bson_value(value: Any) -> Any:
    if isinstance(value, Decimal128):
        return value
    if hasattr(value, "as_tuple") and value.__class__.__name__ == "Decimal":
        return Decimal128(str(value))
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {str(key): bson_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [bson_value(item) for item in value]
    return value


def source_key(row: dict[str, Any], fields: tuple[str, ...]) -> str:
    values = [str(row.get(field) or "").strip() for field in fields]
    if not all(values):
        payload = json.dumps(sorted(row.items()), ensure_ascii=False, default=str, separators=(",", ":"))
        return "full_row:" + hashlib.sha256(payload.encode()).hexdigest()
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def watermark_max(current: Any, row: dict[str, Any], fields: tuple[str, ...]) -> Any:
    candidate = tuple(str(row.get(field) or "").strip() for field in fields)
    if not all(candidate):
        return current
    if current is None or candidate > tuple(str(current.get(field) or "") for field in fields):
        return dict(zip(fields, candidate, strict=True))
    return current


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


def repair_query(mode: str, checkpoint: dict[str, Any] | None, start_date: date | None, end_date: date) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if start_date:
        conditions.append('"ZDATE_WX" >= ?')
        params.append(start_date.strftime("%Y%m%d"))
    conditions.append('"ZDATE_WX" <= ?')
    params.append(end_date.strftime("%Y%m%d"))
    selected = ", ".join(f'"{field}"' for field in REPAIR_COLUMNS)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    order = ", ".join(f'"{field}"' for field in REPAIR_ID_FIELDS)
    return f"SELECT {selected} FROM {REPAIR_VIEW}{where} ORDER BY {order}", params


def repair_scope_filter(start_date: date, end_date: date) -> dict[str, Any]:
    return {
        "_source_view": REPAIR_VIEW_ID,
        "ZDATE_WX": {"$gte": start_date.strftime("%Y%m%d"), "$lte": end_date.strftime("%Y%m%d")},
    }


def repair_cleanup_filter(mode: str, run_id: str, start_date: date, end_date: date) -> dict[str, Any]:
    return {**repair_scope_filter(start_date, end_date), "_scope_run_id": {"$ne": run_id}}


def repair_window_start(end_date: date) -> date:
    month = end_date.year * 12 + end_date.month - 3
    return max(date(month // 12, month % 12 + 1, 1), REPAIR_START_DATE)


def repair_identity(row: Mapping[str, Any]) -> str:
    values = [str(row.get(field) or "").strip() for field in REPAIR_ID_FIELDS]
    if not all(values[:3]):
        raise ValueError("维修记录缺少稳定身份字段 MANDT/PCODE/ZWXDT，拒绝提交快照")
    try:
        if len(values[2]) != 14 or not values[2].isdigit():
            raise ValueError
        datetime.strptime(values[2], "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise ValueError("维修记录 ZWXDT 必须为有效的 14 位维修日期时间") from exc
    # Empty part serials are valid; never fall back to a mutable whole-row hash.
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return "repair:v2:" + hashlib.sha256(payload.encode()).hexdigest()


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
                        stats["batches"] += 1
                        for values in rows:
                            row = dict(zip(REPAIR_COLUMNS, values, strict=True))
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


def validate_repair_cleanup(existing_count: int, stale_count: int, unique_rows: int, allow_large_cleanup: bool) -> None:
    if allow_large_cleanup:
        return
    if existing_count and not unique_rows:
        raise RuntimeError("HANA 快照为空且本地窗口有数据，已阻断清理；核实后可用 --allow-large-cleanup")
    if stale_count > 100 and stale_count > existing_count / 2:
        raise RuntimeError("本次将清理超过 100 条且超过窗口一半的数据，已阻断；核实后可用 --allow-large-cleanup")


def is_complete_month(start_date: date, end_date: date) -> bool:
    next_month = date(start_date.year + (start_date.month == 12), start_date.month % 12 + 1, 1)
    return start_date.day == 1 and end_date + timedelta(days=1) == next_month


def sales_order_vbelns(db: Any) -> set[str]:
    """Return exact non-empty sales-order numbers accepted for repairs."""
    return stream_nonempty_field_values(
        db[env("TARGET_COLLECTION", "sales_orders_sap")], "data.VBELN"
    )


@contextmanager
def hana_connection() -> Iterator[Any]:
    try:
        from hdbcli import dbapi
    except ImportError as exc:
        raise RuntimeError("缺少 hdbcli，请使用源项目 backend/.venv/bin/python 或安装 hdbcli") from exc
    required = ("HANA_ADDRESS", "HANA_USER", "HANA_PASSWORD")
    missing = [name for name in required if not env(name)]
    if missing:
        raise RuntimeError("缺少 HANA 配置: " + ", ".join(missing))
    connection = dbapi.connect(
        address=env("HANA_ADDRESS"), port=int(env("HANA_PORT", "30015")),
        user=env("HANA_USER"), password=env("HANA_PASSWORD"),
        communicationTimeout=int(env("HANA_CONNECT_TIMEOUT", "120")) * 1000,
    )
    try:
        yield connection
    finally:
        connection.close()


def sync_repair(db: Any, collection: str, checkpoint_collection: str, mode: str, start_date: date | None, end_date: date, lookback_days: int, batch_size: int, dry_run: bool, run_id: str, defer_finalize: bool = False, *, allow_large_cleanup: bool = False) -> dict[str, Any]:
    if mode not in {"incremental", "full", "monthly"}:
        raise ValueError("未知维修同步模式")
    if batch_size < 1 or not run_id:
        raise ValueError("维修同步必须提供有效 batch_size 和 run_id")
    if end_date < REPAIR_START_DATE:
        raise ValueError("维修数据仅同步 2026-01-01 及之后的数据")
    if mode == "monthly" and (start_date is None or not is_complete_month(start_date, end_date)):
        raise ValueError("按月同步必须提供完整自然月的起止日期")
    if mode == "incremental":
        start_date = repair_window_start(end_date)
    start_date = max(start_date or REPAIR_START_DATE, REPAIR_START_DATE)
    if start_date > end_date:
        raise ValueError("维修同步起始日期不能晚于结束日期")
    allowed_vbelns = sales_order_vbelns(db)
    if not allowed_vbelns:
        raise RuntimeError("sales_orders_sap 中没有有效 VBELN，拒绝同步维修数据")
    sql, params = repair_query(mode, None, start_date, end_date)
    stats: dict[str, Any] = {
        "success": False, "mode": mode, "strategy": "rolling_snapshot" if mode == "incremental" else "snapshot",
        "run_id": run_id, "snapshot_complete": False, "source_rows": 0, "unique_rows": 0,
        "duplicate_source_rows": 0, "matched_sales_orders": 0, "empty_sales_orders_retained": 0,
        "unmatched_sales_orders_retained": 0, "filtered_missing_sales_order": 0, "batches": 0,
        "inserted": 0, "updated": 0, "unchanged": 0, "relinked": 0, "would_insert": 0, "would_update": 0,
        "sales_order_vbelns": len(allowed_vbelns), "allow_large_cleanup": allow_large_cleanup,
        "range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
    }
    target = db[collection]
    with repair_snapshot(sql, params, batch_size, stats) as snapshot:
        existing_by_identity: dict[str, list[dict[str, Any]]] = {}
        existing_count = 0
        cursor = target.find(repair_scope_filter(start_date, end_date))
        try:
            for document in cursor:
                existing_count += 1
                try:
                    identity = repair_identity(document)
                except ValueError:
                    continue
                existing_by_identity.setdefault(identity, []).append(document)
        finally:
            if hasattr(cursor, "close"):
                cursor.close()
        reused = sum(identity in existing_by_identity for identity, in snapshot.execute("SELECT identity FROM repairs"))
        stats["stale_candidates"] = existing_count - reused
        validate_repair_cleanup(existing_count, stats["stale_candidates"], stats["unique_rows"], allow_large_cleanup)
        if not dry_run:
            target.create_index("_repair_id", unique=True, name="repair_identity_unique",
                                partialFilterExpression={"_repair_id": {"$type": "string"}})
            target.create_index("_source_key", unique=True, name="source_key_unique")
            target.create_index([("_source_view", ASCENDING), ("ZDATE_WX", ASCENDING), ("_scope_run_id", ASCENDING)],
                                name="repair_snapshot_cleanup")
            target.create_index("_legacy_ids", name="repair_legacy_ids")
        operations = []
        for identity, payload, content_hash, legacy_key in snapshot.execute("SELECT * FROM repairs ORDER BY identity"):
            row = BSON(payload).decode()
            normalized_vbeln = str(row.get("VBELN") or "").strip()
            if not normalized_vbeln:
                stats["empty_sales_orders_retained"] += 1
            elif normalized_vbeln in allowed_vbelns:
                stats["matched_sales_orders"] += 1
            else:
                stats["unmatched_sales_orders_retained"] += 1
            previous = existing_by_identity.get(identity, [])
            canonical = max(previous, key=lambda doc: (
                doc.get("_repair_id") == identity, str(doc.get("_synced_at") or ""), str(doc["_id"]),
            )) if previous else None
            changed = canonical is None or canonical.get("_source_hash") != content_hash
            relinked = canonical is not None and canonical.get("_repair_id") != identity
            stats["would_insert"] += canonical is None
            stats["would_update"] += canonical is not None and changed
            stats["unchanged"] += canonical is not None and not changed
            stats["relinked"] += relinked
            if dry_run:
                continue
            now = datetime.now(timezone.utc)
            aliases = {legacy_key}
            for document in previous:
                aliases.update(document.get("_legacy_ids") or [])
                aliases.add(str(document["_id"]))
                if document.get("_source_key"):
                    aliases.add(document["_source_key"])
            metadata: dict[str, Any] = {
                "_repair_id": identity, "_source_key": identity, "_source_view": REPAIR_VIEW_ID,
                "_source_identity_version": 2, "_source_hash": content_hash,
                "_scope_run_id": run_id, "_sync_run_id": run_id, "_last_seen_at": now,
                "_legacy_ids": sorted(aliases),
            }
            if changed:
                source_orders = {field: row[field] for field in ("AUFNR", "VBELN")}
                if canonical:
                    previous_orders = canonical.get("_source_orders") or {}
                    for field in ("AUFNR", "VBELN"):
                        if not row[field] and not previous_orders.get(field):
                            row[field] = canonical.get(field, "")
                    if row["AUFNR"] != canonical.get("AUFNR", ""):
                        row["GSTRS"] = ""
                        row["VBELN"] = source_orders["VBELN"]
                metadata.update(row)
                metadata.update({"_source_orders": source_orders, "_synced_at": now})
            if canonical and "_first_seen_at" not in canonical:
                metadata["_first_seen_at"] = canonical.get("_synced_at") or now
            query = {"_id": canonical["_id"]} if canonical else {"_repair_id": identity}
            update: dict[str, Any] = {"$set": metadata}
            if canonical is None:
                update["$setOnInsert"] = {"_id": hashlib.sha256(identity.encode()).hexdigest(), "_first_seen_at": now}
            operations.append(UpdateOne(query, update, upsert=canonical is None))
            stats["inserted"] += canonical is None
            stats["updated"] += canonical is not None and changed
            if len(operations) >= batch_size:
                target.bulk_write(operations, ordered=False)
                operations.clear()
        if operations:
            target.bulk_write(operations, ordered=False)
    stats["success"] = True
    if not dry_run and not defer_finalize:
        stats.update(finalize_repair_run(db, collection, checkpoint_collection, stats, run_id))
    return stats


def finalize_repair_run(db: Any, collection: str, checkpoint_collection: str, sync_result: Mapping[str, Any], run_id: str) -> dict[str, int]:
    """Archive stale window records only after the validated workflow succeeds."""
    if not sync_result.get("success") or not sync_result.get("snapshot_complete") or sync_result.get("run_id") != run_id:
        raise ValueError("不能提交未完整校验或 run_id 不匹配的维修快照")
    sync_range = sync_result["range"]
    start_date = date.fromisoformat(sync_range["start"])
    end_date = date.fromisoformat(sync_range["end"])
    target = db[collection]
    covered_count = target.count_documents({
        **repair_scope_filter(start_date, end_date), "_scope_run_id": run_id,
    })
    if covered_count != sync_result["unique_rows"]:
        raise RuntimeError("本次维修快照尚未完整写入，拒绝清理旧记录")
    query = repair_cleanup_filter(str(sync_result["mode"]), run_id, start_date, end_date)
    stale_count = target.count_documents(query)
    existing_count = target.count_documents(repair_scope_filter(start_date, end_date))
    validate_repair_cleanup(existing_count, stale_count, sync_result["unique_rows"], sync_result.get("allow_large_cleanup", False))
    archive = db[collection + "_archive"]
    deleted = 0
    documents: list[dict[str, Any]] = []

    def archive_batch() -> None:
        nonlocal deleted
        if not documents:
            return
        archive.bulk_write([
            UpdateOne({"_id": f"{run_id}:{doc['_id']}"}, {"$setOnInsert": {
                "document": doc, "run_id": run_id, "archived_at": datetime.now(timezone.utc),
                "reason": "not_in_repair_snapshot", "range": sync_range,
            }}, upsert=True)
            for doc in documents
        ], ordered=False)
        deleted += target.delete_many({**query, "_id": {"$in": [doc["_id"] for doc in documents]}}).deleted_count
        documents.clear()

    cursor = target.find(query)
    try:
        for document in cursor:
            documents.append(document)
            if len(documents) >= 1000:
                archive_batch()
        archive_batch()
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    return {"deleted_out_of_scope": deleted}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SAP SG/KK 销售订单同步到独立 MongoDB")
    parser.add_argument("--full", action="store_true", help="执行全量日期范围同步；必须同时提供 --start-date")
    parser.add_argument("--start-date", type=parse_date, help="全量起始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", type=parse_date, default=date.today(), help="结束日期，默认今天")
    parser.add_argument("--lookback-days", type=int, default=int(env("SYNC_LOOKBACK_DAYS", "7")), help="增量回看天数，默认 .env 中的值")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prodh-list", nargs="+", help="覆盖产品层次过滤，例如 --prodh-list 00100 00200")
    group.add_argument("--all-prodh", action="store_true", help="取消产品层次过滤")
    parser.add_argument("--dry-run", action="store_true", help="只请求和统计，不写入订单及水位线")
    parser.add_argument("--replace-collection", action="store_true", help="全量成功后强制删除本次未覆盖的 SG/KK 订单；必须同时提供 --confirm-delete")
    parser.add_argument("--confirm-delete", action="store_true", help="确认删除范围外旧订单；覆盖安全闸或配合 --replace-collection")
    return parser


def configured_prodh(args: argparse.Namespace) -> list[str] | None:
    if args.all_prodh:
        return None
    if args.prodh_list:
        return args.prodh_list
    raw = env("SOURCE_PRODH_LIST", "00100")
    return parse_prodh(raw) or None


def payload(start: date, end: date, prodh: list[str] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"CHDAT": start.isoformat(), "CHDAT_TO": end.isoformat()}
    if prodh:
        body["PRODH_LIST"] = prodh
    return body


def date_windows(start: date, end: date, days: int) -> list[tuple[date, date]]:
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=days - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


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
        "cleanup": "skipped_partial_range",
    }
    replace = replace_collection or covers_sales_full_scope(start, end)
    if not replace:
        return result
    dangerous = would_delete > 0 and (written == 0 or would_delete > max(written, 1) * CLEANUP_DELETE_RATIO)
    if dangerous and not confirm_delete:
        result["cleanup"] = "aborted"
        result["error"] = (
            f"将删除 {would_delete} 条范围外订单，但本次仅写入 {written} 条；已拒绝清理。"
            f"请使用 --full --start-date {SALES_SCOPE_START.isoformat()} 且结束日期覆盖今天，"
            "或显式传入 --replace-collection --confirm-delete"
        )
        return result
    result["deleted_out_of_scope"] = db[collection].delete_many(query).deleted_count
    result["cleanup"] = "replaced"
    return result


def read_watermark(db: Any, collection: str, source: str) -> date | None:
    doc = db[collection].find_one({"_id": f"sales_orders:{source}"})
    value = doc.get("last_success_date") if doc else None
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def save_watermark(db: Any, collection: str, source: str, value: date, run_id: str) -> None:
    db[collection].update_one(
        {"_id": f"sales_orders:{source}"},
        {"$set": {"source": "sales_orders", "source_name": source, "last_success_date": value.isoformat(), "updated_at": datetime.now(timezone.utc), "run_id": run_id}},
        upsert=True,
    )


def upsert(db: Any, collection: str, documents: list[dict[str, Any]], synced_at: datetime) -> tuple[int, int]:
    if not documents:
        return 0, 0
    coll = db[collection]
    ids = [doc["_id"] for doc in documents]
    existing = {doc["_id"] for doc in coll.find({"_id": {"$in": ids}}, {"_id": 1})}
    operations = []
    for doc in documents:
        operations.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {key: value for key, value in doc.items() if key != "_id"}, "$setOnInsert": {"first_seen_at": synced_at}, "$inc": {"sync_count": 1}},
            upsert=True,
        ))
    coll.bulk_write(operations, ordered=False)
    return len(ids) - len(existing), len(existing)


def sync_sales(args: argparse.Namespace) -> dict[str, Any]:
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

    database_name = env("MONGODB_DATABASE")
    if not database_name:
        raise RuntimeError("MONGODB_DATABASE 未配置")
    target_collection = env("TARGET_COLLECTION", "sales_orders_sap")
    checkpoint_collection = env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    synced_at = datetime.now(timezone.utc)
    prodh = configured_prodh(args)
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
                item: dict[str, Any] = {"range": {"start": start.isoformat(), "end": end.isoformat()}, "windows": len(windows), "fetched": 0, "unique": 0, "skipped": 0, "inserted": 0, "updated": 0}
                try:
                    for window_start, window_end in windows:
                        response = http.post(url, json=payload(window_start, window_end, prodh), headers=request_headers())
                        response.raise_for_status()
                        records = validate_records(response.json(), source)
                        documents, stats = aggregate(source, records, synced_at)
                        for key in ("fetched", "unique", "skipped"):
                            item[key] += stats[key]
                        if not args.dry_run:
                            if args.full:
                                for document in documents:
                                    document["_scope_run_id"] = run_id
                            inserted, updated = upsert(db, target_collection, documents, synced_at)
                            item["inserted"] += inserted
                            item["updated"] += updated
                    if not args.dry_run:
                        save_watermark(db, checkpoint_collection, source, end, run_id)
                    item["success"] = True
                except Exception as exc:
                    item.update({"success": False, "error": str(exc), "inserted": 0, "updated": 0})
                    summary["success"] = False
                summary["sources"][source] = item
        if args.full and not args.dry_run and summary["success"]:
            cleanup = cleanup_sales_orders(
                db, target_collection,
                run_id=run_id, start=args.start_date, end=args.end_date,
                replace_collection=replace_collection, confirm_delete=confirm_delete,
            )
            summary.update(cleanup)
            if cleanup.get("error"):
                summary["success"] = False
        if not args.dry_run:
            db[env("SYNC_RUN_COLLECTION", "sync_runs")].insert_one({**summary, "created_at": synced_at})
        return summary
    finally:
        client.close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    return sync_sales(args)


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args()
    try:
        summary = run(args)
        print(json.dumps(summary, ensure_ascii=False, default=str, sort_keys=True))
        return 0 if summary.get("success") is not False else 1
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
