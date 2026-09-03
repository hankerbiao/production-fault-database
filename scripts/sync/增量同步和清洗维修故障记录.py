#!/usr/bin/env python3
"""Incrementally synchronize and clean repair records through one workflow."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from pymongo import MongoClient, UpdateOne

from scripts.sources.hana.hana_view_sync import (
    env,
    load_dotenv,
    mongo_client_options,
    mongo_lease_lock,
    mongo_uri,
    mongo_write_concern_summary,
    process_lock,
)
from scripts.sync import sync_sales_orders


LOGGER = logging.getLogger("增量同步和清洗维修故障记录")
DEFAULT_LOCK = "/tmp/line-fault-station-order-backfill.lock"
DEFAULT_MONGO_LOCK = "repair-records-station-backfill"
SAP_METHOD = "ZSIMS_CL_INBOUND_SN_INFO"
SAP_SOURCES = (
    ("KK", "http://10.8.100.11:8001/sap/ZHTTP_SIMS?sap-client=600"),
    ("SG", "http://10.2.101.37:8000/sap/ZHTTP_SIMS?sap-client=800"),
)


def text(value: Any) -> str:
    return str(value or "").strip()


def normalized_order(value: Any) -> str:
    value = text(value)
    return value.lstrip("0") or "0" if value else ""


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD") from exc


def missing_text_filter(field: str) -> dict[str, Any]:
    return {
        "$or": [
            {field: {"$exists": False}},
            {field: None},
            {field: ""},
            {field: {"$regex": r"^\s*$"}},
        ]
    }


def sap_headers() -> dict[str, str]:
    today = date.today().strftime("%Y%m%d")
    signature = hashlib.md5(f"sugon{SAP_METHOD}{today}sugon".encode("utf-8")).hexdigest().upper()
    return {"Content-Type": "application/json", "method": SAP_METHOD, "sign": signature, "time": today}


class Progress:
    """Minimal stderr progress reporter that also works in non-interactive logs."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.stage = ""
        self.last_logged = 0

    def update(self, stage: str, current: int, total: int) -> None:
        if stage != self.stage:
            self.stage, self.last_logged = stage, 0
            LOGGER.info("阶段开始: %s，目标 %d", stage, total)
        milestone = max(1, total // 10) if total else 1
        if current == total or current - self.last_logged >= milestone:
            LOGGER.info("阶段进度: %s %d/%d", stage, current, total)
            self.last_logged = current
        if self.enabled:
            print(f"\r{stage}: {current}/{total}", end="", file=sys.stderr, flush=True)

    def finish(self) -> None:
        if self.enabled and self.stage:
            print(file=sys.stderr, flush=True)


def configure_logging(level: str, log_file: str | None) -> None:
    LOGGER.handlers.clear()
    LOGGER.setLevel(level)
    LOGGER.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)


def build_collection_a(collection: Any, progress: Progress) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    """Build PCODE -> (AUFNR, KDAUF), retaining only one-pair serial numbers."""
    total = collection.count_documents({})
    pairs_by_sn: dict[str, dict[tuple[str, str], tuple[str, str]]] = {}
    stats = {
        "station_rows_scanned": 0,
        "station_rows_skipped_missing_fields": 0,
        "station_unique_sns": 0,
        "station_ambiguous_sns": 0,
        "collection_a_size": 0,
    }
    cursor = collection.find({}, {"PCODE": 1, "AUFNR": 1, "KDAUF": 1})
    try:
        for document in cursor:
            stats["station_rows_scanned"] += 1
            progress.update("构建工位集合 A", stats["station_rows_scanned"], total)
            sn, production, sales = text(document.get("PCODE")), text(document.get("AUFNR")), text(document.get("KDAUF"))
            if not sn or not production or not sales:
                stats["station_rows_skipped_missing_fields"] += 1
                continue
            pairs_by_sn.setdefault(sn, {})[(production, sales)] = (production, sales)
    finally:
        if hasattr(cursor, "close"):
            cursor.close()

    stats["station_unique_sns"] = len(pairs_by_sn)
    collection_a: dict[str, tuple[str, str]] = {}
    for sn, pairs in pairs_by_sn.items():
        if len(pairs) == 1:
            collection_a[sn] = next(iter(pairs.values()))
        else:
            stats["station_ambiguous_sns"] += 1
    stats["collection_a_size"] = len(collection_a)
    LOGGER.info(
        "集合 A 构建完成: scanned=%d unique_sns=%d usable=%d ambiguous=%d skipped_missing=%d",
        stats["station_rows_scanned"], stats["station_unique_sns"], stats["collection_a_size"],
        stats["station_ambiguous_sns"], stats["station_rows_skipped_missing_fields"],
    )
    return collection_a, stats


