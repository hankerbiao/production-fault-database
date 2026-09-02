# 产线故障数据库

MongoDB 数据库网关 + Vite/React 查询工作台。后端直接读取项目根目录 `.env`，MongoDB 是唯一数据源。

## 初始化开发环境

首次部署或更换机器时，在项目根目录执行：

```bash
./scripts/prepare_env.sh
```

脚本会幂等地创建 `.venv`、安装 Python 依赖、下载 Go 模块、执行前端 `npm ci`，并在根目录不存在 `.env` 时从 `backend/.env.example` 生成初始配置。已有 `.env` 不会被覆盖；生成后请按实际 MongoDB 和 SAP 环境补充凭据。

如果当前机器只运行前后端查询、不执行 HANA 同步，可跳过 SAP HANA 驱动：

```bash
./scripts/prepare_env.sh --skip-hdbcli
```

检查环境而不进行安装：

```bash
./scripts/prepare_env.sh --check-only --skip-hdbcli
```

更多选项见 `./scripts/prepare_env.sh --help`。

脚本运行环境也可直接按 `requirements.txt` 安装。当前机器已创建 `.venv` 并安装
`pymongo`、`httpx`、`hdbcli`、`python-dotenv` 和测试依赖；后续执行同步或清理时使用：

```bash
.venv/bin/python repair_records.py --mode incremental --dry-run
.venv/bin/python station_records.py --mode incremental --dry-run
```

## 启动后端

```bash
cd backend
go mod tidy
go run ./cmd/server
```

服务会自动读取 `../.env`（也支持 `ENV_FILE=/path/to/.env`）。连接优先使用 `MONGODB_URI`；未设置时根据 `MONGODB_HOSTS`、`MONGODB_USERNAME`、`MONGODB_PASSWORD`、`MONGODB_AUTH_SOURCE`、`MONGODB_REPLICA_SET` 拼接集群连接。默认查询 `REPAIR_COLLECTION`（`repair_records_sap`），也可用 `MONGODB_COLLECTION` 覆盖。

接口：

- `GET /api/health`
- `GET /api/faults?page=1&pageSize=20&keyword=&productionOrder=&salesOrder=&productModel=&dateFrom=&dateTo=`（其余条件见高级筛选参数）
- `GET /api/faults/detail?id=<维修记录_source_key>`
- `GET /api/faults/stats`（接受相同筛选参数；统计维修记录总数、错误信息和维修人员登记情况）
- `GET /api/orders?page=1&pageSize=20&productionOrder=&salesOrder=&productModel=&dateFrom=&dateTo=&source=SG|KK`
- `GET /api/orders/all?productionOrder=&salesOrder=&productModel=&dateFrom=&dateTo=&source=SG|KK`（按筛选条件查询全部订单，单次最多 10,000 条；也可给 `/api/orders` 追加 `all=true`）
- `GET /api/orders/detail?id=<source:AUFNR>`
- `GET /api/orders/stats`（接受相同筛选参数；按 `data.GSTRS` 过滤并统计生产订单数、去重销售订单数、来源、订单数量、机器数量汇总（`machineQuantity`，取 `GAMNG` 汇总）和入库数量）
- `GET /api/orders/models?keyword=`：返回销售订单数据中去重后的 `MAKTX_TH` 生产机型列表；`keyword` 可选，用于候选搜索。
- `POST /api/sync/incremental`（启动销售订单和维修数据增量同步；同一时间仅允许一个任务）
- `GET /api/sync/status`（查询增量同步状态、起止时间和脚本摘要）

完整的看板 API 参数、响应字段、筛选规则和调用示例见
[`docs/看板后端API说明.md`](docs/看板后端API说明.md)。
机器可读的 OpenAPI 3.1 定义见 [`docs/openapi.json`](docs/openapi.json)，可导入 Swagger UI、Postman 或代码生成器。

维修故障、销售订单、订单过账、序列号绑定和工位记录看板均支持导出当前筛选结果为 UTF-8 CSV 文件；销售订单导出查询全部筛选结果，单次最多 10,000 条，不受当前看板分页限制。

## 启动前端

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 <http://localhost:5173>。前端只展示网关返回的 MongoDB 数据；MongoDB 未连接时显示连接错误和空结果。

开发环境中，访问后端根地址也会自动跳转到前端；若后端使用 `18080` 端口，请打开 <http://127.0.0.1:5173> 或 <http://127.0.0.1:18080>。

## SAP 数据同步

### 四表同步与清洗

