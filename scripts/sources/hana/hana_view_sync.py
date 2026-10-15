#!/usr/bin/env python3
"""Shared HANA-to-MongoDB synchronization primitives for documented views."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import quote_plus
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))

try:
    from pymongo import MongoClient, UpdateOne
    from pymongo.errors import OperationFailure
except ImportError as exc:  # pragma: no cover - deployment error
    raise SystemExit(f"missing dependency: {exc.name}; install pymongo") from exc


ROOT = PROJECT_ROOT
CHECKPOINT_PREFIX = "hana_views:"

from scripts.sync_runtime import (
    Snapshot, cleanup_records, check_leases, validate_snapshot_cleanup,
)


def install_graceful_shutdown() -> None:
    def handle_shutdown(signum: int, _frame: Any) -> None:
        raise RuntimeError(f"收到终止信号 {signum}，正在关闭数据库连接")

    signal.signal(signal.SIGTERM, handle_shutdown)


def progress_log(message: str) -> None:
    print(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}] {message}", file=sys.stderr, flush=True)


def ensure_index(collection: Any, keys: str | Sequence[tuple[str, int]], *, name: str, unique: bool = False) -> str:
    """Create an index, reusing a legacy index with the same key pattern.

    MongoDB rejects equivalent indexes when only their names differ.  This is
    common when the backend creates default names (for example ``SERNR_1``)
    before a sync script asks for a descriptive name.  Reuse is allowed only
    when the key pattern matches and, for unique indexes, uniqueness is also
    preserved.
    """
    key_items = [(keys, 1)] if isinstance(keys, str) else list(keys)
    try:
        return collection.create_index(key_items if len(key_items) > 1 else key_items[0][0], name=name, unique=unique)
    except OperationFailure as exc:
        if exc.code != 85:
            raise
        for existing in collection.list_indexes():
            existing_keys = list(existing.get("key", {}).items())
            if existing_keys != key_items:
                continue
            if unique and not existing.get("unique", False):
                break
            return str(existing.get("name", name))
        raise


@dataclass(frozen=True)
class ViewSpec:
    view_id: str
    qualified_name: str
    columns: tuple[str, ...]
    collection: str
    key_fields: tuple[str, ...]
    order_by: tuple[str, ...]
    watermark_fields: tuple[str, ...] = ()
    index_fields: tuple[str, ...] = ()
    start_date_field: str | None = None
    fallback_to_full_row: bool = False


def fields(value: str) -> tuple[str, ...]:
    return tuple(value.split())


VIEW_SPECS: dict[str, ViewSpec] = {
    "ZSGV_ZSD124": ViewSpec(
        view_id="ZSGV_ZSD124",
        qualified_name='"_SYS_BIC"."BW_LOCAL.BI/ZSGV_ZSD124_BI"',
        columns=fields(
            "MANDT WERKS MATNR BWART BUDAT_MKPF LGORT MENGE_A KUNNR_A MAKTX NAME1_A "
            "TYPE VGBEL_A VGPOS_A MATNR_SC MATKX_SC2 AUFNR_1 POSNR_EX VBELN_EX KUNNR_EX "
            "NAME1_EX AUART KUNNR_1 NAME1_X MAKTX_CP MATNR_CP PSMNG MATKL CXFLG WGBEZ CPX "
            "BU MATNR_BI ZSTAT MBLNR MJAHR ZEILE FDATU_O DATE_JH_O ETENR_O MAT_KDAUF "
            "MAT_KDPOS VGBEL VGPOS KUNNR NAME1 MENGE USNAM_MKPF VSNMR_V ZNAM"
        ),
        collection="order_bom_postings_sap",
        key_fields=("MANDT", "MBLNR", "MJAHR", "ZEILE"),
        order_by=("MBLNR", "MJAHR", "ZEILE", "WERKS", "MATNR"),
        watermark_fields=("BUDAT_MKPF",),
        index_fields=("AUFNR_1", "VBELN_EX", "MATNR", "FDATU_O"),
        start_date_field="BUDAT_MKPF",
    ),
    "ZSGV_ZPP_SERNOLIST": ViewSpec(
        view_id="ZSGV_ZPP_SERNOLIST",
        qualified_name='"_SYS_BIC"."BW_LOCAL.PP/ZSGV_ZPP_SERNOLIST"',
        columns=fields("ZCODE_HEAD ZCODE_ITEM AUFNR_HEAD AUFNR_ITEM PRODH"),
        collection="serial_bindings_sap",
        key_fields=("ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM", "PRODH"),
        order_by=("ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM"),
        index_fields=("ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM"),
        fallback_to_full_row=True,
    ),
    "Z_V_ZMES_T_001": ViewSpec(
        view_id="Z_V_ZMES_T_001",
        qualified_name='"_SYS_BIC"."BW_LOCAL.ZTRRI/Z_V_ZMES_T_001"',
        columns=fields(
            "MANDT HISTROYID PCODE OCODE AUFNR KDAUF KDPOS LGORT MAKTX_TH LEAD_CYCLE AUFNR_CYCLE "
            "CUSTOMIZE_CYCLE OEMBZ SPEC OPERATION SPEC_DESC UNAME LASTSPEC_TIME SPEC_TIME GSTRS "
            "PLAN_END_TIME ACTUAL_START_TIME ACTUAL_END_TIME HADE1 HADE2 HADE3 HADE4 HADE5 HADE6 "
            "HADE7 HADE8 STATU MESS"
        ),
        collection="station_records_sap",
        key_fields=("HISTROYID", "SPEC", "SPEC_TIME", "PCODE", "OCODE"),
        order_by=("HISTROYID", "SPEC_TIME", "PCODE", "SPEC"),
        watermark_fields=("ACTUAL_START_TIME",),
        index_fields=("PCODE", "ACTUAL_START_TIME", "AUFNR", "SPEC"),
        start_date_field="ACTUAL_START_TIME",
        fallback_to_full_row=True,
    ),
}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def mongo_client_options() -> dict[str, Any]:
    """Build optional MongoDB write-concern settings from the environment."""
    options: dict[str, Any] = {
        "serverSelectionTimeoutMS": int(env("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "10000")),
    }
    if raw_w := env("MONGODB_WRITE_CONCERN_W"):
        if raw_w.lower() == "majority":
            options["w"] = "majority"
        else:
            try:
                w = int(raw_w)
            except ValueError as exc:
                raise RuntimeError("MONGODB_WRITE_CONCERN_W 必须为正整数或 majority") from exc
            if w < 1:
                raise RuntimeError("MONGODB_WRITE_CONCERN_W 必须大于 0")
            options["w"] = w
    if raw_journal := env("MONGODB_JOURNALED"):
        normalized = raw_journal.lower()
        if normalized not in {"true", "false"}:
            raise RuntimeError("MONGODB_JOURNALED 必须为 true 或 false")
        options["journal"] = normalized == "true"
    if raw_timeout := env("MONGODB_WRITE_TIMEOUT_MS"):
        try:
            timeout = int(raw_timeout)
        except ValueError as exc:
            raise RuntimeError("MONGODB_WRITE_TIMEOUT_MS 必须为非负整数") from exc
        if timeout < 0:
            raise RuntimeError("MONGODB_WRITE_TIMEOUT_MS 必须为非负整数")
        options["wTimeoutMS"] = timeout
    return options


def mongo_write_concern_summary(options: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "w": options.get("w", "server_default"),
        "journal": options.get("journal", "server_default"),
        "timeout_ms": options.get("wTimeoutMS", "server_default"),
    }


def stream_nonempty_field_values(collection: Any, field_path: str) -> set[str]:
    """Read distinct-like string values without MongoDB's 16MB command limit."""
    path = field_path.split(".")
    cursor = collection.find({}, {field_path: 1})
    values: set[str] = set()
    try:
        for document in cursor:
            value: Any = document
            for part in path:
                value = value.get(part) if isinstance(value, Mapping) else None
            normalized = str(value or "").strip()
            if normalized:
                values.add(normalized)
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    return values