def build_repair_pcode_pairs(collection: Any, progress: Progress) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    """Build PCODE -> (AUFNR, VBELN) from repair rows that already have both orders."""
    total = collection.count_documents({})
    pairs_by_sn: dict[str, dict[tuple[str, str], tuple[str, str]]] = {}
    stats = {
        "repair_source_rows_scanned": 0,
        "repair_source_rows_skipped_missing_fields": 0,
        "repair_source_unique_sns": 0,
        "repair_source_ambiguous_sns": 0,
        "repair_collection_b_size": 0,
    }
    cursor = collection.find({}, {"PCODE": 1, "AUFNR": 1, "VBELN": 1})
    try:
        for document in cursor:
            stats["repair_source_rows_scanned"] += 1
            progress.update("构建维修记录集合 B", stats["repair_source_rows_scanned"], total)
            sn, production, sales = text(document.get("PCODE")), text(document.get("AUFNR")), text(document.get("VBELN"))
            if not sn or not production or not sales:
                stats["repair_source_rows_skipped_missing_fields"] += 1
                continue
            pairs_by_sn.setdefault(sn, {})[(production, sales)] = (production, sales)
    finally:
        if hasattr(cursor, "close"):
            cursor.close()

    stats["repair_source_unique_sns"] = len(pairs_by_sn)
    pairs: dict[str, tuple[str, str]] = {}
    for sn, values in pairs_by_sn.items():
        if len(values) == 1:
            pairs[sn] = next(iter(values.values()))
        else:
            stats["repair_source_ambiguous_sns"] += 1
    stats["repair_collection_b_size"] = len(pairs)
    LOGGER.info(
        "集合 B 构建完成: scanned=%d unique_sns=%d usable=%d ambiguous=%d skipped_missing=%d",
        stats["repair_source_rows_scanned"], stats["repair_source_unique_sns"],
        stats["repair_collection_b_size"], stats["repair_source_ambiguous_sns"],
        stats["repair_source_rows_skipped_missing_fields"],
    )
    return pairs, stats


def build_sales_order_pairs(collection: Any, progress: Progress) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    """Build unambiguous AUFNR -> VBELN and AUFNR -> GSTRS indexes from sales details."""
    total = collection.count_documents({})
    sales_candidates: dict[str, set[str]] = {}
    planned_start_candidates: dict[str, set[str]] = {}
    stats = {
        "sales_detail_rows_scanned": 0,
        "sales_detail_rows_skipped_missing_fields": 0,
        "sales_detail_unique_production_orders": 0,
        "sales_detail_ambiguous_production_orders": 0,
        "sales_detail_pair_count": 0,
        "planned_start_unique_production_orders": 0,
        "planned_start_ambiguous_production_orders": 0,
    }
    cursor = collection.find({}, {"aufnr": 1, "gstrs_date": 1, "data.AUFNR": 1, "data.VBELN": 1, "data.GSTRS": 1})
    try:
        for document in cursor:
            stats["sales_detail_rows_scanned"] += 1
            progress.update("构建销售订单明细索引", stats["sales_detail_rows_scanned"], total)
            data = document.get("data") if isinstance(document.get("data"), dict) else {}
            production = normalized_order(data.get("AUFNR") or document.get("aufnr"))
            sales = text(data.get("VBELN"))
            planned_start = text(data.get("GSTRS") or document.get("gstrs_date"))
            if not production:
                stats["sales_detail_rows_skipped_missing_fields"] += 1
                continue
            if sales:
                sales_candidates.setdefault(production, set()).add(sales)
            if planned_start:
                planned_start_candidates.setdefault(production, set()).add(planned_start)
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    stats["sales_detail_unique_production_orders"] = len(sales_candidates)
    pairs = {production: next(iter(values)) for production, values in sales_candidates.items() if len(values) == 1}
    planned_starts = {
        production: next(iter(values))
        for production, values in planned_start_candidates.items()
        if len(values) == 1
    }
    stats["sales_detail_ambiguous_production_orders"] = len(sales_candidates) - len(pairs)
    stats["sales_detail_pair_count"] = len(pairs)
    stats["planned_start_unique_production_orders"] = len(planned_starts)
    stats["planned_start_ambiguous_production_orders"] = len(planned_start_candidates) - len(planned_starts)
    LOGGER.info(
        "销售订单明细索引完成: scanned=%d usable=%d ambiguous=%d skipped_missing=%d",
        stats["sales_detail_rows_scanned"], stats["sales_detail_pair_count"],
        stats["sales_detail_ambiguous_production_orders"], stats["sales_detail_rows_skipped_missing_fields"],
    )
    return pairs, planned_starts, stats


