# 产线故障数据库 Agent 任务编排指南

本指南面向调用本项目 API 的 AI Agent、自动化脚本和运维工具。它说明如何安全地查询产线数据、判断数据新鲜度，以及如何在获得人工确认后发起、观察和恢复同步任务。

默认网关地址：`http://127.0.0.1:18080`。机器可读契约：`GET /api/openapi.json`。

## 1. 边界与首要规则

1. 查询接口可由 Agent 直接调用；任何 `POST /api/sync/*` 都会改变本地数据状态，必须先获得明确人工确认。
2. 不要把空数组当作“没有数据”。先检查 HTTP 状态码；错误响应统一是 `{ "error": "..." }`。
3. 不要直接调用单个同步脚本来替代日常任务。日常同步必须通过编排器，确保依赖、互斥锁、重试、阶段记录和水位线处理一致。
4. 列表结果用于筛选和定位，详情结果用于读取完整字段。不要假定 `raw` 源文档字段固定。
5. 默认只处理业务键已经确定的记录。跨域关联优先使用生产订单、销售订单、主机/部件序列号等稳定键，不能仅用描述性文本做唯一匹配。

## 2. 开始前检查

在回答“数据是否最新”“能否开始同步”或执行复杂查询前，按顺序读取以下接口：

```text
GET /api/health
GET /api/data-status
GET /api/sync/status
```

| 检查项 | 正常信号 | 异常时的处理 |
| --- | --- | --- |
| 网关和 MongoDB | `/api/health` 返回 `200` 和 `{"status":"ok"}` | 停止后续判断，报告网关或数据库不可用 |
| 数据新鲜度 | `/api/data-status` 中对应集合的 `LastSyncedAt` 符合任务需求 | 明确指出数据截至时间；如需更新，先请求人工确认 |
| 编排互斥 | `/api/sync/status.state` 不是 `running` | 不启动第二个任务，改为跟踪已有 `runId` |

`/api/data-status` 同时返回 SCS DOA、SCS 换上换下的同步时间与记录数。仅比较同一数据域的时间，例如查询 SCS 换上换下时看 `scsChangeLastSyncedAt`，不要用维修故障的时间代替。

## 3. 查询契约与分页策略

- 所有列表查询返回 JSON，`page` 从 1 开始，`pageSize` 默认 20、最大 100。
- 日期优先使用 `YYYY-MM-DD`。SAP 源字段可能显示为 `YYYYMMDD`，但请求参数仍使用连字符日期。
- 字符串会去除首尾空格；关键字是安全的大小写不敏感匹配，不应把它当作正则表达式。
- 列表的 `items[].id` 是详情查询唯一可依赖的标识：`/api/faults/detail?id=...`、`/api/orders/detail?id=...`、`/api/views/{viewID}/detail?id=...`。
- `/all` 或 `all=true` 单次最多返回 10,000 条。预计更多时，按日期窗口或业务键分段，不要在失败后无限增大 `pageSize`。
- 查询完成后应返回实际筛选条件、`total`、数据时间范围和是否存在截断；对需要人工判断的关联，保留无法匹配的记录数量。

### 推荐查询流程

1. 识别用户给出的稳定键：优先生产订单，其次销售订单、主机 SN、部件 SN、服务单号。
2. 选择最小数据域的列表接口，带上时间范围和分页参数。
3. 从结果中选取 `id` 调用详情接口，不要先拉取整个集合。
4. 需要跨域关联时，以列表中实际返回的键作为下一次请求输入，并显式说明未命中的记录。
5. 大批量结果按分页或时间窗口收集；任一页失败时保留已完成页并报告失败页，而不是把部分结果描述成全量结果。

## 4. 常用数据查询

### 维修故障与订单追溯

```text
GET /api/faults?productionOrder=PO001&dateFrom=2026-01-01&dateTo=2026-01-31&page=1&pageSize=100
GET /api/faults/by-orders?productionOrders=PO001,PO002&page=1&pageSize=100
GET /api/faults/by-sns?sns=SN001,SN002&page=1&pageSize=100
GET /api/orders?productionOrder=PO001&source=SG&page=1&pageSize=20
GET /api/orders/detail?id=SG:PO001
```

维修故障列表允许叠加 `dateFrom`、`dateTo`、`station` 和 `timeField`。按 SN 的批量 POST 接口单次不应超过 10,000 个 SN。故障列表逐条返回，不按序列号去重；需要去重时应在结果层明确使用的键和规则。

### 工位、BOM 与序列号绑定

```text
GET /api/views/Z_V_ZMES_T_001?sn=SN001&dateFrom=2026-01-01&dateTo=2026-01-31
GET /api/views/ZSGV_ZSD124?productionOrder=PO001&missingSalesOrder=true
GET /api/views/ZSGV_ZPP_SERNOLIST?headSn=HEAD001
GET /api/views/ZSGV_ZPP_SERNOLIST?itemSn=ITEM001
```

