"""Shared SAP request signatures and strict response validation."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any


def headers(method: str) -> dict[str, str]:
    today = datetime.now().strftime("%Y%m%d")
    signature = hashlib.md5(f"sugon{method}{today}sugon".encode()).hexdigest().upper()
    return {"Content-Type": "application/json", "method": method, "sign": signature, "time": today}


def records(body: Any, source: str = "SAP") -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        raise RuntimeError(f"{source}: 响应不是 JSON 对象")
    message = str(body.get("MSGTX") or "").strip()
    kind = str(body.get("MSGTY") or "").upper()
    if kind in {"E", "A", "X"}:
        if "no data" in message.lower() or "无数据" in message:
            return []
        raise RuntimeError(f"{source}: SAP 返回错误 ({kind}): {message or 'Unknown SAP error'}")
    rows = body.get("DATA")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{source}: 响应缺少有效 DATA 数组")
    return rows
