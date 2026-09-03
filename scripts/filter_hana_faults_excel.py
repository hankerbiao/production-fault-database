#!/usr/bin/env python3
"""One-time streaming filter for a HANA fault-export workbook."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_hana_faults_to_excel import (
    DEFAULT_OUTPUT_DIR,
    EXCEL_MAX_DATA_ROWS,
    REPAIR_COLUMNS,
    finish_sheet,
    start_sheet,
    write_data_row,
    write_workbook_metadata,
)


DEFAULT_INPUT = DEFAULT_OUTPUT_DIR / "hana_faults_full.xlsx"
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIR / "hana_faults_20260101_to_20260731.xlsx"
DATE_FIELD = "ZDATE_WX"
CELL_REFERENCE = re.compile(r"([A-Z]+)")
WORKSHEET_PART = re.compile(r"xl/worksheets/sheet(\d+)\.xml$")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="筛选 HANA 故障导出 Excel 中指定维修日期范围的记录")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="源 .xlsx 路径")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="筛选后的 .xlsx 路径")
    parser.add_argument("--date-from", type=parse_date, default=date(2026, 1, 1), help="起始日期，含当天")
    parser.add_argument("--date-to", type=parse_date, default=date(2026, 7, 31), help="结束日期，含当天")
    return parser.parse_args()


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", maxsplit=1)[-1]


def column_index(reference: str) -> int | None:
    match = CELL_REFERENCE.match(reference)
    if not match:
        return None
    result = 0
    for character in match.group(1):
        result = result * 26 + ord(character) - 64
    return result - 1


def row_values(row: ET.Element) -> tuple[str | None, ...]:
    values: list[str | None] = [None] * len(REPAIR_COLUMNS)
    for cell in row:
        if local_name(cell) != "c":
            continue
        index = column_index(cell.attrib.get("r", ""))
        if index is None or index >= len(values):
            continue
        value = "".join(cell.itertext())
        values[index] = value or None
    return tuple(values)


def worksheet_parts(archive: zipfile.ZipFile) -> list[str]:
    parts: list[tuple[int, str]] = []
    for name in archive.namelist():
        match = WORKSHEET_PART.fullmatch(name)
        if match:
            parts.append((int(match.group(1)), name))
    return [name for _, name in sorted(parts)]


def iter_data_rows(archive: zipfile.ZipFile) -> Iterator[tuple[str | None, ...]]:
    expected_header = tuple(REPAIR_COLUMNS)
    for part in worksheet_parts(archive):
        with archive.open(part) as handle:
            for _, row in ET.iterparse(handle, events=("end",)):
                if local_name(row) != "row":
                    continue
                values = row_values(row)
                if row.attrib.get("r") == "1":
                    header = tuple(value or "" for value in values)
                    if header != expected_header:
                        raise ValueError(f"{part} 的列名与 HANA 故障导出格式不匹配")
                else:
                    yield values
                row.clear()


def repair_date(value: str | None) -> date | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def filter_workbook(input_path: Path, output_path: Path, start_date: date, end_date: date) -> tuple[int, int, list[str]]:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("--input 和 --output 不能是同一个文件")
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到源文件: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output_path.stem}_", suffix=".xlsx", dir=output_path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    scanned_rows = 0
    kept_rows = 0
    sheet_names: list[str] = []
    date_index = REPAIR_COLUMNS.index(DATE_FIELD)
    try:
        with zipfile.ZipFile(input_path) as source, zipfile.ZipFile(
            temporary_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as output:
            sheet_number = 0
            sheet_rows = 0
            sheet_handle: Any | None = None

            def open_sheet() -> Any:
                nonlocal sheet_number, sheet_rows
                sheet_number += 1
                sheet_rows = 0
                sheet_names.append(f"Faults_{sheet_number}")
                handle = output.open(f"xl/worksheets/sheet{sheet_number}.xml", mode="w")
                start_sheet(handle, REPAIR_COLUMNS)
                return handle

            sheet_handle = open_sheet()
            for values in iter_data_rows(source):
                scanned_rows += 1
                value = repair_date(values[date_index])
                if value is None or not start_date <= value <= end_date:
                    continue
                if sheet_rows == EXCEL_MAX_DATA_ROWS:
                    finish_sheet(sheet_handle)
                    sheet_handle.close()
                    sheet_handle = open_sheet()
                sheet_rows += 1
                kept_rows += 1
                write_data_row(sheet_handle, sheet_rows + 1, values)
            finish_sheet(sheet_handle)
            sheet_handle.close()
            write_workbook_metadata(output, sheet_names)
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return scanned_rows, kept_rows, sheet_names


def main() -> int:
    args = parse_args()
    if args.date_from > args.date_to:
        raise SystemExit("--date-from 不能晚于 --date-to")
    scanned_rows, kept_rows, sheet_names = filter_workbook(
        args.input.resolve(), args.output.resolve(), args.date_from, args.date_to
    )
    print(
        json.dumps(
            {
                "success": True,
                "dateField": DATE_FIELD,
                "dateFrom": args.date_from.isoformat(),
                "dateTo": args.date_to.isoformat(),
                "scannedRows": scanned_rows,
                "keptRows": kept_rows,
                "outputPath": str(args.output.resolve()),
                "sheets": sheet_names,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"筛选失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