`Z_V_ZMES_T_001` 用于工位过程记录，`ZSGV_ZSD124` 用于 BOM 过账，`ZSGV_ZPP_SERNOLIST` 用于机头/部件序列号映射。BOM、工位和 SCS 视图支持 `GET /api/views/{viewID}/stream` 的 TSV 流式输出，适用于服务端批量计算，不适用于浏览器端无边界加载。

### SCS 数据

```text
GET /api/views/SCS_DOA?serviceOrder=SH001&company5000=true&page=1&pageSize=20
GET /api/views/SCS_DOA?dateFrom=2026-01-01&dateTo=2026-01-31&salesOrder=XHG2609190
GET /api/views/SCS_CHANGE?changeType=换上-up&partNumber=33000306&page=1&pageSize=20
GET /api/views/SCS_CHANGE?serviceOrder=SH001&company5000=true
```

SCS DOA 支持 `doaCode`、`doaType`、`status`、`customer`、`serviceOrder`、`sn`、`salesOrder`、`company5000`；时间字段是 `declare_time`。SCS 换上换下支持 `serviceOrder`、`customer`、`sn`、`changeType`、`partNumber`、`partSn`、`operator`、`needReturn`、`revoked`、`company5000`；时间字段是 `create_time`。

## 5. 同步任务目录与依赖

编排器按以下稳定顺序执行，并自动补齐所选任务的依赖：

| 任务 ID | 数据域 | 直接依赖 | 典型用途 |
| --- | --- | --- | --- |
| `sales_orders` | 销售订单 | 无 | 更新订单、客户、机型等参考数据 |
| `station_records` | 工位记录 | `sales_orders` | 同步工位过程并补齐订单关联 |
| `repair_records` | 维修故障 | `sales_orders`, `station_records` | 同步维修记录并执行关联清洗 |
| `order_bom_postings` | BOM 过账 | `sales_orders` | 同步订单 BOM 过账记录 |
| `serial_bindings` | 序列号绑定 | 无 | 同步机头与部件 SN 映射 |
| `scs_doa` | SCS DOA | `sales_orders` | 同步 SCS DOA 申报及订单关联 |
| `scs_change` | SCS 换上换下 | `scs_doa` | 同步换上换下记录及 5000 公司标识 |

例如，请求 `scs_change` 会实际按 `sales_orders -> scs_doa -> scs_change` 执行。请求 `repair_records` 会先执行 `sales_orders -> station_records -> repair_records`。Agent 应把 API 返回的 `resolved_task_ids` 视为实际执行范围，而不是只复述提交时的 `taskIds`。

## 6. 增量与全量的选择

| 模式 | 适用场景 | 必填字段 | 风险与限制 |
| --- | --- | --- | --- |
| `incremental` | 日常刷新、补抓近期修改 | `mode`，可选 `taskIds` | 从检查点/水位线回看一段时间；适合重复执行 |
| `full` | 首次建库、明确范围的历史重建、修复错误范围 | `mode`、`startDate`，建议同时给出 `taskIds` 和 `endDate` | 成功后部分任务会清理所选范围外的旧数据，必须二次人工确认 |

全量模式的 `startDate` 必须是 `YYYY-MM-DD`；`endDate` 可选，未传时由编排器使用当天。SCS 换上换下的当前同步范围固定约束在 2026 年，服务端请求为 `2026-01-01` 到 `2026-12-31`，并以每页少于 500 条或空页作为列表结束条件。

## 7. 发起同步的安全流程

### 7.1 人工确认前，Agent 必须说明

- 执行模式、目标任务和自动补齐的依赖。
- 全量同步的开始/结束日期，以及可能触发范围清理的影响。
- 当前数据的最后同步时间、是否已有运行中的任务。
- 预计会访问的外部数据源及任务仍可通过运行记录恢复。

### 7.2 创建运行

获得明确确认后，使用 `POST /api/sync/runs`：

```json
{
  "mode": "incremental",
  "taskIds": ["scs_change"]
}
```

全量示例：

```json
{
  "mode": "full",
  "taskIds": ["repair_records"],
  "startDate": "2026-01-01",
  "endDate": "2026-01-31"
}
```

成功受理返回 `202`，响应包含 `runId` 与 `state: "running"`。兼容入口 `POST /api/sync/incremental` 会启动全部注册任务；仅在人工明确要求“全量日常管道”时使用，不建议 Agent 默认调用。

若返回 `409`，说明已有运行中任务。响应中的 `status` 是当前任务状态；应转为轮询该任务，而不是重试创建。

## 8. 观察、解释与恢复运行

### 运行状态