def mongo_uri() -> str:
    if uri := env("MONGODB_URI"):
        return uri
    hosts = [item.strip() for item in env("MONGODB_HOSTS").split(",") if item.strip()]
    if not hosts:
        raise RuntimeError("MONGODB_HOSTS 或 MONGODB_URI 未配置")
    username, password = env("MONGODB_USERNAME"), env("MONGODB_PASSWORD")
    auth = f"{quote_plus(username)}:{quote_plus(password)}@" if username or password else ""
    if bool(username) != bool(password):
        raise RuntimeError("MONGODB_USERNAME 和 MONGODB_PASSWORD 必须同时配置")
    database = env("MONGODB_DATABASE")
    if not database:
        raise RuntimeError("MONGODB_DATABASE 未配置")
    options = ["authSource=" + quote_plus(env("MONGODB_AUTH_SOURCE", database))]
    if replica_set := env("MONGODB_REPLICA_SET"):
        options.append("replicaSet=" + quote_plus(replica_set))
    return f"mongodb://{auth}{','.join(hosts)}/{quote_plus(database)}?{'&'.join(options)}"


@contextmanager
def hana_connection() -> Iterator[Any]:
    try:
        from hdbcli import dbapi
    except ImportError as exc:  # pragma: no cover - deployment error
        raise RuntimeError("缺少 hdbcli，请使用已安装 HANA 驱动的 Python 环境") from exc
    required = ("HANA_ADDRESS", "HANA_USER", "HANA_PASSWORD")
    missing = [name for name in required if not env(name)]
    if missing:
        raise RuntimeError("缺少 HANA 配置: " + ", ".join(missing))
    connection = dbapi.connect(
        address=env("HANA_ADDRESS"),
        port=int(env("HANA_PORT", "30015")),
        user=env("HANA_USER"),
        password=env("HANA_PASSWORD"),
        communicationTimeout=int(env("HANA_CONNECT_TIMEOUT", "120")) * 1000,
    )
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def process_lock(path: str) -> Iterator[None]:
    lock_path = Path(path).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"已有同步任务运行中: {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def normalize_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    value = str(value).strip()
    return value or None