系统只保留四个可执行脚本：`repair_records.py`、`station_records.py`、
`order_bom_postings.py` 和 `serial_bindings.py`。它们均支持
`--mode full|incremental`、`--start-date`、`--end-date`、`--lookback-days`、
`--batch-size`、`--dry-run`、`--apply`。默认只预览；传入 `--apply` 后，同步完成会自动执行
字段补全和清理，无需额外确认字符串。

维修故障明细会先刷新销售订单参考数据，再扫描 `repair_records_sap` 中 `_source_view=ZSGV_ZZT_WLJL` 的记录。对缺失 `AUFNR`
或 `VBELN` 的记录，脚本首先以共同的主机序列号 `PCODE` 查询 `station_records_sap`
（`_source_view=Z_V_ZMES_T_001`）：同一 SN 的 `AUFNR` 或 `KDAUF` 只有唯一值时，分别回填维修
记录的 `AUFNR` 或 `VBELN`；存在多个不同值时不自动写入。仅当工位表不能提供生产订单时，才使用
`PCODE` 查询 SAP，先查 KK 生产、再查 SG 生产；之后按来源优先级查询销售订单看板
`data.AUFNR/data.VBELN` 补充仍缺失的销售订单。两边都查不到的记录保留；已得到生产订单但销售
订单看板不存在的记录列为非 5000 订单，可在 apply 模式删除；看板中存在生产订单但销售订单仍为空
的记录保留并跳过。对于已确定生产订单且 `GSTRS` 为空的维修记录，脚本从销售订单表回填计划开始
时间；同一生产订单存在多个计划开始时间时不写入，并计入冲突统计。生产订单和销售订单均非空但生产
订单不在看板中的记录也会列为删除候选。汇总中的 `filled_production_from_station`、
`filled_sales_from_station`、`planned_start_candidates`、`skipped_ambiguous_planned_start` 和
`station_ambiguous_*_sns` 用于核对回填与冲突情况。

维修明细默认只预览，不写入或删除：

```bash
python repair_records.py --mode incremental --dry-run
```

确认统计无误后执行同步、回填和删除：

```bash
python repair_records.py --mode incremental --apply
```

全量同步必须传入 `--start-date`。全量数据先按本次 `run_id` 写入，清洗成功后才删除未出现在本次
运行中的旧数据并提交水位线；同步或清洗失败时不会删除旧数据，也不会推进水位线。增量清洗只处理
本次同步写入的数据。每个命令输出含 `sync`、`cleanup`、`run_id` 和执行统计的 JSON 报告。

### 1. 销售订单表 `sales_orders_sap`

| 项目 | 说明 |
|---|---|
| 数据来源 | SAP HTTP 接口 `ZSIMS_CL_INBOUND_MO_PSINFO`，分别访问 SG（client 800）和 KK（client 600） |
| 请求参数 | `CHDAT`、`CHDAT_TO`；默认带 `PRODH_LIST=00100`，可用 `--all-prodh` 取消 |
| 目标集合 | `.env` 的 `TARGET_COLLECTION`，默认 `sales_orders_sap` |
| 业务唯一键 | `source + AUFNR`；MongoDB `_id` 为 `{source}:{AUFNR}`，SG/KK 同号不会互相覆盖 |
| 增量水位 | `sync_checkpoints` 中的 `sales_orders:SG`、`sales_orders:KK`，按接口查询日期推进 |
| 增量窗口 | 水位日期向前回看 `SYNC_LOOKBACK_DAYS` 天（默认 7 天）到当天，覆盖迟到或修改记录 |
| 全量窗口 | `--full --start-date YYYY-MM-DD`；按 `FULL_WINDOW_DAYS`（默认 7 天）分段请求，避免大响应占满内存；所有来源成功后清理 SG/KK 范围外旧订单，失败时保留旧数据 |

订单文档字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `_id` | string | `{source}:{AUFNR}`，稳定主键 |
| `source` | string | SAP 来源：`SG` 或 `KK` |
| `aufnr` | string | 生产订单号（接口 `AUFNR`） |
| `source_aufnr` | string | 来源和生产订单的组合键 |
| `data` | object | 接口返回的最新一条订单记录，保留全部源字段，如 `VBELN`、`KID`、`NAME1_ZU`、`MAKTX_TH`、`LGORT`、`GSTRS`、`ZDATE_STARTED`、`GAMNG`、`WMENG` |
| `records` | array | 同一来源、同一 `AUFNR` 返回的全部明细行 |
| `record_count` | integer | `records` 的行数 |
| `order_quantity` | number | 明细 `GAMNG`（订单数量）求和 |
| `storage_quantity` | number | 明细 `WMENG`（入库数量）求和 |
| `gstrs_date` | string/null | `data.GSTRS` 的日期部分 |
| `first_seen_at` | datetime | 首次写入时间，仅插入时设置 |
| `last_synced_at` | datetime | 最近一次同步时间 |
| `sync_count` | integer | 成功 upsert 次数 |
| `_scope_run_id` | string | 最近一次成功全量同步批次；用于清理全量范围外旧订单 |

