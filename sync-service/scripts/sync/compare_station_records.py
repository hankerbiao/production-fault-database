#!/usr/bin/env python3
"""Read-only daily comparison of station records in HANA and MongoDB."""
from __future__ import annotations

import json
import os
import sys
import calendar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymongo import MongoClient

from scripts.sources.hana import hana_view_sync as hana
from scripts.sync.station_records_sales_orders import FIELDS, key_for, load_production_orders, values_for


EXAMPLE_LIMIT = 100


def comparison_window(now: datetime | None = None) -> tuple[datetime, datetime, str, str]:
    shanghai = ZoneInfo("Asia/Shanghai")
    if now is None:
        local_now = datetime.now(shanghai)
    elif now.tzinfo is None:
        local_now = now.replace(tzinfo=shanghai)
    else:
        local_now = now.astimezone(shanghai)
    month_index = local_now.year * 12 + local_now.month - 1 - 3
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(local_now.day, calendar.monthrange(year, month)[1])
    start = datetime(year, month, day, local_now.hour, local_now.minute, local_now.second)
    end = local_now.replace(tzinfo=None)
    return start, end, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def notify_anomalies(summary: dict[str, Any]) -> dict[str, Any] | None:
    if not summary["has_anomalies"]:
        return None
    token = hana.env("GQUAN_APP_TOKEN")
    if not token:
        return {"success": False, "error": "未配置 GQUAN_APP_TOKEN"}
    url = hana.env("GQUAN_APP_URL", "https://one.1oa.com.cn/springboard/api/v1/integrations/gquan/app")
    recipient = hana.env("GQUAN_APP_TO", "libiao1")
    title = "工位过站数据比对异常"
    desc = (
        f"近 3 个月 HANA 与 MongoDB 工位过站记录比对发现差异。"
        f"缺失 {summary['missing_in_mongo']} 条，多余 {summary['extra_in_mongo']} 条，"
        f"字段不一致 {summary['field_mismatches']} 条，重复记录 {summary['duplicate_rows']} 条。"
        f"运行编号：{summary['run_id']}"
    )
    payload = json.dumps({"to": [recipient], "title": title, "desc": desc, "content": desc,
                          "msg_type": "AGENDA"}).encode()
    request = Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    try:
        with urlopen(request, timeout=float(hana.env("GQUAN_APP_TIMEOUT", "15"))) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            if not 200 <= response.status < 300:
                return {"success": False, "error": f"光圈接口返回 HTTP {response.status}: {body}"}
            return {"success": True, "status": response.status}
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {"success": False, "error": str(exc)}


