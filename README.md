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

## 启动后端

```bash
cd backend
go mod tidy
go run ./cmd/server
```

服务会自动读取 `../.env`（也支持 `ENV_FILE=/path/to/.env`）。连接优先使用 `MONGODB_URI`；未设置时根据 `MONGODB_HOSTS`、`MONGODB_USERNAME`、`MONGODB_PASSWORD`、`MONGODB_AUTH_SOURCE`、`MONGODB_REPLICA_SET` 拼接集群连接。默认查询 `REPAIR_COLLECTION`（`repair_records_sap`），也可用 `MONGODB_COLLECTION` 覆盖。

接口：

- `GET /api/health`
- `GET /api/faults?page=1&pageSize=20&keyword=&hostBarcode=&ngStation=&defectResponsibility=&salesOrder=&productionOrder=`
- `GET /api/faults/detail?id=<维修记录_source_key>`
- `GET /api/faults/stats`（接受相同筛选参数；统计维修记录总数、错误信息和维修人员登记情况）
- `GET /api/orders?page=1&pageSize=20&keyword=&source=SG|KK&gstrsFrom=YYYY-MM-DD&gstrsTo=YYYY-MM-DD`
- `GET /api/orders/detail?id=<source:AUFNR>`
- `GET /api/orders/stats`（接受相同筛选参数；按 `data.GSTRS` 过滤并统计生产订单数、去重销售订单数、来源、订单数量和入库数量）
- `POST /api/sync/incremental`（启动销售订单和维修数据增量同步；同一时间仅允许一个任务）
- `GET /api/sync/status`（查询增量同步状态、起止时间和脚本摘要）

完整的看板 API 参数、响应字段、筛选规则和调用示例见
[`docs/看板后端API说明.md`](docs/看板后端API说明.md)。

## 启动前端

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 <http://localhost:5173>。前端只展示网关返回的 MongoDB 数据；MongoDB 未连接时显示连接错误和空结果。

开发环境中，访问后端根地址也会自动跳转到前端；若后端使用 `18080` 端口，请打开 <http://127.0.0.1:5173> 或 <http://127.0.0.1:18080>。

## SAP 数据同步

同步脚本按数据源划分为销售订单/维修入口 `sync_sales_orders.py` 和三个 HANA 视图入口；
脚本独立连接 SAP HTTP/HANA 和目标 MongoDB，不依赖参考项目源码；目标库和账号密码全部从
当前目录 `.env` 读取。

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
| 日期范围 | 强制只同步 `ZDATE >= 20260101` 的数据，结束日期为任务当天或 `--end-date` |
| 销售订单准入 | 只有维修行的 `VBELN` 精确存在于 `sales_orders_sap.data.VBELN` 才写入；空值或未匹配值全部过滤 |
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

### 3. 运行方式

#### 清理销售订单看板中不存在的维修记录

`clean_orphan_repair_records.py` 以 `TARGET_COLLECTION.data.VBELN` 为销售订单白名单，扫描
`REPAIR_COLLECTION.VBELN`，将空值或未匹配的维修记录列为清理对象。默认限定
`_source_view=ZSGV_ZZT_WLJL`，并且只预览、不删除：

```bash
python clean_orphan_repair_records.py --dry-run
```

确认统计结果后才可永久删除，命令必须包含精确确认字符串：

```bash
python clean_orphan_repair_records.py --apply --confirm DELETE-ORPHAN-REPAIRS
```

支持 `--batch-size`、`--limit`、`--from-date YYYY-MM-DD`、`--to-date YYYY-MM-DD`；
`--all-source-views` 可显式包含没有 `_source_view` 的历史记录。每次运行（包括预览）会将
统计、过滤条件、执行状态和时间写入 `CLEANUP_RUN_COLLECTION`（默认 `cleanup_runs`）。
其中 `empty_sales_order` 单独统计空订单号，`unmatched_sales_order` 统计非空但未知的订单号，
`orphan_records` 为两者之和，也是 `--limit` 的计数口径。
清理器同时持有清理专用锁和 `SYNC_LOCK_PATH`，不会与同步任务并发运行。销售订单集合为空、
确认字符串错误或日期范围无效时会拒绝执行。

### 3. 其他 HANA 视图同步脚本

`docs` 目录中的另外三个 HANA 视图分别使用独立入口脚本：