### 2. 维修数据表 `repair_records_sap`

| 项目 | 说明 |
|---|---|
| 数据来源 | SAP HANA 视图 `"_SYS_BIC"."BW_LOCAL.PP/ZSGV_ZZT_WLJL"` |
| 目标集合 | `.env` 的 `REPAIR_COLLECTION`，默认 `repair_records_sap` |
| 日期范围 | 代码固定起始日期为 `2026-01-01`，强制只同步 `ZDATE >= 20260101`；结束日期默认为任务当天或 `--end-date`。首次增量同步无水位线时自动同步 2026-01-01 至当天，后续增量按维修水位线继续同步 |
| 销售订单准入 | 同步时非空维修行的 `VBELN` 必须精确存在于 `sales_orders_sap.data.VBELN`；空 `VBELN` 保留，非空未匹配值过滤 |
| 业务唯一键 | `MANDT + PCODE + ZMCOD1 + ZDATE_WX + ZTIME`；缺少键字段时使用整行哈希键 |
| 增量水位 | `sync_checkpoints` 的 `_id=repair_records`，保存最大 `ZDATE + ZTIME` |
| 写入方式 | HANA 游标 `fetchmany` 分批读取，MongoDB `bulk_write(..., upsert=True)` 幂等写入 |
| 全量清理 | 全量成功后删除本次范围外、未被当前批次标记的旧维修记录；失败时不清理、不推进水位 |

维修原始字段来自 HANA 视图，脚本当前同步以下字段：

```text
MANDT PCODE ZWXDT ZMCOD1 ZRCOD1 MATNR ZJXMC ZNGGZ ZNGWD ZWXWD ZBJ ZZRFL ZCCLH
ZGZMS MAKTX ZMCOD2 ZRCOD2 ZDATE ZTIME ZUSER ZSOURCE ZDATE_WX REJUDGE RET RPDESC
RNOTE SECFLG FACTORY ZWXWD1 ZWXWD2 ZWXWD3 U_FIX FIX_REMARKS TESTID ZNGSPEC T_FIND
ZNGWD1 ZNGWD2 ZNGWD3 ERROR_CODE ERROR_MSG RETEST_STATION TEST_LOG_NAME SECOND_PART_NO
RECORD01REPAIRM SLOT AUFNR VBELN POSNR U_FIND U_RMA_NAME RMA_RESULT RMA_TYPE2
```

维修文档还包含以下同步审计字段：`_source_key`（稳定业务键）、`_source_view`（源
视图名）、`_scope_run_id`（最近一次成功全量批次）、`_sync_run_id` 和 `_synced_at`。

### 3. 四表执行方式

| 脚本 | 目标集合 | 同步后自动处理 |
|---|---|---|
| `repair_records.py` | `repair_records_sap` | 销售订单预同步、工位优先回填、SAP 兜底、销售订单和 `GSTRS` 补全、异常记录清理 |
| `station_records.py` | `station_records_sap` | 销售订单/生产订单字段补全和无效销售订单清理 |
| `order_bom_postings.py` | `order_bom_postings_sap` | 仅保留 `CPX=5000公司` |
| `serial_bindings.py` | `serial_bindings_sap` | 仅删除完整业务键的精确重复记录，保留并统计不完整键 |

日常增量预览和执行：

```bash
python repair_records.py --sales-only --mode incremental --apply
python station_records.py --mode incremental --apply
python repair_records.py --mode incremental --apply
python order_bom_postings.py --mode incremental --apply
python serial_bindings.py --mode incremental --apply
```

全量重建时，将上述命令的 `--mode incremental` 换为
`--mode full --start-date 2026-01-01`。后端“立即同步”按相同顺序自动执行；所有阶段使用文件锁和
MongoDB 租约锁防止并发运行。`SYNC_PYTHON` 指定 Python 解释器，`SYNC_SCRIPT_DIR` 指向包含这四个
脚本的项目根目录。

