#!/usr/bin/env python3
"""Query SAP production orders by serial number and maintain repair records.

The direct query mode implements ``ZSIMS_CL_INBOUND_SN_INFO``. The optional
repair mode enriches MongoDB repair records from station records sharing the
same host serial number, then queries SAP only for unresolved orders. It also
fills planned-start dates and removes orphaned repair records.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO
from uuid import uuid4

try:
    import httpx
    from pymongo import MongoClient, UpdateOne
except ImportError as exc:  # pragma: no cover - deployment guard
    print(f"缺少依赖 httpx 或 pymongo，请先安装 requirements.txt: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc

try:
    from scripts.sources.hana.hana_view_sync import (
        mongo_client_options,
        mongo_lease_lock,
        mongo_uri,
        mongo_write_concern_summary,
        process_lock,
    )
except ImportError as exc:  # pragma: no cover - deployment guard
    print(f"缺少项目同步依赖: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc


ROOT = Path(__file__).resolve().parents[2]
METHOD = "ZSIMS_CL_INBOUND_SN_INFO"
DEFAULT_URLS = {
    "SG": "http://10.2.101.37:8000/sap/ZHTTP_SIMS?sap-client=800",
    "KK": "http://10.8.100.11:8001/sap/ZHTTP_SIMS?sap-client=600",
}
ERROR_MSGTYS = {"E", "A", "X"}
REPAIR_SOURCE_VIEW = "ZSGV_ZZT_WLJL"
STATION_SOURCE_VIEW = "Z_V_ZMES_T_001"
DEFAULT_REPAIR_LOCK = "/tmp/line-fault-order-enrichment.lock"
REPAIR_CONFIRMATION = "DELETE-REPAIR-ORDERS"
LOGGER = logging.getLogger("get_production_orders_by_sn")
RESULT_FIELDS = (
    "sn",
    "production_order",
    "material_number",
    "material_description",
    "上线日期",
    "下线日期",
    "入库日期",
    "出库日期",
    "status",
    "error",
)

_PROGRESS_ACTIVE = False


class ProgressAwareStreamHandler(logging.StreamHandler):
    """Keep timestamped log lines separate from the carriage-return progress bar."""

    def emit(self, record: logging.LogRecord) -> None:
        if _PROGRESS_ACTIVE:
            self.stream.write("\r" + (" " * 160) + "\r")
        super().emit(record)


class Progress:
    """Small dependency-free progress bar written to stderr."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._last_stage = ""
        self._last_logged = 0

    def _log_progress(self, stage: str, current: int, total: int | None) -> None:
        if stage != self._last_stage:
            self._last_stage = stage
            self._last_logged = 0
            LOGGER.info("阶段开始: %s%s", stage, f"，目标 {total}" if total else "")
        milestone = max(1, total // 10) if total and total > 0 else 100
        if current == total or current - self._last_logged >= milestone:
            LOGGER.info("阶段进度: %s %s", stage, f"{current}/{total}" if total else current)
            self._last_logged = current

    def update(self, stage: str, current: int, total: int | None = None) -> None:
        global _PROGRESS_ACTIVE
        self._log_progress(stage, current, total)
        if not self.enabled:
            return
        _PROGRESS_ACTIVE = True
        if total and total > 0:
            width = 28
            done = min(width, int(width * current / total))
            bar = "#" * done + "." * (width - done)
            text = f"\r{stage} [{bar}] {current}/{total} ({current / total:.0%})"
        else:
            text = f"\r{stage}: {current}"
        print(text, end="", file=sys.stderr, flush=True)

    def finish(self) -> None:
        global _PROGRESS_ACTIVE
        if self.enabled:
            if _PROGRESS_ACTIVE:
                print(file=sys.stderr, flush=True)
            _PROGRESS_ACTIVE = False


def configure_logging(level: str, log_file: str | None = None) -> None:
    """Configure stderr logging and optionally mirror it to a UTF-8 file."""
    normalized = level.strip().upper()
    if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("--log-level 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.setLevel(normalized)
    LOGGER.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    stream_handler = ProgressAwareStreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)


class QueryError(Exception):
    """An SAP response or transport error for one batch."""


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load simple KEY=VALUE pairs without overwriting shell variables."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def signature(method: str = METHOD, day: date | None = None) -> str:
    timestamp = (day or date.today()).strftime("%Y%m%d")
    return hashlib.md5(f"sugon{method}{timestamp}sugon".encode("utf-8")).hexdigest().upper()


def request_headers(day: date | None = None) -> dict[str, str]:
    timestamp = (day or date.today()).strftime("%Y%m%d")
    return {
        "Content-Type": "application/json",
        "method": METHOD,
        "sign": signature(METHOD, day),
        "time": timestamp,
    }


def normalize_sn(value: Any) -> str:
    return str(value or "").strip()


def normalize_order(value: Any) -> str:
    """Normalize SAP order values for matching while preserving source values on write."""
    value = normalize_sn(value)
    return value.lstrip("0") or "0" if value else ""


def read_serial_numbers(path: str | None, values: Iterable[str] | None) -> list[str]:
    """Read SNs from repeated CLI values or a text/CSV file, preserving order."""
    raw: list[str] = list(values or [])
    if path:
        if path == "-":
            content = sys.stdin.read()
        else:
            content = Path(path).read_text(encoding="utf-8-sig")
        lines = content.splitlines()
        if path.lower().endswith(".csv") and lines:
            rows = csv.reader(lines)
            parsed = list(rows)
            header = [normalize_sn(x).upper() for x in parsed[0]]
            column = next((i for i, name in enumerate(header) if name in {"SN", "PCODE", "SN_LIST", "SERIAL_NUMBER"}), None)
            parsed = parsed[1:] if column is not None else parsed
            raw.extend(row[column] if column is not None and len(row) > column else (row[0] if row else "") for row in parsed)
        else:
            raw.extend(lines)
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        sn = normalize_sn(item)
        if sn and sn not in seen:
            seen.add(sn)
            result.append(sn)
    if not result:
        raise ValueError("未提供有效 SN；请使用 --sn 或 --input")
    return result


def validate_response(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        raise QueryError("响应不是 JSON 对象")
    msgty = str(body.get("MSGTY") or "").strip().upper()
    message = normalize_sn(body.get("MSGTX"))
    if msgty in ERROR_MSGTYS and "no data" not in message.lower() and "无数据" not in message:
        raise QueryError(f"SAP 返回错误 ({msgty}): {message or '未知错误'}")
    data = body.get("DATA", [])
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise QueryError("响应缺少有效 DATA 数组")
    return data


def rows_for_serial_numbers(serial_numbers: list[str], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_sn: dict[str, dict[str, Any]] = {}
    for record in records:
        sn = normalize_sn(record.get("PCODE"))
        if sn:
            by_sn[sn] = record
    result = []
    for sn in serial_numbers:
        record = by_sn.get(sn)
        if record is None:
            result.append({"sn": sn, "production_order": "", "status": "not_found", "error": ""})
            continue
        result.append({
            "sn": sn,
            "production_order": normalize_sn(record.get("AUFNR")),
            "material_number": normalize_sn(record.get("MATNR")),
            "material_description": normalize_sn(record.get("MAKTX")),
            "上线日期": normalize_sn(record.get("CDATE_S")),
            "下线日期": normalize_sn(record.get("CDATE_E")),
            "入库日期": normalize_sn(record.get("CDATE_I")),
            "出库日期": normalize_sn(record.get("CDATE_O")),
            "status": "found",
            "error": "",
            "raw": record,
        })
    return result


def query_batch(client: Any, url: str, serial_numbers: list[str], retries: int, retry_delay: float) -> list[dict[str, Any]]:
    payload = {"SN_LIST": serial_numbers}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.post(url, json=payload, headers=request_headers())
            response.raise_for_status()
            return rows_for_serial_numbers(serial_numbers, validate_response(response.json()))
        except QueryError:
            raise
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                LOGGER.warning(
                    "SAP 请求失败，将重试: batch_size=%d attempt=%d/%d error=%s",
                    len(serial_numbers), attempt + 1, retries + 1, exc,
                )
                time.sleep(retry_delay * (2**attempt))
    LOGGER.error("SAP 请求最终失败: batch_size=%d retries=%d error=%s", len(serial_numbers), retries, last_error)
    raise QueryError(f"请求失败（重试 {retries} 次）: {last_error}")


def repair_filter(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {} if getattr(args, "all_source_views", False) else {"_source_view": REPAIR_SOURCE_VIEW}
    if getattr(args, "sync_run_id", None):
        result["_sync_run_id"] = args.sync_run_id
    dates: dict[str, str] = {}
    if args.from_date:
        dates["$gte"] = args.from_date.strftime("%Y%m%d")
    if args.to_date:
        dates["$lte"] = args.to_date.strftime("%Y%m%d")
    if dates:
        result["ZDATE_WX"] = dates
    return result


def repair_candidate_batches(
    db: Any,
    collection_name: str,
    args: argparse.Namespace,
    progress: Progress | None = None,
    total_documents: int | None = None,
) -> Iterable[list[dict[str, Any]]]:
    cursor = db[collection_name].find(
        repair_filter(args), {"_id": 1, "PCODE": 1, "AUFNR": 1, "VBELN": 1, "GSTRS": 1, "_source_view": 1}
    )
    if isinstance(cursor, list):
        cursor.sort(key=lambda row: str(row.get("_id", "")))
    elif hasattr(cursor, "sort"):
        cursor = cursor.sort("_id", 1)
    batch: list[dict[str, Any]] = []
    scanned = 0
    visited = 0
    batch_number = 0
    LOGGER.info("维修记录扫描开始: collection=%s", collection_name)
    try:
        for document in cursor:
            visited += 1
            if progress:
                progress.update("扫描维修记录", visited, total_documents)
            if getattr(args, "missing_sales_only", False) and normalize_sn(document.get("VBELN")):
                continue
            batch.append(document)
            scanned += 1
            if len(batch) >= args.batch_size:
                batch_number += 1
                LOGGER.info("维修候选批次准备完成: batch=%d candidates=%d", batch_number, len(batch))
                yield batch
                batch = []
            if args.limit is not None and scanned >= args.limit:
                break
        if batch:
            batch_number += 1
            LOGGER.info("维修候选批次准备完成: batch=%d candidates=%d", batch_number, len(batch))
            yield batch
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
        LOGGER.info("维修记录扫描结束: visited=%d candidates=%d batches=%d", visited, scanned, batch_number)


def lookup_missing_orders(
    client: Any,
    serial_numbers: list[str],
    retries: int,
    retry_delay: float,
    batch_size: int,
    urls: dict[str, str],
    progress: Progress | None = None,
    progress_offset: int = 0,
    progress_total: int | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    """Query KK first, then query only unresolved SNs against SG."""
    pending = list(dict.fromkeys(serial_numbers))
    found: dict[str, dict[str, str]] = {}
    stats = {"kk_batches": 0, "kk_found": 0, "sg_batches": 0, "sg_found": 0}
    processed = 0
    for source in ("KK", "SG"):
        if not pending:
            break
        next_pending: list[str] = []
        total_batches = (len(pending) + batch_size - 1) // batch_size
        LOGGER.info("SAP %s 查询开始: sns=%d batches=%d", source, len(pending), total_batches)
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            batch_number = start // batch_size + 1
            LOGGER.info("SAP %s 查询批次开始: batch=%d/%d sns=%d", source, batch_number, total_batches, len(batch))
            stats[f"{source.lower()}_batches"] += 1
            rows = query_batch(client, urls[source], batch, retries, retry_delay)
            processed += len(batch)
            if progress:
                progress.update(f"查询 {source} 生产订单", progress_offset + processed, progress_total)
            for row in rows:
                sn = normalize_sn(row.get("sn"))
                order = normalize_sn(row.get("production_order"))
                if order:
                    found[sn] = {"production_order": order, "source": source}
                    stats[f"{source.lower()}_found"] += 1
                else:
                    next_pending.append(sn)
            LOGGER.info(
                "SAP %s 查询批次结束: batch=%d/%d found=%d unresolved=%d",
                source, batch_number, total_batches,
                sum(bool(normalize_sn(row.get("production_order"))) for row in rows),
                sum(not normalize_sn(row.get("production_order")) for row in rows),
            )
        pending = next_pending
        LOGGER.info(
            "SAP %s 查询结束: found=%d unresolved=%d",
            source, stats[f"{source.lower()}_found"], len(pending),
        )
    return found, stats


def sales_order_index(collection: Any) -> dict[tuple[str, str], str]:
    """Build a normalized (source, AUFNR) -> VBELN index from the order board."""
    index: dict[tuple[str, str], str] = {}
    cursor = collection.find({}, {"source": 1, "aufnr": 1, "data.AUFNR": 1, "data.VBELN": 1})
    try:
        for document in cursor:
            data = document.get("data") if isinstance(document.get("data"), dict) else {}
            order = normalize_order(data.get("AUFNR") or document.get("aufnr"))
            vbeln = normalize_sn(data.get("VBELN"))
            if not order:
                continue
            source = normalize_sn(document.get("source")).upper()
            key = (source, order)
            if key not in index or not index[key] and vbeln:
                index[key] = vbeln
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    return index


def planned_start_index(collection: Any) -> tuple[dict[str, str], set[str]]:
    """Build unambiguous production-order -> planned-start-date mappings."""
    values: dict[str, set[str]] = {}
    cursor = collection.find({}, {"aufnr": 1, "data.AUFNR": 1, "data.GSTRS": 1, "gstrs_date": 1})
    try:
        for document in cursor:
            data = document.get("data") if isinstance(document.get("data"), dict) else {}
            order = normalize_order(data.get("AUFNR") or document.get("aufnr"))
            planned_start = normalize_sn(data.get("GSTRS")) or normalize_sn(document.get("gstrs_date"))
            if order and planned_start:
                values.setdefault(order, set()).add(planned_start)
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    ambiguous = {order for order, dates in values.items() if len(dates) > 1}
    return ({order: next(iter(dates)) for order, dates in values.items() if len(dates) == 1}, ambiguous)


def resolve_sales_order(index: dict[tuple[str, str], str], order: str, preferred_source: str | None) -> tuple[str, str] | None:
    normalized = normalize_order(order)
    if not normalized:
        return None
    sources = []
    if preferred_source:
        sources.append(preferred_source.upper())
    sources.extend(source for source in ("KK", "SG") if source not in sources)
    sources.append("")
    for source in sources:
        key = (source, normalized)
        if key in index:
            return index[key], source
    return None


def station_order_index(
    collection: Any,
    serial_numbers: Iterable[str],
    batch_size: int,
) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    """Return unique AUFNR/KDAUF values from station records keyed by PCODE."""
    serials = list(dict.fromkeys(sn for sn in (normalize_sn(value) for value in serial_numbers) if sn))
    result = {sn: {"production_order": "", "sales_order": ""} for sn in serials}
    stats = {
        "station_lookup_sns": len(serials),
        "station_matched_sns": 0,
        "station_ambiguous_production_sns": 0,
        "station_ambiguous_sales_sns": 0,
    }
    if not serials:
        return result, stats

    values: dict[str, dict[str, dict[str, str]]] = {
        sn: {"production_order": {}, "sales_order": {}} for sn in serials
    }
    LOGGER.info("工位记录查询开始: collection=%s sns=%d", getattr(collection, "name", "station"), len(serials))
    for start in range(0, len(serials), batch_size):
        batch = serials[start : start + batch_size]
        cursor = collection.find(
            {"_source_view": STATION_SOURCE_VIEW, "PCODE": {"$in": batch}},
            {"PCODE": 1, "AUFNR": 1, "KDAUF": 1},
        )
        try:
            for document in cursor:
                sn = normalize_sn(document.get("PCODE"))
                if sn not in values:
                    continue
                production = normalize_sn(document.get("AUFNR"))
                production_key = normalize_order(production)
                if production_key:
                    values[sn]["production_order"].setdefault(production_key, production)
                sales = normalize_sn(document.get("KDAUF"))
                if sales:
                    values[sn]["sales_order"].setdefault(sales, sales)
        finally:
            if hasattr(cursor, "close"):
                cursor.close()

    for sn, fields in values.items():
        production_values = fields["production_order"]
        sales_values = fields["sales_order"]
        if production_values or sales_values:
            stats["station_matched_sns"] += 1
        if len(production_values) == 1:
            result[sn]["production_order"] = next(iter(production_values.values()))
        elif len(production_values) > 1:
            stats["station_ambiguous_production_sns"] += 1
        if len(sales_values) == 1:
            result[sn]["sales_order"] = next(iter(sales_values.values()))
        elif len(sales_values) > 1:
            stats["station_ambiguous_sales_sns"] += 1
    LOGGER.info(
        "工位记录查询结束: sns=%d matched=%d ambiguous_production=%d ambiguous_sales=%d",
        stats["station_lookup_sns"], stats["station_matched_sns"],
        stats["station_ambiguous_production_sns"], stats["station_ambiguous_sales_sns"],
    )
    return result, stats


def plan_repair_actions(
    candidates: list[dict[str, Any]],
    sn_orders: dict[str, dict[str, str]],
    order_index: dict[tuple[str, str], str],
    station_index: dict[str, dict[str, str]],
    planned_starts: dict[str, str],
    ambiguous_planned_starts: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    actions: list[dict[str, Any]] = []
    stats = {
        "matched_sales_orders": 0,
        "skipped_no_sn": 0,
        "skipped_sap_not_found": 0,
        "skipped_board_missing_sales": 0,
        "non_5000_orders": 0,
        "matched_station_records": 0,
        "filled_production_from_station": 0,
        "filled_sales_from_station": 0,
        "update_candidates": 0,
        "delete_candidates": 0,
        "empty_planned_start": 0,
        "planned_start_candidates": 0,
        "skipped_ambiguous_planned_start": 0,
    }
    for document in candidates:
        current_order = normalize_sn(document.get("AUFNR"))
        current_sales = normalize_sn(document.get("VBELN"))
        sn = normalize_sn(document.get("PCODE"))
        station = station_index.get(sn, {})
        fields: dict[str, str] = {}
        station_sales_used = False
        if not current_order and normalize_sn(station.get("production_order")):
            current_order = normalize_sn(station["production_order"])
            fields["AUFNR"] = current_order
            stats["filled_production_from_station"] += 1
        if not current_sales and normalize_sn(station.get("sales_order")):
            current_sales = normalize_sn(station["sales_order"])
            fields["VBELN"] = current_sales
            station_sales_used = True
            stats["filled_sales_from_station"] += 1

        lookup = sn_orders.get(sn) if not current_order else None
        if not current_order:
            if not sn:
                stats["skipped_no_sn"] += 1
            elif not lookup:
                stats["skipped_sap_not_found"] += 1
            else:
                current_order = lookup["production_order"]
                fields["AUFNR"] = current_order
            if not current_order:
                if fields:
                    stats["matched_station_records"] += 1
                    stats["update_candidates"] += 1
                    actions.append({"kind": "update", "document": document, "fields": fields, "sales_source": "station"})
                continue

        sales_source = "station" if station_sales_used else ""
        if not station_sales_used:
            preferred_source = lookup["source"] if lookup else None
            matched = resolve_sales_order(order_index, current_order, preferred_source)
            if matched is None:
                stats["non_5000_orders"] += 1
                stats["delete_candidates"] += 1
                actions.append({"kind": "delete", "document": document})
                continue
            sales_order, sales_source = matched
            if not current_sales:
                if not sales_order:
                    stats["skipped_board_missing_sales"] += 1
                    continue
                fields["VBELN"] = sales_order
        planned_start = normalize_sn(document.get("GSTRS"))
        normalized_order = normalize_order(current_order)
        if not planned_start:
            stats["empty_planned_start"] += 1
            if normalized_order in planned_starts:
                fields["GSTRS"] = planned_starts[normalized_order]
                stats["planned_start_candidates"] += 1
            elif normalized_order in ambiguous_planned_starts:
                stats["skipped_ambiguous_planned_start"] += 1
        if station_sales_used:
            stats["matched_station_records"] += 1
        if fields:
            if "VBELN" in fields:
                stats["matched_sales_orders"] += 1
            stats["update_candidates"] += 1
            actions.append({"kind": "update", "document": document, "fields": fields, "sales_source": sales_source})
    return actions, stats


def apply_repair_actions(
    db: Any,
    collection_name: str,
    actions: list[dict[str, Any]],
    batch_size: int,
    apply: bool,
    progress: Progress | None = None,
) -> tuple[int, int]:
    updates = deletes = 0
    collection = db[collection_name]
    total_batches = (len(actions) + batch_size - 1) // batch_size if actions else 0
    LOGGER.info(
        "维修数据变更开始: mode=%s actions=%d batches=%d",
        "apply" if apply else "dry-run", len(actions), total_batches,
    )
    for start in range(0, len(actions), batch_size):
        batch = actions[start : start + batch_size]
        batch_number = start // batch_size + 1
        update_count = sum(action["kind"] == "update" for action in batch)
        delete_count = len(batch) - update_count
        LOGGER.info(
            "维修数据变更批次开始: batch=%d/%d updates=%d deletes=%d",
            batch_number, total_batches, update_count, delete_count,
        )
        if progress:
            progress.update("执行维修数据变更", min(start + len(batch), len(actions)), len(actions))
        if not apply:
            LOGGER.info("维修数据变更批次跳过写入: batch=%d/%d reason=dry-run", batch_number, total_batches)
            continue
        update_operations = []
        delete_operations = []
        updated_in_batch = deleted_in_batch = 0
        for action in batch:
            document = action["document"]
            identity = {"_id": document.get("_id"), "_source_view": document.get("_source_view")}
            identity.update({field: document.get(field) for field in ("AUFNR", "VBELN")})
            if action["kind"] == "update":
                update_operations.append(UpdateOne(identity, {"$set": action["fields"]}))
            else:
                delete_operations.append(identity)
        if update_operations:
            result = collection.bulk_write(update_operations, ordered=False)
            updated_in_batch = result.modified_count
            updates += updated_in_batch
        if delete_operations:
            result = collection.delete_many({"$or": delete_operations})
            deleted_in_batch = result.deleted_count
            deletes += deleted_in_batch
        LOGGER.info(
            "维修数据变更批次结束: batch=%d/%d updated=%d deleted=%d",
            batch_number, total_batches,
            updated_in_batch, deleted_in_batch,
        )
    LOGGER.info("维修数据变更结束: updated=%d deleted=%d", updates, deletes)
    return updates, deletes


def repair_workflow(db: Any, args: argparse.Namespace, client_factory: Any = httpx.Client, run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or uuid4().hex
    LOGGER.info(
        "维修任务开始: run_id=%s mode=%s repair_collection=%s station_collection=%s order_collection=%s missing_sales_only=%s limit=%s",
        run_id, "apply" if args.apply else "dry-run", args.repair_collection, args.station_collection, args.order_collection,
        getattr(args, "missing_sales_only", False), args.limit,
    )
    summary: dict[str, Any] = {
        "success": False, "run_id": run_id, "mode": "apply" if args.apply else "dry-run",
        "repair_collection": args.repair_collection, "station_collection": args.station_collection,
        "order_collection": args.order_collection,
        "source_view": None if getattr(args, "all_source_views", False) else REPAIR_SOURCE_VIEW,
        "missing_sales_only": getattr(args, "missing_sales_only", False),
        "scanned": 0, "unique_sns": 0,
        "updates": 0, "deleted": 0,
        "kk_batches": 0, "kk_found": 0, "sg_batches": 0, "sg_found": 0,
        "matched_sales_orders": 0, "skipped_no_sn": 0, "skipped_sap_not_found": 0,
        "skipped_board_missing_sales": 0, "non_5000_orders": 0,
        "station_lookup_sns": 0, "station_matched_sns": 0,
        "station_ambiguous_production_sns": 0, "station_ambiguous_sales_sns": 0,
        "matched_station_records": 0,
        "filled_production_from_station": 0, "filled_sales_from_station": 0,
        "update_candidates": 0, "delete_candidates": 0,
        "planned_start_orders": 0, "ambiguous_planned_start_orders": 0,
        "empty_planned_start": 0, "planned_start_candidates": 0,
        "skipped_ambiguous_planned_start": 0,
    }
    order_index = sales_order_index(db[args.order_collection])
    planned_starts, ambiguous_planned_starts = planned_start_index(db[args.order_collection])
    summary["planned_start_orders"] = len(planned_starts)
    summary["ambiguous_planned_start_orders"] = len(ambiguous_planned_starts)
    LOGGER.info(
        "销售订单索引构建完成: sales_entries=%d planned_start_orders=%d ambiguous_planned_start_orders=%d",
        len(order_index), len(planned_starts), len(ambiguous_planned_starts),
    )
    progress = Progress(not args.no_progress)
    actions: list[dict[str, Any]] = []
    sn_orders: dict[str, dict[str, str]] = {}
    unresolved_sns: set[str] = set()
    station_records: dict[str, dict[str, str]] = {}
    with client_factory(timeout=args.timeout, trust_env=False) as client:
        candidate_batch_number = 0
        for candidates in repair_candidate_batches(db, args.repair_collection, args, progress):
            candidate_batch_number += 1
            LOGGER.info(
                "维修候选批次开始处理: batch=%d candidates=%d",
                candidate_batch_number, len(candidates),
            )
            candidate_sns = list(dict.fromkeys(normalize_sn(document.get("PCODE")) for document in candidates if normalize_sn(document.get("PCODE"))))
            station_sns = [sn for sn in candidate_sns if sn not in station_records]
            if station_sns:
                batch_station_records, station_stats = station_order_index(
                    db[args.station_collection], station_sns, args.batch_size,
                )
                station_records.update(batch_station_records)
                for key, value in station_stats.items():
                    summary[key] += value
            serial_numbers = list(dict.fromkeys(
                normalize_sn(document.get("PCODE"))
                for document in candidates
                if not normalize_order(document.get("AUFNR"))
                and normalize_sn(document.get("PCODE"))
                and not normalize_sn(station_records.get(normalize_sn(document.get("PCODE")), {}).get("production_order"))
            ))
            serial_numbers = [sn for sn in serial_numbers if sn not in sn_orders and sn not in unresolved_sns]
            if serial_numbers:
                batch_orders, source_stats = lookup_missing_orders(
                    client, serial_numbers, args.retries, args.retry_delay, args.batch_size,
                    DEFAULT_URLS, progress, summary["unique_sns"], None,
                )
                sn_orders.update(batch_orders)
                unresolved_sns.update(sn for sn in serial_numbers if sn not in batch_orders)
                for key, value in source_stats.items():
                    summary[key] += value
            summary["scanned"] += len(candidates)
            summary["unique_sns"] = len(sn_orders) + len(unresolved_sns)
            batch_actions, action_stats = plan_repair_actions(
                candidates, sn_orders, order_index, station_records, planned_starts, ambiguous_planned_starts,
            )
            actions.extend(batch_actions)
            for key, value in action_stats.items():
                summary[key] += value
            LOGGER.info(
                "维修候选批次处理完成: batch=%d actions=%d station_sns=%d queried_sns=%d",
                candidate_batch_number, len(batch_actions), len(station_sns), len(serial_numbers),
            )
    summary["actions"] = len(actions)
    LOGGER.info(
        "维修动作计划完成: run_id=%s actions=%d updates=%d deletes=%d planned_start_candidates=%d",
        run_id, summary["actions"], summary["update_candidates"], summary["delete_candidates"], summary["planned_start_candidates"],
    )
    summary["updates"], summary["deleted"] = apply_repair_actions(db, args.repair_collection, actions, args.batch_size, args.apply, progress)
    progress.finish()
    summary["success"] = True
    LOGGER.info(
        "维修任务完成: run_id=%s scanned=%d actions=%d updated=%d deleted=%d planned_start_candidates=%d",
        run_id, summary["scanned"], summary["actions"], summary["updates"], summary["deleted"], summary["planned_start_candidates"],
    )
    return summary


def write_results(rows: list[dict[str, Any]], output: str | None, output_format: str) -> None:
    LOGGER.info(
        "结果输出开始: format=%s rows=%d destination=%s",
        output_format, len(rows), "stdout" if not output or output == "-" else output,
    )
    stream: TextIO = sys.stdout if not output or output == "-" else Path(output).open("w", encoding="utf-8", newline="")
    close = stream is not sys.stdout
    try:
        if output_format == "json":
            json.dump(rows, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        elif output_format == "jsonl":
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        else:
            writer = csv.DictWriter(stream, fieldnames=RESULT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    finally:
        if close:
            stream.close()
    LOGGER.info("结果输出完成: format=%s rows=%d", output_format, len(rows))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过整机 SN 批量查询 SAP 生产订单号")
    parser.add_argument("--repair", action="store_true", help="补全并清理 MongoDB 维修故障明细")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--sn", nargs="+", help="一个或多个整机 SN")
    source.add_argument("--input", help="SN 文件（每行一个，或 CSV；使用 - 从标准输入读取）")
    parser.add_argument("--source", choices=sorted(DEFAULT_URLS), default=env("SN_SOURCE", "SG"), help="SAP 来源，默认 SG")
    parser.add_argument("--url", help="覆盖 SAP 接口地址")
    parser.add_argument("--batch-size", type=int, default=int(env("SN_BATCH_SIZE", "100")), help="每次请求的 SN 数量")
    parser.add_argument("--retries", type=int, default=int(env("SN_RETRIES", "2")), help="请求失败重试次数")
    parser.add_argument("--retry-delay", type=float, default=float(env("SN_RETRY_DELAY", "1")), help="首次重试等待秒数，之后指数退避")
    parser.add_argument("--timeout", type=float, default=float(env("HTTP_TIMEOUT", "120")), help="HTTP 超时秒数")
    parser.add_argument("--output", help="输出路径，默认标准输出；使用 - 输出到标准输出")
    parser.add_argument("--format", choices=("csv", "json", "jsonl"), default="csv", help="输出格式，默认 csv")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="维修模式只统计，不写入或删除（默认）")
    mode.add_argument("--apply", action="store_true", help="维修模式执行订单、计划开始时间回填和孤立记录删除")
    parser.add_argument("--confirm", help=f"删除确认字符串：{REPAIR_CONFIRMATION}")
    parser.add_argument("--from-date", type=date.fromisoformat, help="维修日期起始 YYYY-MM-DD")
    parser.add_argument("--to-date", type=date.fromisoformat, help="维修日期结束 YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="维修模式最多处理的候选记录数")
    parser.add_argument("--missing-sales-only", action="store_true", help="维修模式只处理销售订单 VBELN 为空的记录")
    parser.add_argument("--all-source-views", action="store_true", help="维修模式包含没有 _source_view 的历史记录")
    parser.add_argument("--no-progress", action="store_true", help="关闭进度条（日志仍会保留）")
    parser.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default=env("LOG_LEVEL", "INFO"), help="日志级别，默认 INFO",
    )
    parser.add_argument("--log-file", default=env("LOG_FILE", ""), help="将详细日志追加写入指定文件")
    parser.set_defaults(
        repair_collection=env("REPAIR_COLLECTION", "repair_records_sap"),
        station_collection=env("STATION_COLLECTION", "station_records_sap"),
        order_collection=env("TARGET_COLLECTION", "sales_orders_sap"),
    )
    return parser


def run(args: argparse.Namespace, client_factory: Any = httpx.Client) -> tuple[list[dict[str, Any]], bool]:
    if args.batch_size < 1 or args.retries < 0 or args.retry_delay < 0 or args.timeout <= 0:
        raise ValueError("batch-size 必须大于 0，retries/retry-delay 不能为负数，timeout 必须大于 0")
    serial_numbers = read_serial_numbers(args.input, args.sn)
    url = args.url or DEFAULT_URLS[args.source]
    rows: list[dict[str, Any]] = []
    success = True
    total_batches = (len(serial_numbers) + args.batch_size - 1) // args.batch_size
    progress = Progress(not args.no_progress)
    LOGGER.info(
        "直接查询开始: source=%s url_override=%s sns=%d batches=%d batch_size=%d retries=%d",
        args.source, bool(args.url), len(serial_numbers), total_batches, args.batch_size, args.retries,
    )
    try:
        with client_factory(timeout=args.timeout, trust_env=False) as client:
            for start in range(0, len(serial_numbers), args.batch_size):
                batch = serial_numbers[start : start + args.batch_size]
                batch_number = start // args.batch_size + 1
                LOGGER.info("直接查询批次开始: batch=%d/%d sns=%d", batch_number, total_batches, len(batch))
                try:
                    batch_rows = query_batch(client, url, batch, args.retries, args.retry_delay)
                    rows.extend(batch_rows)
                    LOGGER.info(
                        "直接查询批次完成: batch=%d/%d found=%d not_found=%d",
                        batch_number, total_batches,
                        sum(row.get("status") == "found" for row in batch_rows),
                        sum(row.get("status") == "not_found" for row in batch_rows),
                    )
                except QueryError as exc:
                    success = False
                    rows.extend({"sn": sn, "production_order": "", "status": "failed", "error": str(exc)} for sn in batch)
                    LOGGER.error("直接查询批次失败: batch=%d/%d sns=%d error=%s", batch_number, total_batches, len(batch), exc)
                progress.update("查询生产订单", min(start + len(batch), len(serial_numbers)), len(serial_numbers))
    finally:
        progress.finish()
    LOGGER.info(
        "直接查询完成: success=%s total=%d found=%d not_found=%d failed=%d",
        success, len(rows),
        sum(row.get("status") == "found" for row in rows),
        sum(row.get("status") == "not_found" for row in rows),
        sum(row.get("status") == "failed" for row in rows),
    )
    return rows, success


def run_repair(args: argparse.Namespace) -> dict[str, Any]:
    if args.sn or args.input:
        raise ValueError("--repair 不能与 --sn 或 --input 同时使用")
    if args.confirm and not args.apply:
        raise ValueError("--confirm 只能与 --apply 一起使用")
    if args.apply and not getattr(args, "internal", False) and args.confirm != REPAIR_CONFIRMATION:
        raise ValueError(f"永久删除必须提供 --confirm {REPAIR_CONFIRMATION}")
    if args.from_date and args.to_date and args.from_date > args.to_date:
        raise ValueError("--from-date 不能晚于 --to-date")
    if args.batch_size < 1 or args.retries < 0 or args.retry_delay < 0 or args.timeout <= 0:
        raise ValueError("batch-size 必须大于 0，retries/retry-delay 不能为负数，timeout 必须大于 0")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit 必须大于 0")
    database = env("MONGODB_DATABASE", "")
    if not database:
        raise ValueError("MONGODB_DATABASE 未配置")
    options = mongo_client_options()
    client = MongoClient(mongo_uri(), **options)
    started_at = datetime.now(timezone.utc)
    run_id = uuid4().hex
    audit_collection = env("ORDER_ENRICHMENT_RUN_COLLECTION", "sync_runs")
    enrichment_lock = env("ORDER_ENRICHMENT_LOCK_PATH", DEFAULT_REPAIR_LOCK)
    sync_lock = env("SYNC_LOCK_PATH", str(ROOT / ".sales_orders_sync.lock"))
    if Path(enrichment_lock).expanduser().resolve() == Path(sync_lock).expanduser().resolve():
        client.close()
        raise ValueError("ORDER_ENRICHMENT_LOCK_PATH 不能与 SYNC_LOCK_PATH 相同")
    result: dict[str, Any] = {"success": False, "run_id": run_id, "mode": "apply" if args.apply else "dry-run", "started_at": started_at}
    try:
        db = client[database]
        with process_lock(enrichment_lock):
            with process_lock(sync_lock):
                with mongo_lease_lock(db, env("SYNC_DISTRIBUTED_LOCK_NAME", "sales_repair_sync")):
                    result = repair_workflow(db, args, run_id=run_id)
        result.update({"started_at": started_at, "finished_at": datetime.now(timezone.utc), "mongo_write_concern": mongo_write_concern_summary(options)})
        return result
    except Exception as exc:
        result.update({"finished_at": datetime.now(timezone.utc), "error": str(exc)})
        raise
    finally:
        try:
            db = client[database]
            db[audit_collection].insert_one({**result, "status": "success" if result.get("success") else "failed"})
        except Exception as exc:
            LOGGER.warning("审计记录写入失败: collection=%s error=%s", audit_collection, exc)
        client.close()

