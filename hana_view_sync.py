#!/usr/bin/env python3
"""Shared HANA-to-MongoDB synchronization primitives for documented views."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import quote_plus
from uuid import uuid4

try:
    from pymongo import ASCENDING, MongoClient, ReturnDocument, UpdateOne
    from pymongo.errors import DuplicateKeyError
except ImportError as exc:  # pragma: no cover - deployment error
    raise SystemExit(f"missing dependency: {exc.name}; install pymongo") from exc


ROOT = Path(__file__).resolve().parent
DEFAULT_START_DATE = "2026-01-01"
CHECKPOINT_PREFIX = "hana_views:"


def install_graceful_shutdown() -> None:
    def handle_shutdown(signum: int, _frame: Any) -> None:
        raise RuntimeError(f"收到终止信号 {signum}，正在关闭数据库连接")

    signal.signal(signal.SIGTERM, handle_shutdown)


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
            "HADE7 HADE8 STATU MESS CLASSCODE EQUIPMENTNUMBER ERR_FLAG LINE_CODE NEXT_SECTION "
            "PASSCOUNT TEST_ID PRODH"
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


@contextmanager
def mongo_lease_lock(database: Any, name: str) -> Iterator[None]:
    """Coordinate destructive sync work across hosts through a MongoDB lease."""
    collection = database[env("SYNC_LOCK_COLLECTION", "sync_locks")]
    lease_seconds = int(env("SYNC_MONGO_LOCK_TTL_SECONDS", "43200"))
    if lease_seconds < 1:
        raise RuntimeError("SYNC_MONGO_LOCK_TTL_SECONDS 必须大于 0")
    owner = uuid4().hex
    now = datetime.now(timezone.utc)
    collection.create_index("expires_at", expireAfterSeconds=0, name="expires_at_ttl")
    try:
        lock = collection.find_one_and_update(
            {"_id": name, "$or": [{"expires_at": {"$lte": now}}, {"owner": owner}]},
            {"$set": {"owner": owner, "acquired_at": now, "expires_at": now + timedelta(seconds=lease_seconds)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        raise RuntimeError(f"已有其他主机同步任务运行中: {name}") from exc
    if not lock or lock.get("owner") != owner:
        raise RuntimeError(f"已有其他主机同步任务运行中: {name}")
    try:
        yield
    finally:
        collection.delete_one({"_id": name, "owner": owner})


def parse_date(value: str) -> str:
    normalized = value.strip().replace("-", "")
    try:
        return datetime.strptime(normalized, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD 或 YYYYMMDD") from exc


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
    return value


def build_query(spec: ViewSpec, checkpoint: Mapping[str, Any] | None, mode: str, start_date: str | None, lookback_days: int) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    watermark = checkpoint.get("watermark") if checkpoint else None
    if mode == "incremental" and watermark is not None and spec.watermark_fields:
        field = spec.watermark_fields[0]
        conditions.append(f'"{field}" >= ?')
        params.append(date_overlap(watermark, lookback_days) if field == "BUDAT_MKPF" else watermark)
    if start_date and spec.start_date_field:
        value = start_date
        if spec.start_date_field == "ACTUAL_START_TIME":
            value = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]} 00:00:00"
        conditions.append(f'"{spec.start_date_field}" >= ?')
        params.append(value)
    selected = ", ".join(f'"{field}"' for field in spec.columns)
    order = ", ".join(f'"{field}"' for field in spec.order_by)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return f"SELECT {selected} FROM {spec.qualified_name}{where} ORDER BY {order}", params


def max_watermark(spec: ViewSpec, current: Any, row: Mapping[str, Any]) -> Any:
    if not spec.watermark_fields:
        return current
    value = normalize_value(row.get(spec.watermark_fields[0]))
    if value is None or current is not None and value <= normalize_value(current):
        return current
    return value


def sync_view(spec: ViewSpec, *, mode: str, start_date: str | None, batch_size: int, lookback_days: int, dry_run: bool) -> dict[str, Any]:
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
    }
    mongo_client = None
    database = None
    try:
        if not dry_run:
            client_options = mongo_client_options()
            stats["mongo_write_concern"] = mongo_write_concern_summary(client_options)
            mongo_client = MongoClient(
                mongo_uri(),
                **client_options,
            )
            database = mongo_client[env("MONGODB_DATABASE")]
            collection = database[spec.collection]
            checkpoints = database[env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints")]
            runs = database[env("SYNC_RUN_COLLECTION", "sync_runs")]
            collection.create_index("_source_key", unique=True, name="source_key_unique")
            for field in spec.index_fields:
                collection.create_index(field, name=f"source_{field.lower()}")
            runs.insert_one({"_id": f"{run_id}:{spec.view_id}", "view_id": spec.view_id, "collection": spec.collection, "status": "running", "started_at": datetime.now(timezone.utc)})
            checkpoint = checkpoints.find_one({"_id": CHECKPOINT_PREFIX + spec.view_id}) if mode == "incremental" else None
            if mode == "full":
                deleted = collection.delete_many({"_source_view": spec.view_id})
                stats["deleted_before_sync"] = deleted.deleted_count
                checkpoints.delete_one({"_id": CHECKPOINT_PREFIX + spec.view_id})
        else:
            checkpoint = None
        sql, params = build_query(spec, checkpoint, mode, start_date, lookback_days)
        watermark = None
        with hana_connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, tuple(params))
                while rows := cursor.fetchmany(batch_size):
                    operations = []
                    for values in rows:
                        row = dict(zip(spec.columns, values, strict=True))
                        watermark = max_watermark(spec, watermark, row)
                        key = source_key(row, spec)
                        stats["source_rows"] += 1
                        if not dry_run:
                            document = {field: bson_value(value) for field, value in row.items()}
                            document.update({"_source_view": spec.view_id, "_source_key": key, "_sync_run_id": run_id, "_synced_at": datetime.now(timezone.utc)})
                            operations.append(UpdateOne({"_source_key": key}, {"$set": document, "$setOnInsert": {"_id": hashlib.sha256(key.encode("utf-8")).hexdigest()}}, upsert=True))
                    if operations:
                        result = collection.bulk_write(operations, ordered=False)
                        stats["upserted"] += result.upserted_count
                        stats["matched"] += getattr(result, "matched_count", 0)
                        stats["modified"] += getattr(result, "modified_count", 0)
                    stats["batches"] += 1
            finally:
                cursor.close()
        stats["watermark"] = watermark
        stats["success"] = True
        if not dry_run:
            now = datetime.now(timezone.utc)
            checkpoints.update_one({"_id": CHECKPOINT_PREFIX + spec.view_id}, {"$set": {"view_id": spec.view_id, "collection": spec.collection, "watermark": bson_value(watermark), "run_id": run_id, "updated_at": now}}, upsert=True)
            runs.update_one({"_id": f"{run_id}:{spec.view_id}"}, {"$set": {"status": "success", "finished_at": now, "stats": stats}})
        return stats
    except Exception as exc:
        stats["error"] = str(exc)
        if database is not None:
            database[env("SYNC_RUN_COLLECTION", "sync_runs")].update_one({"_id": f"{run_id}:{spec.view_id}"}, {"$set": {"status": "failed", "finished_at": datetime.now(timezone.utc), "error": str(exc)}})
        raise
    finally:
        if mongo_client is not None:
            mongo_client.close()


def run_cli(spec: ViewSpec, argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    install_graceful_shutdown()
    parser = argparse.ArgumentParser(description=f"同步 SAP HANA 视图 {spec.view_id}")
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--start-date", type=parse_date, default=env("SYNC_START_DATE", DEFAULT_START_DATE).replace("-", ""))
    parser.add_argument("--lookback-days", type=int, default=int(env("SYNC_LOOKBACK_DAYS", "7")))
    parser.add_argument("--batch-size", type=int, default=int(env("SYNC_BATCH_SIZE", "1000")))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        with process_lock(env("SYNC_LOCK_PATH", "/tmp/line-fault-sales-orders-sync.lock")):
            result = sync_view(spec, mode=args.mode, start_date=args.start_date, batch_size=args.batch_size, lookback_days=args.lookback_days, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "view_id": spec.view_id, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