| 脚本 | 视图 | 目标集合 | 增量依据 |
|---|---|---|---|
| `sync_zsgv_zsd124.py` | `ZSGV_ZSD124` | `order_bom_postings_sap` | `BUDAT_MKPF` |
| `sync_zsgv_zpp_sernolist.py` | `ZSGV_ZPP_SERNOLIST` | `serial_bindings_sap` | 无日期字段，每次全量扫描后幂等更新 |
| `sync_z_v_zmes_t_001.py` | `Z_V_ZMES_T_001` | `station_records_sap` | `ACTUAL_START_TIME` |

三个脚本共享根目录的 `hana_view_sync.py`。每次脚本运行只建立一次 MongoDB 连接和一次
HANA session，使用单个 cursor 分批读取和 `bulk_write` 批量写入；脚本结束时在 `finally`
中关闭 cursor、HANA session 和 MongoClient。同步过程不会按记录或批次重复连接数据库。

MongoDB 写确认策略可通过以下环境变量配置：`MONGODB_WRITE_CONCERN_W`（正整数或
`majority`）、`MONGODB_JOURNALED`（`true`/`false`）与 `MONGODB_WRITE_TIMEOUT_MS`。
默认未设置时沿用 MongoDB 服务端策略。当前生产同步使用 `w=1`、不要求 journal 确认及
30 秒超时，避免副本集 secondary 不可用时 `majority` 写确认超时；主节点写入失败仍会使
任务失败且不会推进水位线。

单独执行示例：

```bash
python sync_zsgv_zsd124.py --mode incremental
python sync_zsgv_zpp_sernolist.py --mode incremental
python sync_z_v_zmes_t_001.py --mode incremental
```

首次或指定范围重建时使用 `--mode full --start-date 2026-01-01`。所有脚本支持
`--dry-run`、`--batch-size` 和 `--lookback-days`；水位线写入 `sync_checkpoints`，运行审计
写入 `sync_runs`，视图水位线 ID 为 `hana_views:<VIEW_ID>`。

前端顶栏提供两个数据操作：

- **刷新**：只重新读取 MongoDB，两个看板通过同一个刷新状态同时更新，不访问 SAP。
- **立即同步**：调用 `POST /api/sync/incremental`，后端异步顺序执行
  `sync_sales_orders.py --dataset all`、`sync_zsgv_zsd124.py`、
  `sync_zsgv_zpp_sernolist.py` 和 `sync_z_v_zmes_t_001.py`。任务完成后前端自动刷新维修故障
  和销售订单两个看板，同步期间按钮显示“同步中”，重复点击会收到 HTTP `409`。

前端同时提供三个 HANA 视图看板：**订单过账**对应 `ZSGV_ZSD124`、**序列号绑定**对应
`ZSGV_ZPP_SERNOLIST`、**工位记录**对应 `Z_V_ZMES_T_001`。每个看板从对应 MongoDB
集合读取数据，支持关键字搜索、分页、记录详情和（有日期字段的视图）起止日期筛选；
页面显示数据来源、数据时间区间和最新同步时间。后端接口分别为
`GET /api/views/{viewID}`、`GET /api/views/{viewID}/stats` 与
`GET /api/views/{viewID}/detail?id=...`，只允许 docs 中登记的三个视图 ID，刷新只访问
MongoDB，不会重新访问 HANA。

后端进程必须使用已安装 `httpx`、`pymongo`、`hdbcli` 的 Python 环境执行脚本。默认调用
`python`，可在 `.env` 中覆盖：

```bash
SYNC_PYTHON=/Users/libiao/miniconda3/bin/python
SYNC_SCRIPT_PATH=/absolute/path/to/sync_sales_orders.py
SYNC_VIEW_SCRIPT_DIR=/absolute/path/to/project/root
```

网页按钮和定时任务应按顺序执行根目录的四个生产入口：`sync_sales_orders.py --dataset all`、
`sync_zsgv_zsd124.py`、`sync_zsgv_zpp_sernolist.py`、`sync_z_v_zmes_t_001.py`。
销售订单按 SG/KK 来源水位线增量拉取，维修数据和三个 HANA 视图按各自水位线增量拉取。
`SYNC_LOCK_PATH` 防止命令行、定时任务和网页按钮并发执行。同步输出会保存在
`GET /api/sync/status` 的 `summary` 中；任一来源失败时任务标记失败，该来源水位线不会推进。

首次全量同步（销售订单和维修数据）：

```bash
/Users/libiao/Desktop/github/QualityMonitoringSystem/backend/.venv/bin/python \
  sync_sales_orders.py --dataset all --full --start-date 2026-01-01 --all-prodh
```

日常增量同步：

```bash
python sync_sales_orders.py --dataset all
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