前端顶栏提供两个数据操作：

- **刷新**：只重新读取 MongoDB，两个看板通过同一个刷新状态同时更新，不访问 SAP。
- **立即同步**：调用 `POST /api/sync/incremental`，后端按销售订单、工位、维修、BOM、序列号绑定的
  顺序同步并清洗；任一阶段失败会停止后续步骤。任务完成后前端自动刷新维修故障
  和销售订单两个看板，同步期间按钮显示“同步中”，重复点击会收到 HTTP `409`。

前端同时提供三个 HANA 视图看板：**订单过账**对应 `ZSGV_ZSD124`、**序列号绑定**对应
`ZSGV_ZPP_SERNOLIST`、**工位记录**对应 `Z_V_ZMES_T_001`。每个看板从对应 MongoDB
集合读取数据，支持关键字搜索、分页、记录详情和（有日期字段的视图）起止日期筛选；
页面显示数据来源、数据时间区间和最新同步时间。后端接口分别为
`GET /api/views/{viewID}`、`GET /api/views/{viewID}/stats` 与
`GET /api/views/{viewID}/detail?id=...`，只允许 docs 中登记的三个视图 ID，刷新只访问
MongoDB，不会重新访问 HANA。

序列号绑定看板的表头、筛选提示、统计、分页、详情和操作按钮均采用中文/英文对照；字段标签遵循
`ZSGV_ZPP_SERNOLIST` 文档定义，例如“大刀/机头序列号（ZCODE_HEAD）”。

后端进程必须使用已安装 `httpx`、`pymongo`、`hdbcli` 的 Python 环境执行脚本。默认调用
`python`，可在 `.env` 中覆盖：

```bash
SYNC_PYTHON=/Users/libiao/miniconda3/bin/python
SYNC_SCRIPT_DIR=/absolute/path/to/产线故障数据库
```

网页按钮和定时任务应按上述五个阶段执行；销售订单作为维修和工位清洗的内部参考数据，由
`repair_records.py --sales-only` 预同步。
`SYNC_LOCK_PATH` 防止命令行、定时任务和网页按钮并发执行。同步输出会保存在
`GET /api/sync/status` 的 `summary` 中；任一来源失败时任务标记失败，该来源水位线不会推进。

首次全量同步：

```bash
python repair_records.py --sales-only --mode full --start-date 2026-01-01 --apply
python station_records.py --mode full --start-date 2026-01-01 --apply
python repair_records.py --mode full --start-date 2026-01-01 --apply
python order_bom_postings.py --mode full --start-date 2026-01-01 --apply
python serial_bindings.py --mode full --start-date 2026-01-01 --apply
```

日常增量同步：

```bash
python repair_records.py --sales-only --mode incremental --apply
```

建议 crontab 每日执行增量任务。`--dry-run` 只请求和统计，不写入订单、维修记录或
水位线；同一时间只允许一个进程运行。除本机锁文件 `SYNC_LOCK_PATH` 外，脚本还使用
MongoDB 的 `sync_locks` 租约锁跨主机协调；租约时长由 `SYNC_MONGO_LOCK_TTL_SECONDS`
配置，必须大于一次任务的最长运行时间。

清理孤立维修记录时，`empty_sales_order` 仅统计空 `VBELN`，
`unmatched_sales_order` 仅统计非空但不在销售订单表中的值；删除前会重新加载销售订单
白名单，并同时匹配维修记录的 `_id` 与原始 `VBELN`，避免并发修改导致误删。

### 4. 表结构变更同步要求

- 销售订单接口新增字段会自动保存在 `data` 和 `records` 中，无需改 MongoDB schema；若新增字段参与唯一键、数量或筛选逻辑，必须同步修改 `sync_sales_orders.py` 并补充 README。
- 维修 HANA 视图新增字段时，必须将字段加入脚本的 `REPAIR_COLUMNS`，同时更新本节字段清单；否则脚本不会读取该列。
- 新增同步表时，沿用同一脚本的 `--dataset` 分支、检查点、运行摘要、批量 upsert 和索引初始化模式，并在 README 增加来源、主键、日期范围、准入规则和水位线说明。
- 源字段重命名、业务键变化或目标集合改名属于迁移操作：先备份目标集合，再更新脚本和索引，最后用 `--full --start-date` 重建，禁止直接覆盖旧集合造成混合数据。
- 每次表或字段变更后先运行 `--dry-run`，确认返回行数、过滤数、唯一键和水位线，再执行正式同步。