def sap_rows(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        raise RuntimeError("SAP 响应不是 JSON 对象")
    message_type = text(body.get("MSGTY")).upper()
    message = text(body.get("MSGTX"))
    if message_type in {"E", "A", "X"} and "no data" not in message.lower() and "无数据" not in message:
        raise RuntimeError(f"SAP 返回错误 ({message_type}): {message or '未知错误'}")
    rows = body.get("DATA", [])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("SAP 响应缺少有效 DATA 数组")
    return rows


def query_sap_pcode_orders(
    serials: list[str], batch_size: int, retries: int, retry_delay: float, timeout: float,
    progress: Progress, client_factory: Any = httpx.Client,
) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    """Query KK first, then send only no-order SNs to SG."""
    pending = list(dict.fromkeys(serials))
    results: dict[str, dict[str, str]] = {}
    stats = {
        "sap_pcode_candidates": len(pending), "sap_kk_batches": 0, "sap_sg_batches": 0,
        "sap_kk_found": 0, "sap_sg_found": 0, "sap_not_found": 0, "sap_failed_sns": 0,
    }
    processed = 0
    with client_factory(timeout=timeout, trust_env=False) as client:
        for source, url in SAP_SOURCES:
            next_pending: list[str] = []
            for start in range(0, len(pending), batch_size):
                batch = pending[start : start + batch_size]
                stats[f"sap_{source.lower()}_batches"] += 1
                last_error: Exception | None = None
                rows: list[dict[str, Any]] | None = None
                for attempt in range(retries + 1):
                    try:
                        response = client.post(url, json={"SN_LIST": batch}, headers=sap_headers())
                        response.raise_for_status()
                        rows = sap_rows(response.json())
                        break
                    except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError, ValueError) as exc:
                        last_error = exc
                        if attempt < retries:
                            time.sleep(retry_delay * (2**attempt))
                processed += len(batch)
                progress.update("查询 SAP 主机序列号", processed, len(serials) * 2)
                if rows is None:
                    stats["sap_failed_sns"] += len(batch)
                    LOGGER.error("SAP %s 查询失败: sns=%d error=%s", source, len(batch), last_error)
                    continue
                returned = {text(row.get("PCODE")): row for row in rows if text(row.get("PCODE"))}
                for sn in batch:
                    row = returned.get(sn)
                    production = text(row.get("AUFNR")) if row else ""
                    if production:
                        results[sn] = {
                            "AUFNR": production,
                            "VBELN": text(row.get("VBELN") or row.get("KDAUF")),
                            "source": source,
                        }
                        stats[f"sap_{source.lower()}_found"] += 1
                    else:
                        next_pending.append(sn)
            pending = next_pending
            if not pending:
                break
    stats["sap_not_found"] = len(pending)
    return results, stats


def build_sap_pcode_orders(
    repairs: Any, collection_a: dict[str, tuple[str, str]],
    repair_pcode_pairs: dict[str, tuple[str, str]], args: argparse.Namespace,
    progress: Progress, client_factory: Any = httpx.Client,
) -> tuple[dict[str, str], dict[str, int]]:
    """Return SAP production orders for repair rows that do not have AUFNR yet."""
    serials: list[str] = []
    seen: set[str] = set()
    cursor = repairs.find(missing_text_filter("VBELN"), {"PCODE": 1, "AUFNR": 1})
    try:
        for document in cursor:
            sn = text(document.get("PCODE"))
            if sn and not text(document.get("AUFNR")) and sn not in collection_a and sn not in repair_pcode_pairs and sn not in seen:
                seen.add(sn)
                serials.append(sn)
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    results, stats = query_sap_pcode_orders(
        serials, args.sap_batch_size, args.sap_retries, args.sap_retry_delay, args.sap_timeout,
        progress, client_factory,
    )
    orders: dict[str, str] = {}
    stats.update({"sap_response_sales_matches": 0, "sap_found_without_sales": 0, "sap_production_orders": 0})
    for sn, result in results.items():
        production, sales = result["AUFNR"], result["VBELN"]
        if sales:
            stats["sap_response_sales_matches"] += 1
        else:
            stats["sap_found_without_sales"] += 1
        orders[sn] = production
    stats["sap_production_orders"] = len(orders)
    LOGGER.info(
        "SAP 生产订单查询完成: sns=%d production_found=%d without_sales=%d",
        stats["sap_pcode_candidates"], stats["sap_production_orders"], stats["sap_found_without_sales"],
    )
    return orders, stats