| 状态 | 含义 | Agent 的下一步 |
| --- | --- | --- |
| `idle` | 当前进程没有内存中的任务，或尚未产生运行记录 | 可读取历史；开始新任务前仍检查 `/api/sync/runs` |
| `running` | 正在执行，心跳持续更新 | 每 2-5 秒查询一次，不重复创建运行 |
| `success` | 全部已解析任务完成 | 刷新对应数据域并报告实际阶段摘要 |
| `failed` | 某阶段失败，后续阶段停止 | 读取运行详情和失败阶段；仅在人工确认后重试 |
| `abandoned` | 编排器心跳超时，被判定为中断 | 检查外部进程是否仍在运行，再按原请求发起重试 |

### 推荐轮询顺序

```text
GET /api/sync/runs/{runId}
GET /api/sync/status
GET /api/data-status             # 仅在任务进入 success 后调用
```

`GET /api/sync/runs/{runId}` 包含 `requested_task_ids`、`resolved_task_ids`、`stages`、`heartbeat_at`、`message`。每个阶段可能是 `pending`、`running`、`retrying`、`success` 或 `failed`，并记录 `attempts`、`summary` 与 `error`。

编排器对可重试错误最多尝试 3 次，退避等待最多 8 秒。连接超时、临时网络错误、`429`、`502`、`503` 等会被标记为可重试；业务校验、无效参数和脚本输出格式错误不会自动无限重试。

### 重试失败任务

`POST /api/sync/runs/{id}/retry` 会使用原运行的模式、任务选择和日期范围创建新的运行。先读取旧运行，确认失败阶段和错误是否属于临时问题；不要对持续的凭据、参数或数据质量错误进行无意义重试。

SCS 换上换下在 MongoDB `sync_checkpoints` 中保存页码、页内偏移、已抓取条数、查询范围与运行 ID；相同运行条件下中断后会从该检查点恢复，并每 100 条批量写入。Agent 在报告恢复状态时应同时给出页码、累计抓取数和已入库数。

## 9. 失败处置决策表

| 现象 | 先查什么 | 处理原则 |
| --- | --- | --- |
| `409 已有同步任务运行中` | `/api/sync/status`、运行详情 | 不新建任务，切换为轮询 |
| `failed` 且 `retryable: true` | 失败阶段的 `error`、`attempts` | 告知人工可重试原因，确认后调用 retry |
| `failed` 且 `retryable: false` | 参数、权限、脚本输出和日志 | 先修正根因，不自动重试 |
| `abandoned` | `heartbeat_at`、任务进程、检查点 | 确认旧进程不再执行后再重试 |
| HTTP `503` | `/api/health` | 数据库或网关不可用，停止自动化动作 |
| 列表为空但状态正常 | 实际筛选参数、`total`、日期范围 | 说明筛选范围内没有结果，不推断全库为空 |

## 10. 对用户的报告格式

完成查询时，报告应包含：数据域、实际筛选条件、返回数量/总数、数据时间范围、未匹配或被截断的数量、来源更新时间。完成同步时，报告应包含：`runId`、模式、请求任务、自动补齐任务、最终状态、每个阶段的尝试次数与关键摘要、失败原因或检查点位置。

避免把“任务已受理”表述为“同步已完成”。只有运行状态为 `success`，且相应 `/api/data-status` 的更新时间已刷新，才能确认数据可用于后续分析。

## 11. 数据域速查

| 数据域 | 主要接口 | 关键字段 |
| --- | --- | --- |
| 维修故障 | `/api/faults*` | `PCODE`, `AUFNR`, `VBELN`, `ERROR_CODE`, `ERROR_MSG`, `ZDATE_WX` |
| 销售订单 | `/api/orders*` | `AUFNR`, `VBELN`, `KID`, `MAKTX_TH`, `GSTRS`, `GAMNG`, `WMENG` |
| BOM 过账 | `/api/views/ZSGV_ZSD124*` | `MATNR`, `AUFNR_1`, `VBELN_EX`, `MENGE_A`, `BUDAT_MKPF` |
| 序列号绑定 | `/api/views/ZSGV_ZPP_SERNOLIST*` | `ZCODE_HEAD`, `ZCODE_ITEM`, `AUFNR_HEAD`, `AUFNR_ITEM`, `PRODH` |
| 工位记录 | `/api/views/Z_V_ZMES_T_001*` | `PCODE`, `OCODE`, `AUFNR`, `SPEC`, `ACTUAL_START_TIME`, `ACTUAL_END_TIME` |
| SCS DOA 申报 | `/api/views/SCS_DOA*` | `doa_code`, `service_uid`, `sales_order`, `sugon_sn`, `is_5000_company` |
| SCS 换上换下 | `/api/views/SCS_CHANGE*` | `so_code`, `device_sn`, `change_type`, `part_number`, `part_sn`, `is_5000_company` |