def compare(db: Any) -> dict[str, Any]:
    spec = hana.configured_spec("Z_V_ZMES_T_001_SALES_ORDERS")
    collection = db[spec.collection]
    batch_size = int(hana.env("SYNC_BATCH_SIZE", "1000"))
    order_batch_size = int(hana.env("STATION_ORDER_BATCH_SIZE", "100"))
    if batch_size < 1 or order_batch_size < 1:
        raise ValueError("SYNC_BATCH_SIZE 和 STATION_ORDER_BATCH_SIZE 必须大于 0")

    cutoff, through, cutoff_text, through_text = comparison_window()
    summary: dict[str, Any] = {
        "success": False, "run_id": uuid4().hex, "view_id": spec.view_id,
        "collection": spec.collection, "candidate_orders": 0, "matched_orders": 0,
        "source_rows": 0, "mongo_rows": 0, "missing_in_mongo": 0,
        "extra_in_mongo": 0, "field_mismatches": 0, "duplicate_rows": 0,
        "has_anomalies": False, "examples": [], "started_at": datetime.now(timezone.utc),
        "range": {"start": cutoff_text, "end": through_text, "timezone": "Asia/Shanghai"},
    }
    examples: list[dict[str, Any]] = summary["examples"]
    time_expression = 'TRIM(COALESCE("SPEC_TIME", \'\'))'
    time_filter = f"{time_expression} >= ? AND {time_expression} <= ?"
    candidate_sql = f'SELECT DISTINCT "AUFNR" FROM {spec.qualified_name} WHERE {time_filter}'
    columns = ", ".join(f'"{field}"' for field in spec.columns)
    with hana.hana_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(candidate_sql, (cutoff_text, through_text))
            candidates: set[str] = set()
            while rows := cursor.fetchmany(batch_size):
                candidates.update(str(row[0] or "").strip() for row in rows if str(row[0] or "").strip())
            production_orders = set(load_production_orders(db[hana.env("TARGET_COLLECTION", "sales_orders_sap")]))
            # Include current production orders even when HANA has no row in the
            # window. This lets the comparison report stale MongoDB rows.
            matched_orders = sorted(production_orders)
            summary["candidate_orders"] = len(candidates)
            summary["matched_orders"] = len(matched_orders)
            projection = {field: 1 for field in spec.columns}

            for offset in range(0, len(matched_orders), order_batch_size):
                orders = matched_orders[offset:offset + order_batch_size]
                placeholders = ", ".join("?" for _ in orders)
                cursor.execute(
                    f'SELECT {columns} FROM {spec.qualified_name} '
                    f'WHERE {time_filter} AND TRIM(COALESCE("AUFNR", \'\')) IN ({placeholders})',
                    (cutoff_text, through_text, *orders),
                )
                source_by_key: dict[str, dict[str, Any]] = {}
                source_duplicates = 0
                while rows := cursor.fetchmany(batch_size):
                    for values in rows:
                        document = {field: hana.bson_value(value) for field, value in zip(spec.columns, values, strict=True)}
                        identity = values_for(document)
                        row_key = key_for(document)
                        if row_key in source_by_key:
                            source_duplicates += 1
                            if len(examples) < EXAMPLE_LIMIT:
                                examples.append({"kind": "duplicate_source", "key": identity})
                            continue
                        source_by_key[row_key] = document

                mongo_filter = {"$and": [
                    {"$or": [
                        {"AUFNR_NORMALIZED": {"$in": orders}},
                        {"AUFNR": {"$in": orders}},
                    ]},
                    {"$or": [
                        {"SPEC_TIME": {"$gte": cutoff_text, "$lte": through_text}},
                        {"SPEC_TIME": {"$gte": cutoff, "$lte": through}},
                    ]},
                ]}
                target_by_key: dict[str, dict[str, Any]] = {}
                target_duplicates = 0
                mongo_cursor = collection.find(mongo_filter, projection).batch_size(batch_size)
                try:
                    for document in mongo_cursor:
                        identity = values_for(document)
                        row_key = key_for(document)
                        if row_key in target_by_key:
                            target_duplicates += 1
                            if len(examples) < EXAMPLE_LIMIT:
                                examples.append({"kind": "duplicate_mongo", "key": identity})
                            continue
                        target_by_key[row_key] = document
                finally:
                    mongo_cursor.close()

                summary["source_rows"] += len(source_by_key) + source_duplicates
                summary["mongo_rows"] += len(target_by_key) + target_duplicates
                summary["duplicate_rows"] += source_duplicates + target_duplicates
                for row_key, source in source_by_key.items():
                    target = target_by_key.get(row_key)
                    identity = values_for(source)
                    if target is None:
                        summary["missing_in_mongo"] += 1
                        if len(examples) < EXAMPLE_LIMIT:
                            examples.append({"kind": "missing_in_mongo", "key": identity})
                        continue
                    changed = [field for field in spec.columns if canonical(source.get(field)) != canonical(target.get(field))]
                    if changed:
                        summary["field_mismatches"] += 1
                        if len(examples) < EXAMPLE_LIMIT:
                            examples.append({"kind": "field_mismatch", "key": identity, "fields": changed[:20]})
                for row_key, target in target_by_key.items():
                    if row_key not in source_by_key:
                        summary["extra_in_mongo"] += 1
                        if len(examples) < EXAMPLE_LIMIT:
                            examples.append({"kind": "extra_in_mongo", "key": values_for(target)})
                hana.progress_log(
                    f"[data-check] 订单批次 {offset // order_batch_size + 1} 完成："
                    f"源端 {len(source_by_key) + source_duplicates:,} 条，MongoDB {len(target_by_key) + target_duplicates:,} 条"
                )
        finally:
            cursor.close()

    summary["has_anomalies"] = any(summary[name] for name in (
        "missing_in_mongo", "extra_in_mongo", "field_mismatches", "duplicate_rows",
    ))
    summary["finished_at"] = datetime.now(timezone.utc)
    summary["notification"] = notify_anomalies(summary)
    summary["success"] = True
    return summary


def main() -> int:
    hana.load_dotenv()
    client = None
    try:
        client = MongoClient(hana.mongo_uri(), **hana.mongo_client_options())
        database = hana.env("MONGODB_DATABASE")
        if not database:
            raise RuntimeError("MONGODB_DATABASE 未配置")
        result = compare(client[database])
        print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
