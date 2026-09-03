#!/usr/bin/env python3
"""One-time, read-only export of every HANA fault record to an Excel workbook."""
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
from typing import Any
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sources.hana.hana_view_sync import load_dotenv
from scripts.sources.sap_http.sync_sales_orders import REPAIR_COLUMNS, REPAIR_VIEW, hana_connection


EXCEL_MAX_DATA_ROWS = 1_048_575
DEFAULT_BATCH_SIZE = 5_000
DEFAULT_OUTPUT_DIR = ROOT / "outputs"
INVALID_XML_CHARACTERS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将 HANA 故障视图的全部记录导出为 Excel")
    parser.add_argument(
        "--output",
        type=Path,
        help="输出 .xlsx 路径；默认保存到 outputs/hana_faults_YYYYMMDD_HHMMSS.xlsx",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="每次从 HANA 读取的行数")
    parser.add_argument("--limit", type=int, help="仅导出前 N 条记录，用于连通性验证")
    return parser.parse_args()


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def excel_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def xml_text(value: str) -> str:
    cleaned = INVALID_XML_CHARACTERS.sub("", value)
    return escape(cleaned, {'"': "&quot;"})


def column_width(column: str) -> int:
    if column in {"ZGZMS", "ERROR_MSG", "RPDESC", "RNOTE", "FIX_REMARKS", "TEST_LOG_NAME"}:
        return 30
    if column in {"PCODE", "ZMCOD1", "ZRCOD1", "ZMCOD2", "ZRCOD2", "AUFNR", "VBELN"}:
        return 18
    return 14


def write_text(handle: Any, content: str) -> None:
    handle.write(content.encode("utf-8"))


def write_cell(handle: Any, row: int, column: int, value: str, style: int = 0) -> None:
    reference = f"{excel_column_name(column)}{row}"
    style_attribute = f' s="{style}"' if style else ""
    write_text(
        handle,
        f'<c r="{reference}" t="inlineStr"{style_attribute}><is><t xml:space="preserve">{xml_text(value)}</t></is></c>',
    )


def start_sheet(handle: Any, columns: tuple[str, ...]) -> None:
    write_text(
        handle,
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/><cols>',
    )
    for index, column in enumerate(columns, start=1):
        write_text(handle, f'<col min="{index}" max="{index}" width="{column_width(column)}" customWidth="1"/>')
    write_text(handle, '</cols><sheetData><row r="1" ht="30" customHeight="1">')
    for index, column in enumerate(columns, start=1):
        write_cell(handle, 1, index, column, style=1)
    write_text(handle, '</row>')


def write_data_row(handle: Any, row_number: int, values: tuple[Any, ...]) -> None:
    write_text(handle, f'<row r="{row_number}">')
    for index, value in enumerate(values, start=1):
        text = as_text(value)
        if text is not None:
            write_cell(handle, row_number, index, text)
    write_text(handle, '</row>')


def finish_sheet(handle: Any) -> None:
    write_text(handle, '</sheetData></worksheet>')


def write_workbook_metadata(archive: zipfile.ZipFile, sheet_names: list[str]) -> None:
    sheet_entries = "".join(
        f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    relationship_entries = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheet_names) + 1)
    )
    content_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheet_names) + 1)
    )
    archive.writestr(
        "[Content_Types].xml",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f'{content_overrides}</Types>',
    )
    archive.writestr(
        "_rels/.rels",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>',
    )
    archive.writestr(
        "xl/workbook.xml",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{sheet_entries}</sheets></workbook>',
    )
    archive.writestr(
        "xl/_rels/workbook.xml.rels",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{relationship_entries}'
        f'<Relationship Id="rId{len(sheet_names) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>',
    )
    archive.writestr(
        "xl/styles.xml",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" applyFont="1" applyFill="1" applyAlignment="1" xfId="0"><alignment horizontal="center" vertical="center" wrapText="1"/></xf></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>',
    )


def export_hana_records(output_path: Path, batch_size: int, limit: int | None) -> tuple[int, list[str]]:
    selected = ", ".join(f'"{column}"' for column in REPAIR_COLUMNS)
    order_by = ", ".join(f'"{column}"' for column in ("PCODE", "ZWXDT", "ZMCOD1", "ZDATE_WX", "ZTIME"))
    sql = f"SELECT {selected} FROM {REPAIR_VIEW} ORDER BY {order_by}"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output_path.stem}_", suffix=".xlsx", dir=output_path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    row_count = 0
    sheet_names: list[str] = []
    try:
        with zipfile.ZipFile(temporary_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            sheet_number = 0
            sheet_rows = 0
            sheet_handle: Any | None = None

            def open_sheet() -> Any:
                nonlocal sheet_number, sheet_rows
                sheet_number += 1
                sheet_rows = 0
                sheet_names.append(f"Faults_{sheet_number}")
                handle = archive.open(f"xl/worksheets/sheet{sheet_number}.xml", mode="w")
                start_sheet(handle, REPAIR_COLUMNS)
                return handle

            with hana_connection() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute(sql)
                    sheet_handle = open_sheet()
                    while rows := cursor.fetchmany(batch_size):
                        for values in rows:
                            if sheet_rows == EXCEL_MAX_DATA_ROWS:
                                finish_sheet(sheet_handle)
                                sheet_handle.close()
                                sheet_handle = open_sheet()
                            sheet_rows += 1
                            row_count += 1
                            write_data_row(sheet_handle, sheet_rows + 1, values)
                            if limit is not None and row_count >= limit:
                                break
                        if limit is not None and row_count >= limit:
                            break
                finally:
                    cursor.close()
            if sheet_handle is not None:
                finish_sheet(sheet_handle)
                sheet_handle.close()
            write_workbook_metadata(archive, sheet_names)
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return row_count, sheet_names


def main() -> int:
    args = parse_args()
    if args.batch_size < 1 or (args.limit is not None and args.limit < 1):
        raise SystemExit("--batch-size 和 --limit 必须大于 0")
    load_dotenv()
    output_path = (args.output or DEFAULT_OUTPUT_DIR / f"hana_faults_{datetime.now():%Y%m%d_%H%M%S}.xlsx").resolve()
    if output_path.suffix.lower() != ".xlsx":
        raise SystemExit("--output 必须是 .xlsx 文件")
    source_rows, sheets = export_hana_records(output_path, args.batch_size, args.limit)
    print(json.dumps({"success": True, "sourceRows": source_rows, "outputPath": str(output_path), "sheets": sheets}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"导出失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
