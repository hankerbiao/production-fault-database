#!/usr/bin/env python3
"""Synchronize SAP sales orders into an independent MongoDB database.

The default mode is incremental.  Each SAP source has its own date watermark;
the watermark is advanced only after that source has fetched and written
successfully.  Use ``--full --start-date YYYY-MM-DD`` for the initial load.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus

try:
    import httpx
    from pymongo import ASCENDING, MongoClient, UpdateOne
    from bson.decimal128 import Decimal128
except ImportError as exc:  # pragma: no cover - exercised by deployment, not unit tests
    print(json.dumps({"success": False, "error": f"missing dependency: {exc.name}; install httpx and pymongo"}, ensure_ascii=False))
    raise SystemExit(2) from exc

from hana_view_sync import (
    mongo_client_options,
    mongo_lease_lock,
    mongo_write_concern_summary,
    stream_nonempty_field_values,
)


ROOT = Path(__file__).resolve().parent
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
REPAIR_START_DATE = date(2026, 1, 1)


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs without requiring an extra dotenv package."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


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


def validate_records(body: Any, source: str) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        raise RuntimeError(f"{source}: 响应不是 JSON 对象")
    msgty = str(body.get("MSGTY") or "").upper()
    if msgty in ERROR_MSGTYS:
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
    if mode == "incremental" and checkpoint and checkpoint.get("watermark"):
        wm = checkpoint["watermark"]
        conditions.append('("ZDATE" > ? OR ("ZDATE" = ? AND "ZTIME" >= ?))')
        params.extend((wm["ZDATE"], wm["ZDATE"], wm["ZTIME"]))
    if start_date:
        conditions.append('"ZDATE" >= ?')
        params.append(start_date.strftime("%Y%m%d"))
    conditions.append('"ZDATE" <= ?')
    params.append(end_date.strftime("%Y%m%d"))
    selected = ", ".join(f'"{field}"' for field in REPAIR_COLUMNS)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    order = ", ".join(f'"{field}"' for field in ("PCODE", "ZWXDT", "ZMCOD1", "ZDATE_WX", "ZTIME"))
    return f"SELECT {selected} FROM {REPAIR_VIEW}{where} ORDER BY {order}", params


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


def sync_repair(db: Any, collection: str, checkpoint_collection: str, mode: str, start_date: date | None, end_date: date, lookback_days: int, batch_size: int, dry_run: bool, run_id: str) -> dict[str, Any]:
    if end_date < REPAIR_START_DATE:
        raise ValueError("维修数据仅同步 2026-01-01 及之后的数据")
    checkpoint = None if mode == "full" or dry_run else db[checkpoint_collection].find_one({"_id": "repair_records"})
    if mode == "incremental" and checkpoint and checkpoint.get("watermark"):
        watermark_date = date.fromisoformat(str(checkpoint["watermark"]["ZDATE"]))
        start_date = watermark_date - timedelta(days=lookback_days)
    start_date = max(start_date or REPAIR_START_DATE, REPAIR_START_DATE)
    allowed_vbelns = sales_order_vbelns(db)
    if not allowed_vbelns:
        raise RuntimeError("sales_orders_sap 中没有有效 VBELN，拒绝同步维修数据")
    sql, params = repair_query(mode, checkpoint, start_date, end_date)
    stats = {"success": False, "source_rows": 0, "matched_sales_orders": 0, "filtered_missing_sales_order": 0, "batches": 0, "inserted": 0, "updated": 0, "sales_order_vbelns": len(allowed_vbelns), "range": {"start": start_date.isoformat(), "end": end_date.isoformat()}}
    watermark = None
    if not dry_run:
        db[collection].create_index("_source_key", unique=True, name="source_key_unique")
    with hana_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, tuple(params))
            while rows := cursor.fetchmany(batch_size):
                operations = []
                for values in rows:
                    row = dict(zip(REPAIR_COLUMNS, values, strict=True))
                    watermark = watermark_max(watermark, row, ("ZDATE", "ZTIME"))
                    stats["source_rows"] += 1
                    if str(row.get("VBELN") or "").strip() not in allowed_vbelns:
                        stats["filtered_missing_sales_order"] += 1
                        continue
                    stats["matched_sales_orders"] += 1
                    if not dry_run:
                        key = source_key(row, REPAIR_KEY_FIELDS)
                        document = {key: bson_value(value) for key, value in row.items()}
                        document.update({"_source_key": key, "_source_view": "ZSGV_ZZT_WLJL", "_scope_run_id": run_id, "_synced_at": datetime.now(timezone.utc), "_sync_run_id": run_id})
                        operations.append(UpdateOne({"_source_key": key}, {"$set": document, "$setOnInsert": {"_id": hashlib.sha256(key.encode()).hexdigest()}}, upsert=True))
                if operations:
                    result = db[collection].bulk_write(operations, ordered=False)
                    stats["inserted"] += result.upserted_count
                    stats["updated"] += result.modified_count
                stats["batches"] += 1
        finally:
            cursor.close()
    if watermark is None and checkpoint:
        watermark = checkpoint.get("watermark")
    if not dry_run:
        db[collection].create_index([("PCODE", ASCENDING), ("ZDATE_WX", ASCENDING)], name="repair_pcode_date")
        if mode == "full":
            result = db[collection].delete_many({"_source_view": "ZSGV_ZZT_WLJL", "_scope_run_id": {"$ne": run_id}})
            stats["deleted_out_of_scope"] = result.deleted_count
        db[checkpoint_collection].update_one({"_id": "repair_records"}, {"$set": {"dataset": "repair_records", "watermark": watermark, "run_id": run_id, "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    stats["watermark"] = watermark
    stats["success"] = True
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SAP SG/KK 销售订单同步到独立 MongoDB")
    parser.add_argument("--dataset", choices=("sales", "repair", "all"), default=env("SYNC_DATASET", "all"), help="同步数据集，默认 all")
    parser.add_argument("--full", action="store_true", help="执行全量日期范围同步；必须同时提供 --start-date")
    parser.add_argument("--start-date", type=parse_date, help="全量起始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", type=parse_date, default=date.today(), help="结束日期，默认今天")
    parser.add_argument("--lookback-days", type=int, default=int(env("SYNC_LOOKBACK_DAYS", "7")), help="增量回看天数，默认 .env 中的值")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prodh-list", nargs="+", help="覆盖产品层次过滤，例如 --prodh-list 00100 00200")
    group.add_argument("--all-prodh", action="store_true", help="取消产品层次过滤")
    parser.add_argument("--dry-run", action="store_true", help="只请求和统计，不写入订单及水位线")
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


@contextmanager
def process_lock(path: str) -> Iterator[None]:
    lock = Path(path).expanduser()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"已有同步任务运行中: {lock}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
            result = db[target_collection].delete_many({"source": {"$in": list(SOURCES)}, "_scope_run_id": {"$ne": run_id}})
            summary["deleted_out_of_scope"] = result.deleted_count
        if not args.dry_run:
            db[env("SYNC_RUN_COLLECTION", "sync_runs")].insert_one({**summary, "created_at": synced_at})
        return summary
    finally:
        client.close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    mode = "full" if args.full else "incremental"
    if args.dataset == "repair":
        database_name = env("MONGODB_DATABASE")
        if not database_name:
            raise RuntimeError("MONGODB_DATABASE 未配置")
        client_options = mongo_client_options()
        client = MongoClient(mongo_uri(), **client_options)
        try:
            db = client[database_name]
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            result = sync_repair(db, env("REPAIR_COLLECTION", "repair_records_sap"), env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints"), mode, args.start_date, args.end_date, args.lookback_days, int(env("SYNC_BATCH_SIZE", "1000")), args.dry_run, run_id)
            result.update({"run_id": run_id, "dataset": "repair", "mode": mode, "dry_run": args.dry_run, "mongo_write_concern": mongo_write_concern_summary(client_options)})
            if not args.dry_run:
                db[env("SYNC_RUN_COLLECTION", "sync_runs")].insert_one({**result, "created_at": datetime.now(timezone.utc)})
            return result
        finally:
            client.close()
    if args.dataset == "sales":
        return sync_sales(args)

    sales_result = sync_sales(args)
    repair_result = run(argparse.Namespace(**{**vars(args), "dataset": "repair"}))
    return {"success": sales_result.get("success", False) and repair_result.get("success", False), "run_id": sales_result.get("run_id"), "dataset": "all", "sales": sales_result, "repair": repair_result}


def main() -> int:
    load_dotenv(ROOT / ".env")
    def handle_shutdown(signum: int, _frame: Any) -> None:
        raise RuntimeError(f"收到终止信号 {signum}，正在关闭数据库连接")
    signal.signal(signal.SIGTERM, handle_shutdown)
    parser = build_parser()
    args = parser.parse_args()
    lock_client = None
    try:
        with process_lock(env("SYNC_LOCK_PATH", str(ROOT / ".sales_orders_sync.lock"))):
            lock_client = MongoClient(mongo_uri(), **mongo_client_options())
            with mongo_lease_lock(
                lock_client[env("MONGODB_DATABASE")],
                env("SYNC_DISTRIBUTED_LOCK_NAME", "sales_repair_sync"),
            ):
                result = run(args)
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
    finally:
        if lock_client is not None:
            lock_client.close()
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