def backfill_repairs(
    collection: Any,
    collection_a: dict[str, tuple[str, str]],
    repair_pcode_pairs: dict[str, tuple[str, str]],
    sap_pcode_orders: dict[str, str],
    sales_order_pairs: dict[str, str],
    planned_starts: dict[str, str],
    audit_collection: Any | None,
    run_id: str,
    batch_size: int,
    apply: bool,
    preview_limit: int,
    progress: Progress,
) -> dict[str, Any]:
    repair_filter = {"$or": [missing_text_filter("VBELN"), missing_text_filter("GSTRS")]}
    total = collection.count_documents(repair_filter)
    summary: dict[str, Any] = {
        "repair_candidates": total,
        "repair_order_candidates": 0,
        "planned_start_candidates": 0,
        "planned_start_matches": 0,
        "planned_start_missing_production_order": 0,
        "planned_start_unmatched_production_order": 0,
        "repair_missing_pcode": 0,
        "repair_unmatched_pcode": 0,
        "repair_existing_aufnr_differs": 0,
        "station_skipped_existing_aufnr_conflict": 0,
        "repair_pcode_skipped_existing_aufnr_conflict": 0,
        "station_matches": 0,
        "repair_pcode_matches": 0,
        "sap_pcode_matches": 0,
        "sap_pcode_skipped_existing_aufnr": 0,
        "sales_order_detail_matches": 0,
        "sap_sales_order_detail_matches": 0,
        "would_update": 0,
        "updated": 0,
        "audit_records": 0,
        "preview": [],
    }
    cursor = collection.find(repair_filter, {"_id": 1, "PCODE": 1, "AUFNR": 1, "VBELN": 1, "GSTRS": 1})
    operations: list[UpdateOne] = []
    audit_operations: list[UpdateOne] = []
    scanned = 0

    def write_batch() -> None:
        if not operations:
            return
        if apply:
            if audit_collection is not None:
                audit_collection.bulk_write(audit_operations, ordered=False)
                summary["audit_records"] += len(audit_operations)
            result = collection.bulk_write(operations, ordered=False)
            summary["updated"] += result.modified_count
        operations.clear()
        audit_operations.clear()

    try:
        for document in cursor:
            scanned += 1
            progress.update("匹配并回填维修记录", scanned, total)
            old_production = text(document.get("AUFNR"))
            old_sales = text(document.get("VBELN"))
            old_planned_start = text(document.get("GSTRS"))
            production = old_production
            fields: dict[str, str] = {}
            sources: list[str] = []

            if not old_sales:
                summary["repair_order_candidates"] += 1
                sn = text(document.get("PCODE"))
                pair = None
                source = ""
                if not sn:
                    summary["repair_missing_pcode"] += 1
                else:
                    pair = collection_a.get(sn)
                    source = "station"
                    if pair is None:
                        pair = repair_pcode_pairs.get(sn)
                        source = "repair_pcode"
                    if pair is not None and old_production:
                        mapped_production, mapped_sales = pair
                        if normalized_order(old_production) == normalized_order(mapped_production):
                            pair = (old_production, mapped_sales)
                        else:
                            summary["repair_existing_aufnr_differs"] += 1
                            summary[f"{source}_skipped_existing_aufnr_conflict"] += 1
                            pair = None
                    if pair is None:
                        production_from_sap = False
                        if not production:
                            production = sap_pcode_orders.get(sn, "")
                            production_from_sap = bool(production)
                        sales = sales_order_pairs.get(normalized_order(production), "")
                        if production and sales:
                            pair = (production, sales)
                            source = "sap_sales_order_detail" if production_from_sap else "sales_order_detail"
                        elif production_from_sap:
                            # The SAP SN interface authoritatively supplies AUFNR but not VBELN.
                            pair = (production, None)
                            source = "sap_pcode"
                if pair is None:
                    summary["repair_unmatched_pcode"] += 1
                else:
                    summary[f"{source}_matches"] += 1
                    production, sales = pair
                    fields["AUFNR"] = production
                    if sales is not None:
                        fields["VBELN"] = sales
                    sources.append(source)

            if not old_planned_start:
                summary["planned_start_candidates"] += 1
                planned_start = planned_starts.get(normalized_order(production), "")
                if planned_start:
                    fields["GSTRS"] = planned_start
                    sources.append("sales_order_planned_start")
                    summary["planned_start_matches"] += 1
                elif not production:
                    summary["planned_start_missing_production_order"] += 1
                else:
                    summary["planned_start_unmatched_production_order"] += 1

            if not fields:
                continue
            summary["would_update"] += 1
            source = "+".join(sources)
            if len(summary["preview"]) < preview_limit:
                summary["preview"].append({
                    "id": str(document["_id"]),
                    "PCODE": text(document.get("PCODE")),
                    "source": source,
                    "AUFNR": {"before": old_production, "after": production},
                    "VBELN": {"before": old_sales, "after": fields.get("VBELN", old_sales)},
                    "GSTRS": {"before": old_planned_start, "after": fields.get("GSTRS", old_planned_start)},
                })
            write_conditions = [{"_id": document["_id"]}]
            if "AUFNR" in fields or "VBELN" in fields:
                write_conditions.append(missing_text_filter("VBELN"))
            if "GSTRS" in fields:
                write_conditions.append(missing_text_filter("GSTRS"))
            operations.append(UpdateOne(
                {"$and": write_conditions},
                {"$set": fields},
            ))
            if audit_collection is not None:
                audit_operations.append(UpdateOne(
                    {"_id": f"{run_id}:{document['_id']}"},
                    {
                        "$set": {
                            "run_id": run_id,
                            "repair_id": document["_id"],
                            "PCODE": text(document.get("PCODE")),
                            "source": source,
                            "before": {"AUFNR": old_production, "VBELN": old_sales, "GSTRS": old_planned_start},
                            "after": {"AUFNR": fields.get("AUFNR", old_production), "VBELN": fields.get("VBELN", old_sales), "GSTRS": fields.get("GSTRS", old_planned_start)},
                            "fields": fields,
                            "write_status": "attempted",
                            "updated_at": datetime.now(timezone.utc),
                        },
                        "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
                    },
                    upsert=True,
                ))
            if len(operations) >= batch_size:
                write_batch()
        write_batch()
    finally:
        if hasattr(cursor, "close"):
            cursor.close()
    LOGGER.info(
        "维修回填完成: candidates=%d station_matched=%d repair_pcode_matched=%d sap_pcode_matched=%d updated=%d unmatched=%d missing_pcode=%d",
        total, summary["station_matches"], summary["repair_pcode_matches"], summary["sap_pcode_matches"], summary["updated"],
        summary["repair_unmatched_pcode"], summary["repair_missing_pcode"],
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="增量同步 HANA 维修故障记录，并回填缺失订单和计划生产时间")
    parser.add_argument("--batch-size", type=int, default=int(env("SYNC_BATCH_SIZE", "1000")), help="MongoDB 批量写入大小")
    parser.add_argument("--preview-limit", type=int, default=20, help="JSON 摘要中展示的回填样本数量")
    parser.add_argument(
        "--skip-hana-sync", dest="sync_repair_hana", action="store_false",
        help="仅执行现有维修记录的回填，不从 HANA 增量同步",
    )
    parser.add_argument(
        "--sync-end-date", type=parse_date, default=date.today(),
        help="HANA 维修增量同步结束日期，默认今天",
    )
    parser.add_argument(
        "--sync-lookback-days", type=int, default=int(env("SYNC_LOOKBACK_DAYS", "7")),
        help="HANA 维修增量同步水位线回看天数",
    )
    backfill_mode = parser.add_mutually_exclusive_group()
    backfill_mode.add_argument(
        "--one-click", dest="one_click", action="store_true",
        help="启用工位、维修表、SAP SN 和销售订单明细四级回填来源（默认）",
    )
    backfill_mode.add_argument(
        "--basic-backfill", dest="one_click", action="store_false",
        help="仅使用工位记录集合 A 回填订单字段",
    )
    parser.add_argument(
        "--repair-pcode-fallback", action="store_true",
        help="集合 A 未命中时，使用同一 PCODE 的维修表唯一完整订单作为第二级回填来源",
    )
    parser.add_argument(
        "--sap-pcode-fallback", action="store_true",
        help="集合 A/B 未命中且 AUFNR 为空时，批量查询 SAP SN 接口，仅回填生产订单",
    )
    parser.add_argument(
        "--sales-order-fallback", action="store_true",
        help="通过已同步的 SAP 销售订单明细按 AUFNR 反查唯一 VBELN 并回填",
    )
    parser.add_argument(
        "--planned-start-fallback", action="store_true",
        help="通过已同步的 SAP 销售订单明细按 AUFNR 反查唯一 GSTRS 并回填",
    )
    parser.add_argument(
        "--allow-partial-sap", action="store_true",
        help="允许 SAP SN 查询存在失败批次时继续写入；默认 apply 模式会阻断",
    )
    parser.add_argument("--sap-batch-size", type=int, default=int(env("SN_BATCH_SIZE", "100")), help="每次 SAP SN 查询数量")
    parser.add_argument("--sap-retries", type=int, default=int(env("SN_RETRIES", "2")), help="SAP 请求重试次数")
    parser.add_argument("--sap-retry-delay", type=float, default=float(env("SN_RETRY_DELAY", "1")), help="SAP 首次重试等待秒数")
    parser.add_argument("--sap-timeout", type=float, default=float(env("HTTP_TIMEOUT", "120")), help="SAP 请求超时秒数")
    parser.add_argument("--no-progress", action="store_true", help="关闭终端进度显示")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), default=env("LOG_LEVEL", "INFO"))
    parser.add_argument("--log-file", default=env("LOG_FILE", ""), help="追加写入日志文件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="仅生成预览和汇总，不写入（默认）")
    mode.add_argument("--apply", action="store_true", help="执行 AUFNR 和 VBELN 回填")
    parser.set_defaults(
        repair_collection=env("REPAIR_COLLECTION", "repair_records_sap"),
        station_collection=env("STATION_COLLECTION", "station_records_sap"),
        sales_order_collection=env("TARGET_COLLECTION", "sales_orders_sap"),
        audit_collection=env("REPAIR_ORDER_BACKFILL_AUDIT_COLLECTION", "repair_order_backfill_audit"),
        sync_repair_hana=True,
        one_click=True,
    )
    return parser


def resolve_sources(args: argparse.Namespace) -> None:
    """Enable the complete deterministic source chain for one-click execution."""
    if getattr(args, "one_click", False):
        args.repair_pcode_fallback = True
        args.sap_pcode_fallback = True
        args.sales_order_fallback = True
        args.planned_start_fallback = True


def run_workflow(
    db: Any, args: argparse.Namespace, sap_client_factory: Any = httpx.Client,
    run_id: str | None = None,
) -> dict[str, Any]:
    resolve_sources(args)
    run_id = run_id or uuid4().hex
    progress = Progress(not args.no_progress)
    hana_sync: dict[str, Any] = {}
    hana_sync_commit: dict[str, Any] = {}
    try:
        if getattr(args, "sync_repair_hana", True):
            progress.update("同步 HANA 维修故障记录", 0, 1)
            hana_sync = sync_sales_orders.sync_repair(
                db,
                args.repair_collection,
                env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints"),
                "incremental",
                None,
                args.sync_end_date,
                args.sync_lookback_days,
                args.batch_size,
                not args.apply,
                run_id,
                defer_finalize=args.apply,
            )
            progress.update("同步 HANA 维修故障记录", 1, 1)
            if not hana_sync.get("success"):
                raise RuntimeError(f"HANA 维修故障记录同步失败: {hana_sync.get('error', '未知错误')}")
        collection_a, station_stats = build_collection_a(db[args.station_collection], progress)
        repair_pcode_pairs: dict[str, tuple[str, str]] = {}
        repair_source_stats: dict[str, Any] = {}
        if args.repair_pcode_fallback:
            repair_pcode_pairs, repair_source_stats = build_repair_pcode_pairs(db[args.repair_collection], progress)
        sap_pcode_orders: dict[str, str] = {}
        sap_stats: dict[str, Any] = {}
        if args.sap_pcode_fallback:
            sap_pcode_orders, sap_stats = build_sap_pcode_orders(
                db[args.repair_collection], collection_a, repair_pcode_pairs,
                args, progress, sap_client_factory,
            )
            if args.apply and sap_stats["sap_failed_sns"] and not getattr(args, "allow_partial_sap", False):
                raise RuntimeError(
                    f"SAP SN 查询失败 {sap_stats['sap_failed_sns']} 个序列号，已阻断写入；"
                    "请修复 SAP 连接后重试，或显式使用 --allow-partial-sap"
                )
        sales_order_pairs: dict[str, str] = {}
        planned_starts: dict[str, str] = {}
        sales_detail_stats: dict[str, Any] = {}
        if args.sales_order_fallback or getattr(args, "planned_start_fallback", False):
            sales_order_pairs, planned_starts, sales_detail_stats = build_sales_order_pairs(
                db[args.sales_order_collection], progress,
            )
        repair_stats = backfill_repairs(
            db[args.repair_collection], collection_a, repair_pcode_pairs, sap_pcode_orders, sales_order_pairs, planned_starts,
            db[args.audit_collection] if args.apply and getattr(args, "audit_collection", "") else None,
            run_id,
            args.batch_size, args.apply,
            args.preview_limit, progress,
        )
        if getattr(args, "sync_repair_hana", True) and args.apply:
            hana_sync_commit = sync_sales_orders.finalize_repair_run(
                db,
                args.repair_collection,
                env("SYNC_CHECKPOINT_COLLECTION", "sync_checkpoints"),
                hana_sync,
                run_id,
            )
    finally:
        progress.finish()
    return {
        "success": True,
        "mode": "apply" if args.apply else "dry-run",
        "repair_collection": args.repair_collection,
        "station_collection": args.station_collection,
        "sales_order_collection": args.sales_order_collection,
        "repair_pcode_fallback": args.repair_pcode_fallback,
        "sap_pcode_fallback": args.sap_pcode_fallback,
        "sales_order_fallback": args.sales_order_fallback,
        "planned_start_fallback": getattr(args, "planned_start_fallback", False),
        "one_click": getattr(args, "one_click", False),
        "sync_repair_hana": getattr(args, "sync_repair_hana", True),
        "hana_sync": hana_sync,
        "hana_sync_commit": hana_sync_commit,
        "audit_collection": getattr(args, "audit_collection", ""),
        "run_id": run_id,
        **station_stats,
        **repair_source_stats,
        **sap_stats,
        **sales_detail_stats,
        **repair_stats,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.batch_size < 1 or args.sap_batch_size < 1:
        raise ValueError("--batch-size 和 --sap-batch-size 必须大于 0")
    if args.sync_lookback_days < 0:
        raise ValueError("--sync-lookback-days 不能为负数")
    if args.sap_retries < 0 or args.sap_retry_delay < 0 or args.sap_timeout <= 0:
        raise ValueError("SAP retries/retry-delay 不能为负数，timeout 必须大于 0")
    if args.preview_limit < 0:
        raise ValueError("--preview-limit 不能小于 0")
    load_dotenv()
    database = env("MONGODB_DATABASE")
    if not database:
        raise ValueError("MONGODB_DATABASE 未配置")
    options = mongo_client_options()
    client = MongoClient(mongo_uri(), **options)
    started_at = datetime.now(timezone.utc)
    run_id = uuid4().hex
    try:
        db = client[database]
        lock_path = env("REPAIR_STATION_BACKFILL_LOCK_PATH", DEFAULT_LOCK)
        with process_lock(lock_path):
            with process_lock(env("SYNC_LOCK_PATH", "/tmp/line-fault-table-sync.lock")):
                with mongo_lease_lock(db, env("REPAIR_STATION_BACKFILL_LOCK_NAME", DEFAULT_MONGO_LOCK)):
                    result = run_workflow(db, args, run_id=run_id)
        result.update({
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc),
            "mongo_write_concern": mongo_write_concern_summary(options),
        })
        return result
    finally:
        client.close()


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    configure_logging(args.log_level, args.log_file or None)
    try:
        print(json.dumps(run(args), ensure_ascii=False, default=str, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
