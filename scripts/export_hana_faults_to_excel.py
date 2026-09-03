#!/usr/bin/env python3
"""One-time export of every HANA fault record to an Excel workbook.

The script is read-only against HANA.  It stages records as JSON Lines so the
workbook writer can stream rows into separate sheets when Excel's row limit is
reached.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sources.hana.hana_view_sync import load_dotenv
from scripts.sources.sap_http.sync_sales_orders import REPAIR_COLUMNS, REPAIR_VIEW, hana_connection


EXCEL_MAX_DATA_ROWS = 1_048_575
DEFAULT_BATCH_SIZE = 5_000
DEFAULT_OUTPUT_DIR = ROOT / "outputs"


NODE_BUILDER = r'''
import fs from "node:fs/promises";
import path from "node:path";
import readline from "node:readline";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [inputPath, outputPath] = process.argv.slice(2);
const MAX_DATA_ROWS = 1_048_575;
const WRITE_BATCH_SIZE = 2_000;

function cellValue(value) {
  if (value === null || value === undefined) return null;
  const text = String(value);
  return text.startsWith("=") ? "'" + text : text;
}

function columnWidth(column) {
  if (["ZGZMS", "ERROR_MSG", "RPDESC", "RNOTE", "FIX_REMARKS", "TEST_LOG_NAME"].includes(column)) return 30;
  if (["PCODE", "ZMCOD1", "ZRCOD1", "ZMCOD2", "ZRCOD2", "AUFNR", "VBELN"].includes(column)) return 18;
  return 14;
}

function formatSheet(sheet, columns, dataRows) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  const header = sheet.getRangeByIndexes(0, 0, 1, columns.length);
  header.format = {
    fill: "#1F4E78",
    font: { bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
  };
  header.format.rowHeight = 30;
  for (let index = 0; index < columns.length; index += 1) {
    sheet.getRangeByIndexes(0, index, dataRows + 1, 1).format.columnWidth = columnWidth(columns[index]);
  }
}

const input = readline.createInterface({
  input: (await fs.open(inputPath)).createReadStream(),
  crlfDelay: Infinity,
});
let columns = null;
let workbook = null;
let sheet = null;
let sheetNumber = 0;
let rowsInSheet = 0;
let totalRows = 0;
let pendingRows = [];
const sheets = [];

function createSheet() {
  if (sheet) formatSheet(sheet, columns, rowsInSheet);
  sheetNumber += 1;
  sheet = workbook.worksheets.add(`Faults_${sheetNumber}`);
  sheet.getRangeByIndexes(0, 0, 1, columns.length).values = [columns];
  rowsInSheet = 0;
  sheets.push(sheet.name);
}

function flushRows() {
  if (pendingRows.length === 0) return;
  sheet.getRangeByIndexes(rowsInSheet + 1, 0, pendingRows.length, columns.length).values = pendingRows;
  rowsInSheet += pendingRows.length;
  totalRows += pendingRows.length;
  pendingRows = [];
}

for await (const line of input) {
  if (!line) continue;
  const record = JSON.parse(line);
  if (columns === null) {
    columns = record.columns;
    workbook = Workbook.create();
    createSheet();
    continue;
  }
  if (rowsInSheet + pendingRows.length >= MAX_DATA_ROWS) {
    flushRows();
    createSheet();
  }
  pendingRows.push(record.map(cellValue));
  if (pendingRows.length >= WRITE_BATCH_SIZE) flushRows();
}

if (columns === null) throw new Error("The staged HANA export is missing its header.");
flushRows();
formatSheet(sheet, columns, rowsInSheet);
await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
process.stdout.write(JSON.stringify({ outputPath, rowCount: totalRows, sheets }) + "\n");
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将 HANA 故障视图的全部记录导出为 Excel")
    parser.add_argument(
        "--output",
        type=Path,
        help="输出 .xlsx 路径；默认保存到 outputs/hana_faults_YYYYMMDD_HHMMSS.xlsx",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="每次从 HANA 读取的行数")
    parser.add_argument("--limit", type=int, help="仅导出前 N 条记录，用于连通性验证")
    parser.add_argument("--node", help="可运行 @oai/artifact-tool 的 Node.js 可执行文件")
    parser.add_argument("--node-modules", type=Path, help="包含 @oai/artifact-tool 的 node_modules 目录")
    parser.add_argument(
        "--node-memory-mb",
        type=int,
        default=8192,
        help="生成大型工作簿时 Node.js 可使用的最大堆内存（默认 8192）",
    )
    return parser.parse_args()


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def stage_hana_records(staging_path: Path, batch_size: int, limit: int | None) -> int:
    selected = ", ".join(f'"{column}"' for column in REPAIR_COLUMNS)
    order_by = ", ".join(f'"{column}"' for column in ("PCODE", "ZWXDT", "ZMCOD1", "ZDATE_WX", "ZTIME"))
    sql = f"SELECT {selected} FROM {REPAIR_VIEW} ORDER BY {order_by}"
    row_count = 0

    with staging_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"columns": list(REPAIR_COLUMNS)}, ensure_ascii=False) + "\n")
        with hana_connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                while rows := cursor.fetchmany(batch_size):
                    for row in rows:
                        handle.write(json.dumps([as_text(value) for value in row], ensure_ascii=False) + "\n")
                        row_count += 1
                        if limit is not None and row_count >= limit:
                            return row_count
            finally:
                cursor.close()
    return row_count


def find_node(explicit_node: str | None) -> str:
    if explicit_node:
        return explicit_node
    if node := shutil.which("node"):
        return node
    raise RuntimeError("未找到 Node.js；请通过 --node 指定可执行文件")


def export_workbook(
    staging_path: Path,
    output_path: Path,
    node: str,
    node_modules: Path | None,
    node_memory_mb: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="hana_fault_excel_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        builder_path = temp_dir / "build_workbook.mjs"
        builder_path.write_text(NODE_BUILDER, encoding="utf-8")
        if node_modules:
            (temp_dir / "node_modules").symlink_to(node_modules.resolve(), target_is_directory=True)
        environment = os.environ.copy()
        existing_node_options = environment.get("NODE_OPTIONS", "").strip()
        environment["NODE_OPTIONS"] = f"{existing_node_options} --max-old-space-size={node_memory_mb}".strip()
        completed = subprocess.run(
            [node, str(builder_path), str(staging_path), str(output_path)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    stdout = completed.stdout.lstrip("\ufeff").strip()
    if not stdout:
        raise RuntimeError(f"工作簿写入器未返回结果: {completed.stderr.strip() or '无标准错误输出'}")
    try:
        return json.loads(stdout.rsplit("\n", maxsplit=1)[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"工作簿写入器返回了无效结果: {stdout[:500]!r}") from exc


def main() -> int:
    args = parse_args()
    if args.batch_size < 1 or (args.limit is not None and args.limit < 1) or args.node_memory_mb < 512:
        raise SystemExit("--batch-size、--limit 必须大于 0，--node-memory-mb 不得小于 512")
    load_dotenv()
    output_path = (args.output or DEFAULT_OUTPUT_DIR / f"hana_faults_{datetime.now():%Y%m%d_%H%M%S}.xlsx").resolve()
    if output_path.suffix.lower() != ".xlsx":
        raise SystemExit("--output 必须是 .xlsx 文件")
    node_modules = args.node_modules or (Path(os.environ["ARTIFACT_NODE_MODULES"]) if os.environ.get("ARTIFACT_NODE_MODULES") else None)
    if node_modules and not node_modules.is_dir():
        raise SystemExit(f"找不到 node_modules 目录: {node_modules}")

    with tempfile.TemporaryDirectory(prefix="hana_fault_export_") as temp_dir_name:
        staging_path = Path(temp_dir_name) / "faults.jsonl"
        source_rows = stage_hana_records(staging_path, args.batch_size, args.limit)
        result = export_workbook(
            staging_path,
            output_path,
            find_node(args.node),
            node_modules,
            args.node_memory_mb,
        )

    print(json.dumps({"success": True, "sourceRows": source_rows, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        raise SystemExit(exc.returncode) from exc
    except Exception as exc:
        print(f"导出失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