def bson_value(value: Any) -> Any:
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if hasattr(value, "as_tuple") and value.__class__.__name__ == "Decimal":
        from bson.decimal128 import Decimal128

        return Decimal128(str(value))
    if isinstance(value, Mapping):
        return {str(key): bson_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [bson_value(item) for item in value]
    return value


def source_key(row: Mapping[str, Any], spec: ViewSpec) -> str:
    values = [normalize_value(row.get(field)) for field in spec.key_fields]
    if all(value is not None for value in values):
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if not spec.fallback_to_full_row:
        missing = [field for field, value in zip(spec.key_fields, values) if value is None]
        raise RuntimeError(f"{spec.view_id} 缺少业务键字段: {', '.join(missing)}")
    payload = json.dumps(
        [(field, normalize_value(row.get(field))) for field in spec.columns],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "full_row:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def date_overlap(value: Any, days: int) -> Any:
    normalized = normalize_value(value)
    if days > 0 and normalized and len(normalized) == 8 and normalized.isdigit():
        try:
            return (datetime.strptime(normalized, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
        except ValueError:
            pass
    if days > 0 and normalized:
        try:
            return (datetime.fromisoformat(normalized) - timedelta(days=days)).isoformat(sep=" ")
        except ValueError:
            pass
    return value


def build_query(spec: ViewSpec, checkpoint: Mapping[str, Any] | None, mode: str, start_date: str | None, lookback_days: int, end_date: str | None = None) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    watermark = checkpoint.get("watermark") if checkpoint else None
    if mode == "incremental" and watermark is not None and spec.watermark_fields:
        field = spec.watermark_fields[0]
        conditions.append(f'"{field}" >= ?')
        params.append(date_overlap(watermark, lookback_days))
    if start_date and spec.start_date_field:
        value = start_date
        if spec.start_date_field == "ACTUAL_START_TIME":
            value = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]} 00:00:00"
        conditions.append(f'"{spec.start_date_field}" >= ?')
        params.append(value)
    if end_date and spec.start_date_field:
        value = end_date.replace("-", "")
        if spec.start_date_field == "ACTUAL_START_TIME":
            value = f"{end_date.replace('-', '')[:4]}-{end_date.replace('-', '')[4:6]}-{end_date.replace('-', '')[6:8]} 23:59:59"
        conditions.append(f'"{spec.start_date_field}" <= ?')
        params.append(value)
    selected = ", ".join(f'"{field}"' for field in spec.columns)
    order = ", ".join(f'"{field}"' for field in spec.order_by)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return f"SELECT {selected} FROM {spec.qualified_name}{where} ORDER BY {order}", params


def max_watermark(spec: ViewSpec, current: Any, row: Mapping[str, Any]) -> Any:
    if not spec.watermark_fields:
        return current
    value = normalize_watermark(spec, row.get(spec.watermark_fields[0]))
    current = normalize_watermark(spec, current)
    if value is None or current is not None and value <= current:
        return current
    return value


def normalize_watermark(spec: ViewSpec, value: Any) -> str | None:
    normalized = normalize_value(value)
    if normalized and spec.start_date_field == "ACTUAL_START_TIME":
        return datetime.fromisoformat(normalized).isoformat(sep=" ")
    return normalized


def configured_spec(view_id: str) -> ViewSpec:
    spec = VIEW_SPECS[view_id]
    collection_env = {
        "ZSGV_ZSD124": "BOM_COLLECTION", "Z_V_ZMES_T_001": "STATION_COLLECTION",
        "ZSGV_ZPP_SERNOLIST": "SERIAL_COLLECTION",
    }
    return replace(spec, collection=env(collection_env[view_id], spec.collection))


def view_scope(spec: ViewSpec, start: str | None, end: str | None) -> dict[str, Any]:
    query: dict[str, Any] = {"_source_view": spec.view_id}
    if spec.start_date_field and (start or end):
        bounds = {}
        for op, value in (("$gte", start), ("$lte", end)):
            if value:
                compact = value.replace("-", "")
                bounds[op] = (
                    f"{compact[:4]}-{compact[4:6]}-{compact[6:8]} "
                    + ("00:00:00" if op == "$gte" else "23:59:59")
                ) if spec.start_date_field == "ACTUAL_START_TIME" else compact
        query[spec.start_date_field] = bounds
    return query


def sync_view(
    spec: ViewSpec,
    *,
    mode: str,
    start_date: str | None,
    batch_size: int,
    lookback_days: int,
    dry_run: bool,
    defer_finalize: bool = False,
    end_date: str | None = None,
    preview_snapshot: Snapshot | None = None,
    allow_large_cleanup: bool = False,
) -> dict[str, Any]:
    if batch_size < 1 or lookback_days < 0:
        raise ValueError("batch-size 必须大于 0，lookback-days 不能为负数")
    run_id = uuid4().hex
    stats: dict[str, Any] = {
        "success": False,
        "view_id": spec.view_id,
        "collection": spec.collection,
        "mode": mode,
        "run_id": run_id,
        "source_rows": 0,
        "batches": 0,
        "upserted": 0,
        "matched": 0,
        "modified": 0,
        "watermark": None,
        "finalized": False,
        "duplicate_source_keys": 0,
        "allow_large_cleanup": allow_large_cleanup,
    }
    mongo_client = None
    database = None
    run_recorded = False
    stats["range"] = {"start": start_date, "end": end_date}
    stats["snapshot_complete"] = False
    try:
        progress_log(f"[{spec.view_id}] 同步开始：mode={mode} batch_size={batch_size} dry_run={dry_run}")
        client_options = mongo_client_options()
        stats["mongo_write_concern"] = mongo_write_concern_summary(client_options)
        mongo_client = MongoClient(mongo_uri(), **client_options)
        database = mongo_client[env("MONGODB_DATABASE")]
        collection = database[spec.collection]
        checkpoints = database[env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints")]
        checkpoint = checkpoints.find_one({"_id": CHECKPOINT_PREFIX + spec.view_id})
        sql, params = build_query(spec, checkpoint, mode, start_date, lookback_days, end_date)
        watermark = None
        with Snapshot() as snapshot:
            with hana_connection() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute(sql, tuple(params))
                    while rows := cursor.fetchmany(batch_size):
                        check_leases()
                        for values in rows:
                            row = dict(zip(spec.columns, values, strict=True))
                            watermark = max_watermark(spec, watermark, row)
                            snapshot.add(source_key(row, spec), {field: bson_value(value) for field, value in row.items()})
                            stats["source_rows"] += 1
                        stats["batches"] += 1
                        if stats["batches"] == 1 or stats["batches"] % 100 == 0:
                            progress_log(f"[{spec.view_id}] 读取进度：batches={stats['batches']:,} rows={stats['source_rows']:,}")
                finally:
                    cursor.close()
            stats.update(watermark=watermark, unique_rows=snapshot.count,
                         duplicate_source_keys=snapshot.duplicates, snapshot_complete=True)
            if mode == "full":
                validate_snapshot_cleanup(collection, view_scope(spec, start_date, end_date), snapshot,
                                          allow=allow_large_cleanup)
            if dry_run and preview_snapshot is not None:
                for key, document in snapshot:
                    preview_snapshot.add(key, {**document, "_source_view": spec.view_id,
                        "_source_key": key, "_sync_run_id": run_id,
                        "_id": hashlib.sha256(key.encode()).hexdigest()})
            if not dry_run:
                check_leases(verify=True)
                ensure_index(collection, "_source_key", unique=spec.view_id != "ZSGV_ZPP_SERNOLIST",
                             name="source_key_lookup" if spec.view_id == "ZSGV_ZPP_SERNOLIST" else "source_key_unique")
                for field in spec.index_fields:
                    ensure_index(collection, field, name=f"source_{field.lower()}")
                database[env("SYNC_RUN_COLLECTION", "sync_runs")].insert_one({
                    "_id": f"{run_id}:{spec.view_id}", "view_id": spec.view_id,
                    "collection": spec.collection, "status": "running", "started_at": datetime.now(timezone.utc),
                })
                run_recorded = True
                operations = []

                def flush() -> None:
                    if operations:
                        check_leases(verify=True)
                        result = collection.bulk_write(operations, ordered=False)
                        stats["upserted"] += result.upserted_count
                        stats["matched"] += getattr(result, "matched_count", 0)
                        stats["modified"] += getattr(result, "modified_count", 0)
                        operations.clear()

                for key, document in snapshot:
                    document.update({"_source_view": spec.view_id, "_source_key": key,
                                     "_sync_run_id": run_id, "_synced_at": datetime.now(timezone.utc)})
                    operations.append(UpdateOne({"_source_key": key}, {"$set": document,
                        "$setOnInsert": {"_id": hashlib.sha256(key.encode("utf-8")).hexdigest()}}, upsert=True))
                    if len(operations) >= batch_size:
                        flush()
                flush()
        stats["success"] = True
        progress_log(f"[{spec.view_id}] 同步完成：rows={stats['source_rows']:,} batches={stats['batches']:,}")
        if not dry_run and not defer_finalize:
            stats.update(finalize_view_run(spec, stats))
            stats["finalized"] = True
        return stats
    except Exception as exc:
        stats["error"] = str(exc)
        if database is not None and run_recorded and not dry_run:
            database[env("SYNC_RUN_COLLECTION", "sync_runs")].update_one({"_id": f"{run_id}:{spec.view_id}"}, {"$set": {"status": "failed", "finished_at": datetime.now(timezone.utc), "error": str(exc)}})
        raise
    finally:
        if mongo_client is not None:
            mongo_client.close()


def finalize_view_run(spec: ViewSpec, sync_result: Mapping[str, Any]) -> dict[str, int]:
    """Commit a staged view sync after its table cleanup has completed."""
    if not sync_result.get("success") or not sync_result.get("snapshot_complete"):
        raise ValueError("不能提交失败的同步任务")
    run_id = str(sync_result["run_id"])
    client = MongoClient(mongo_uri(), **mongo_client_options())
    try:
        database = client[env("MONGODB_DATABASE")]
        collection = database[spec.collection]
        deleted = 0
        if sync_result.get("mode") == "full":
            scope = view_scope(spec, sync_result["range"]["start"], sync_result["range"]["end"])
            deleted = cleanup_records(database, spec.collection, scope,
                {**scope, "_sync_run_id": {"$ne": run_id}}, int(sync_result["unique_rows"]),
                allow_large_cleanup=bool(sync_result.get("allow_large_cleanup")))
        check_leases(verify=True)
        now = datetime.now(timezone.utc)
        checkpoints = database[env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints")]
        previous = checkpoints.find_one({"_id": CHECKPOINT_PREFIX + spec.view_id}) or {}
        candidates = [value for value in (normalize_watermark(spec, previous.get("watermark")),
                                          normalize_watermark(spec, sync_result.get("watermark"))) if value]
        watermark = max(candidates) if candidates else None
        database[env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints")].update_one(
            {"_id": CHECKPOINT_PREFIX + spec.view_id},
            {"$set": {"view_id": spec.view_id, "collection": spec.collection, "watermark": watermark, "run_id": run_id, "updated_at": now}},
            upsert=True,
        )
        database[env("SYNC_RUN_COLLECTION", "sync_runs")].update_one(
            {"_id": f"{run_id}:{spec.view_id}"},
            {"$set": {"status": "success", "finished_at": now, "stats": dict(sync_result)}},
        )
        return {"deleted_out_of_scope": deleted}
    finally:
        client.close()
