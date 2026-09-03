# 看板后端 API 说明

本文档描述产线故障数据库后端网关为看板提供的 HTTP API。接口只查询 MongoDB，不会在查询时访问 SAP 或 HANA；数据由同步任务写入 MongoDB 后供看板读取。

机器可读的 OpenAPI 3.1 契约见 [`docs/openapi.json`](openapi.json)，可直接导入 Swagger UI、Postman、Insomnia 或代码生成器。本 Markdown 版本用于补充业务语义、数据来源和调用约束；两者应保持同步。

## 1. 基本约定

- 默认服务地址：`http://127.0.0.1:18080`
- 服务地址可由部署环境的 `PORT` 覆盖；前端开发代理默认指向同一地址。根路径 `GET /` 会临时重定向到 `FRONTEND_URL`，业务接口统一位于 `/api` 下。
- 返回格式：`application/json; charset=utf-8`
- 请求方法：查询接口使用 `GET`；启动同步使用 `POST`
- 当前版本不提供认证、授权或限流；服务应部署在受信任网络，生产网关需自行增加认证和访问控制。服务端对所有来源开放 CORS（`Access-Control-Allow-Origin: *`）。
- 字符编码：UTF-8
- 列表、统计和状态接口的查询参数均可省略；三个详情接口的 `id` 必填。字符串参数会在后端 `TrimSpace` 后参与过滤
- 分页接口默认 `page=1`、`pageSize=20`；`pageSize` 超过 100 或小于 1 时按 20 处理
- 页码从 1 开始，排序由后端固定，调用方不需要传排序参数
- 关键字采用不区分大小写的正则匹配，后端会转义用户输入，不支持调用方注入正则表达式

## 1.1 纯数据接口扩展与实现约束

以下接口面向 LRR、复合机型、质量分析等上层服务，职责是稳定地返回 MongoDB 中的原始数据，**不**在网关中计算投入数量、故障数量、RTY、过站数量、直通率或复合机型故障结论。

- 分页列表接口最大 `pageSize=100`；缺省或越界时使用 `20`。`/api/orders?all=true` 和 `/api/orders/all` 是例外，复用筛选条件并同步返回最多 `10,000` 条订单。

销售订单支持全量筛选查询：`GET /api/orders/all`，或在既有接口追加 `all=true`。它使用与 `/api/orders` 完全相同的筛选和排序，但忽略看板分页，仅返回前 `10,000` 条匹配记录；超过上限时响应仍只包含前 `10,000` 条，调用方应使用日期分段查询。可通过 `total` 判断是否被截断。
- `id` 是稳定查询键：维修和 HANA 视图优先为 `_source_key`，销售订单为 `{source}:{AUFNR}`。详情接口接受该值并返回完整 MongoDB 文档/源字段。
- 逗号分隔参数（也接受中文逗号）表示批量精确查询。`salesOrder`、`productionOrder` 及对应批量参数支持 SAP 前导零兼容，例如 `123` 可命中 `0000000123`。
- `/api/faults`、`/api/orders` 的 `items[]` 同时返回稳定摘要字段和 `raw`。`raw` 是当前 MongoDB 源文档的可扩展快照，不承诺字段集合稳定；需要逐字段兼容时应锁定同步版本或使用详情接口。三个 `/api/views/{viewID}` 列表返回完整视图文档，并补充稳定 `id`。
- MongoDB 索引应与同步脚本的 `index_fields` 对齐，并补充复合索引：维修 `ZMCOD1 + ZDATE_WX`、`AUFNR + ZDATE_WX`、`VBELN + ZDATE_WX`；订单 `source + aufnr`、`data.VBELN`、`data.GSTRS`；工位 `PCODE + ACTUAL_START_TIME`、`AUFNR + ACTUAL_START_TIME`、`KDAUF + ACTUAL_START_TIME`；BOM `AUFNR_1 + BUDAT_MKPF`、`VBELN_EX + BUDAT_MKPF`。索引创建应由迁移/运维任务执行，不在查询路径即时创建。

### 销售订单原始数据 `GET /api/orders`

在既有 `page`、`pageSize`、`keyword`、`source`、`gstrsFrom`、`gstrsTo` 基础上支持：

| 参数 | 说明 |
|---|---|
| `salesOrder` / `productionOrder` | 销售订单/生产订单精确或逗号批量过滤，生产订单兼容前导零 |
| `serialNumber` | 按已同步订单源记录中的 SN 字段反查；如果源订单没有 SN，结果为空，不跨集合猜测关联 |
| `productModel` | 精确匹配 `MAKTX_TH`、`CPXH` 或 `MAKTX` |
| `customer` | 精确匹配 `KID` 或 `NAME1_ZU` |
| `base` | 精确匹配 `LGORT` 或 `WERKS` |
| `dateFrom` / `dateTo` | `GSTRS` 日期范围，`YYYY-MM-DD` 或 SAP 无分隔日期均可 |
| `orderScope` | 来源范围（当前为 `SG` 或 `KK`），与旧参数 `source` 兼容 |

`items[].raw` 中保留源字段和同步字段；`raw.data` 至少可取 `AUFNR`、`VBELN`、`KID`、`NAME1_ZU`、`MAKTX`、`MAKTX_TH`、`LGORT`、`GSTRS`、`WMENG`、`GAMNG`、`ZSTAT`、`AUART`、`IF_L6`，顶层可取 `source`。该接口不聚合或计算任何质量指标。

### 维修故障原始数据 `GET /api/faults`

除既有参数外支持：`sns`、`productionOrders`、`salesOrders`（均为逗号批量）、`dateFrom`、`dateTo`、`station`、`productModel`。仍按 `ZDATE_WX`、`ZTIME` 倒序，**逐条返回且不按 SN 去重**。`items[].raw` 保留完整原始文档及 `_source_key`；所需字段包括 `PCODE`、`AUFNR`、`VBELN`、`ZDATE_WX`、`ZTIME`、`ZNGGZ`、`ZNGWD`、`ZWXWD`、`ZJXMC`、`ZUSER`、`ERROR_CODE`、`ERROR_MSG`、`U_FIX`。

专用别名简化调用：

```text
GET /api/faults/by-sns?sns=SN001,SN002
GET /api/faults/by-orders?productionOrders=PO001,PO002
```

二者与 `/api/faults` 使用相同分页、排序和返回格式，可叠加日期、工位等过滤器。

### 工位过站原始数据 `GET /api/views/Z_V_ZMES_T_001`

支持 `page`、`pageSize`、`dateFrom`、`dateTo`、`stationCode`、`sn`、`productionOrder`、`salesOrder`、`base`、`productModel`。原始字段包括 `HISTROYID`、`PCODE`、`OCODE`、`AUFNR`、`SPEC`、`OPERATION`、`GSTRS`、`ACTUAL_START_TIME`、`ACTUAL_END_TIME`。响应附加 `stationCode`：优先取 `LINE_CODE`，否则使用 `SPEC`，再否则 `OPERATION`；原始工位字段始终保留。该接口不计算过站数量或工位直通率。

### 序列号绑定原始数据 `GET /api/views/ZSGV_ZPP_SERNOLIST`

支持 `headOrder`、`itemOrder`、`headSn`、`itemSn` 以及分页参数，分别对应 `AUFNR_HEAD`、`AUFNR_ITEM`、`ZCODE_HEAD`、`ZCODE_ITEM`。因此可按主订单查子订单、整机 SN 查部件 SN、部件 SN 反查整机 SN。结果不去重，保留重复绑定和冲突行；不在此接口判断复合机型故障或回溯 RTY。必要原始字段为 `ZCODE_HEAD`、`ZCODE_ITEM`、`AUFNR_HEAD`、`AUFNR_ITEM`、`PRODH`。

### BOM 过账原始数据 `GET /api/views/ZSGV_ZSD124`

支持 `productionOrder`、`salesOrder`、`productModel`、`materialCode`、`dateFrom`、`dateTo` 和分页参数，分别过滤 `AUFNR_1`、`VBELN_EX`、`MATNR`、`BUDAT_MKPF`。其中 `productModel` 在该视图按 `MATNR` 匹配。返回原始 BOM 行，包含 `MBLNR`、`MJAHR`、`ZEILE`、`MATNR`、`WERKS`、`BWART`、`MENGE_A`、`AUFNR_1`、`VBELN_EX`、`BUDAT_MKPF`；由调用方决定 LRR 或复合机型关联规则。

### 详情与数据状态

```text
GET /api/faults/detail?id=...
GET /api/orders/detail?id=...
GET /api/views/{viewID}/detail?id=...
GET /api/sync/status
GET /api/data-status
```

三个详情接口均保留源字段名并返回完整原始文档字段（不是看板摘要）。`/api/sync/status` 返回同步任务的运行状态和脚本摘要；`/api/data-status` 返回各集合的最新同步时间并带上当前任务的 `state`、`startedAt`、`finishedAt`：

```json
{
  "salesOrdersLastSyncedAt": "2026-09-02T01:00:00Z",
  "faultsLastSyncedAt": "2026-09-02T01:02:00Z",
  "stationRecordsLastSyncedAt": "2026-09-02T01:03:00Z",
  "serialBindingsLastSyncedAt": "2026-09-02T01:04:00Z",
  "bomPostingsLastSyncedAt": "2026-09-02T01:05:00Z",
  "state": "success",
  "startedAt": "2026-09-02T01:00:00Z",
  "finishedAt": "2026-09-02T01:05:00Z"
}
```

统一错误响应（除根路径重定向外）：

```json
{"error":"错误描述"}
```

状态码按接口实现如下：`200` 成功，`202` 已接受异步任务，`400` 缺少 `id` 或视图 ID/视图参数无效，`404` 记录不存在，`409` 已有同步任务运行，`500` 维修/订单/状态接口的数据库或服务端错误，`503` 仅 `/api/health` 在 MongoDB 不可用时返回，`501` 可选扩展 store 未提供对应能力时返回。非法 `page`、`pageSize` 不报错，而是按默认值处理。

## 2. 四个业务看板

四个业务看板分别为：

1. 维修故障记录：`ZSGV_ZZT_WLJL` -> `repair_records_sap`
2. 订单 BOM 过账：`ZSGV_ZSD124` -> `order_bom_postings_sap`
3. 序列号绑定：`ZSGV_ZPP_SERNOLIST` -> `serial_bindings_sap`
4. 工位记录：`Z_V_ZMES_T_001` -> `station_records_sap`

销售订单看板（`/api/orders*`）也是前端提供的独立数据模块，接口说明见第 4 节。

### 2.1 维修故障记录

#### `GET /api/faults`

分页查询维修记录。数据按 `ZDATE_WX`、`ZTIME` 倒序排列。

查询参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `page` | integer | 页码，从 1 开始，默认 1 |
| `pageSize` | integer | 每页条数，1-100，默认 20 |
| `keyword` | string | 匹配 `ERROR_CODE`、`ERROR_MSG`、`ZGZMS`、`RPDESC`、`ZMCOD1`、`PCODE`、`VBELN`、`AUFNR` |
| `hostBarcode` | string | 精确匹配主机条码 `PCODE` |
| `defectResponsibility` | string | 精确匹配缺陷责任分类 `ZZRFL` |
| `ngStation` | string | 精确匹配 NG 工站 `ZNGGZ` |
| `salesOrder` | string | 精确匹配销售订单 `VBELN` |
| `productionOrder` | string | 精确匹配生产订单 `AUFNR` |

响应示例：

```json
{
  "items": [{
    "id": "MANDT:PCODE:...",
    "hostBarcode": "PC-001",
    "serialNumber": "SN-001",
    "errorCode": "E100",
    "faultDescription": "风扇异常",
    "errorDescription": "测试失败",
    "reviewProblem": "复判问题描述",
    "defectResponsibility": "来料",
    "repairAt": "2026-01-02T03:04:05Z",
    "readBy": "reader",
    "repairPerson": "repairer",
    "salesOrder": "SO-001",
    "productionOrder": "PO-001",
    "plannedStartDate": "2026-01-01",
    "materialCode": "MAT-001",
    "materialDescription": "物料描述",
    "ngStation": "工站 A",
    "retestStation": "工站 B",
    "repairRemarks": "维修备注"
  }],
  "page": 1,
  "pageSize": 20,
  "total": 1
}
```

`repairAt` 由维修日期/时间字段组合，无法解析时为 Go 的零值时间（JSON 为 `"0001-01-01T00:00:00Z"`）。`plannedStartDate` 对应维修记录的 `GSTRS`，由维修数据清理脚本按生产订单从销售订单表回填。列表同时返回摘要字段和可扩展的 `raw`；详情接口额外返回格式化的 `fields` 和顶层 `raw`。

#### `GET /api/faults/stats`

接受与 `/api/faults` 相同的筛选参数，但不分页，统计当前筛选结果。

响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `total` | integer | 维修记录数 |
| `withError` | integer | `ERROR_CODE` 或 `ERROR_MSG` 非空的记录数 |
| `withRepairPerson` | integer | `U_FIX` 非空的记录数 |
| `salesOrders` | integer | 去重后的非空 `VBELN` 数 |
| `productionOrders` | integer | 去重后的非空 `AUFNR` 数 |
| `hostBarcodes` | integer | 去重后的非空主机条码 `PCODE` 数 |
| `missingSalesOrder` | integer | `VBELN` 为空、null 或不存在的记录数 |
| `missingProductionOrder` | integer | `AUFNR` 为空、null 或不存在的记录数 |
| `dataStartDate` / `dataEndDate` | string | 当前结果的 `ZDATE_WX` 最小/最大值 |
| `latestSyncedAt` | string | 最近同步时间，UTC ISO-8601 |

#### `GET /api/faults/detail?id={id}`

按 `_source_key` 或 `_id` 查询单条完整维修记录。`id` 必填，建议直接使用列表响应中的 `items[].id`。

响应结构：`{"fault": <Fault>, "fields": [{"key":"PCODE","label":"主机条码","value":"PC-001"}]}`。`fields` 包含源文档中所有非空字段；未知字段标签显示为 `未定义字段（字段名）`。不存在返回 `404`。

### 2.2 三个 HANA 视图看板

三个视图看板共用以下接口模板：

```text
GET /api/views/{viewID}
GET /api/views/{viewID}/stats
GET /api/views/{viewID}/detail?id={id}
```

允许的 `viewID`、集合、日期字段和列表列如下：

| `viewID` | MongoDB 集合 | 日期筛选字段 | 看板主要列 |
|---|---|---|---|
| `ZSGV_ZSD124` | `order_bom_postings_sap` | `BUDAT_MKPF` | `MBLNR`, `MJAHR`, `ZEILE`, `MATNR`, `WERKS`, `BWART`, `MENGE_A`, `AUFNR_1`, `VBELN_EX`, `BUDAT_MKPF` |
| `ZSGV_ZPP_SERNOLIST` | `serial_bindings_sap` | 无 | `ZCODE_HEAD`, `ZCODE_ITEM`, `AUFNR_HEAD`, `AUFNR_ITEM`, `PRODH` |
| `Z_V_ZMES_T_001` | `station_records_sap` | `ACTUAL_START_TIME` | `HISTROYID`, `PCODE`, `OCODE`, `AUFNR`, `SPEC`, `OPERATION`, `GSTRS`, `ACTUAL_START_TIME`, `ACTUAL_END_TIME` |

#### `GET /api/views/{viewID}`

分页查询视图原始文档。

查询参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `page` / `pageSize` | integer | 分页参数，规则同 `/api/faults` |
| `keyword` | string | 在该视图登记的搜索字段中进行不区分大小写匹配 |
| `from` | string | 日期下限，格式 `YYYY-MM-DD`；无日期字段的视图忽略 |
| `to` | string | 日期上限，格式 `YYYY-MM-DD`；无日期字段的视图忽略 |

日期行为：`ZSGV_ZSD124` 会将日期转换为 `YYYYMMDD` 与 `BUDAT_MKPF` 比较；`Z_V_ZMES_T_001` 直接使用 `ACTUAL_START_TIME` 比较。上下限均为包含关系。

响应示例：

```json
{"items":[{"_id":"...","MBLNR":"50000001","MATNR":"MAT-001","id":"..."}],"page":1,"pageSize":20,"total":1}
```

后端会新增公开字段 `id`，取值优先级为 `_source_key`、其次 `_id`，不会移除其他源字段。详情请求应使用 `items[].id`；不要假设 `id` 一定等于 `_id`。

#### `GET /api/views/{viewID}/stats`

接受与对应列表接口相同的筛选参数，返回当前筛选结果统计：

```json
{
  "total": 1250,
  "salesOrders": 80,
  "productionOrders": 112,
  "missingSalesOrder": 23,
  "missingProductionOrder": 8,
  "dataStartDate": "20260101",
  "dataEndDate": "20260131",
  "latestSyncedAt": "2026-02-01T02:00:00Z"
}
```

对于 `ZSGV_ZSD124`，`salesOrders` 按非空 `VBELN_EX` 去重，`productionOrders` 按非空 `AUFNR_1` 去重；`missingSalesOrder` 和 `missingProductionOrder` 分别统计这两个字段为空、`null` 或不存在的 BOM 行。其他视图的订单去重统计返回 `0`。

`missingSalesOrder` 与 `missingProductionOrder` 仅对工位记录 `Z_V_ZMES_T_001` 有效，分别统计 `KDAUF`、`AUFNR` 缺失、`null`、空字符串或仅空格的记录；其他视图返回 `0`。无日期字段的 `ZSGV_ZPP_SERNOLIST` 只返回 `total`，日期和同步时间为空字符串。

#### `GET /api/views/{viewID}/detail?id={id}`

按 `_source_key` 或 `_id` 查询完整视图记录。响应结构：

```json
{"item":{"_id":"...","ZCODE_HEAD":"HEAD-001","id":"..."},"fields":[{"key":"ZCODE_HEAD","label":"大刀/机头序列号（ZCODE_HEAD）","value":"HEAD-001"}]}
```

`fields` 为所有非 null 字段。`ZSGV_ZPP_SERNOLIST` 使用以下中英对照字段标签：

| 字段 | 中文（English） |
|---|---|
| `ZCODE_HEAD` | 大刀/机头序列号（Head serial number） |
| `ZCODE_ITEM` | 小刀/BOX序列号（Item/BOX serial number） |
| `AUFNR_HEAD` | 大刀/机头生产订单号（Head production order） |
| `AUFNR_ITEM` | 小刀/BOX生产订单号（Item/BOX production order） |
| `PRODH` | 产品层次（Product hierarchy） |

`ZSGV_ZSD124` 的 BOM 过账详情字段使用业务中文标签，例如 `MBLNR` 为“物料凭证号”、`MJAHR` 为“物料凭证年度”、`ZEILE` 为“物料凭证行项目”、`MATNR` 为“物料号”、`BWART` 为“移动类型”、`MENGE_A` 为“过账数量”、`AUFNR_1` 为“生产订单”、`VBELN_EX` 为“销售订单”、`BUDAT_MKPF` 为“过账日期”。同步审计字段也会显示为“源记录键”“源视图”“同步批次标识”“同步时间”等中文标签。

其他未定义字段使用“字段 / Field（字段名）”格式。未知 `viewID` 返回 `400`，缺少 `id` 返回 `400`，记录不存在返回 `404`。

## 3. 看板调用示例

```bash
# 维修记录第 1 页，搜索主机条码并限定 NG 工站
curl 'http://127.0.0.1:18080/api/faults?page=1&pageSize=20&keyword=PC-001&ngStation=%E5%B7%A5%E7%AB%99%20A'

# 订单 BOM 过账：按 2026 年 1 月筛选
curl 'http://127.0.0.1:18080/api/views/ZSGV_ZSD124?from=2026-01-01&to=2026-01-31&page=1&pageSize=20'

# 查询视图统计和详情
curl 'http://127.0.0.1:18080/api/views/Z_V_ZMES_T_001/stats?keyword=PO-001'
curl 'http://127.0.0.1:18080/api/views/Z_V_ZMES_T_001/detail?id=record-001'
```

## 4. 销售订单看板 API

销售订单数据来自 SAP HTTP 接口，MongoDB 集合默认为 `sales_orders_sap`。列表按 `data.GSTRS`、`last_synced_at` 倒序排列。

### `GET /api/orders`

查询参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `page` / `pageSize` | integer | 分页参数，规则同上 |
| `keyword` | string | 匹配生产订单、销售订单、客户 ID、最终用户、生产机型或计划开始时间 |
| `source` | string | SAP 来源，支持 `SG`、`KK` |
| `gstrsFrom` / `gstrsTo` | string | 按订单计划开始日期过滤，支持 `YYYY-MM-DD` 或 `YYYYMMDD`；`dateFrom` / `dateTo` 为兼容别名，优先级高于本参数 |

响应 `items[]` 字段：`id`（`{source}:{AUFNR}`）、`source`、`aufnr`、`salesOrder`、`customerId`、`finalUser`、`materialDescription`、`productionModel`、`inventoryLocation`、`plannedStartDate`、`orderQuantity`、`storageQuantity`、`recordCount`。

### `GET /api/orders/stats`

接受相同筛选参数，返回 `total`、去重销售订单数 `salesOrders`、来源订单数 `sg`/`kk`、数量汇总 `orderQuantity`、机器数量汇总 `machineQuantity`（取明细 `GAMNG` 汇总）、`storageQuantity`、`dataStartDate`、`dataEndDate` 和 `latestSyncedAt`。

### `GET /api/orders/models`

返回销售订单数据中去重、排序后的生产机型候选列表：`{"items":["机型 A","机型 B"]}`。可传 `keyword` 进行不区分大小写的候选搜索。前端各看板的机型/产品层次筛选共用此列表，并保留手工输入能力。

销售订单看板的“导出数据”按钮调用 `/api/orders?all=true`，按当前筛选条件查询全量结果（单次最多 10,000 条）并生成 UTF-8 CSV；其它看板仍按分页接口自动拉取各页数据。

### `GET /api/orders/detail?id={source}:{AUFNR}`

按订单文档 `_id` 查询完整字段，`id` 格式为同步生成的 `{source}:{AUFNR}`。响应结构为 `{"order": <Order>, "fields": [...], "raw": <object>}`。`fields` 同时包含订单文档字段（如 `sync_count`、`last_synced_at`）和源接口字段，源接口字段使用 `data.<字段名>` 作为 `key`。

## 5. 健康检查与同步状态

### `GET /api/health`

MongoDB 可连通时返回 `200`：`{"status":"ok"}`；不可用时返回 `503`：`{"status":"unhealthy","error":"..."}`。

### `POST /api/sync/incremental`

启动一次异步增量同步。后端先刷新销售订单参考数据，再依次同步并清洗工位记录、维修故障明细、订单 BOM 过账和序列号绑定。任一阶段失败会停止后续阶段。成功启动返回 `202` 及同步状态；已有任务运行时返回 `409`，响应同时包含当前 `status`。

### `GET /api/sync/status`

返回同步状态快照。典型字段如下：

```json
{
  "state": "idle",
  "message": "等待同步",
  "startedAt": "",
  "finishedAt": "",
  "summary": {
    "sales_orders": {"success": true},
    "station_records": {"success": true},
    "repair_records": {"success": true},
    "order_bom_postings": {"success": true},
    "serial_bindings": {"success": true}
  }
}
```

`state` 通常为 `idle`、`running`、`success` 或 `failed`。任务刚启动时 `summary` 可能为空；任务结束后按脚本文件名记录各脚本输出摘要。前端在 `running` 时轮询此接口；状态回到非 `running` 后重新加载当前看板数据。

## 6. 前端推荐调用流程

1. 页面加载时并行请求列表接口和对应的 `stats` 接口。
2. 用户提交筛选条件时将页码重置为 1，并把相同筛选参数传给列表和统计接口。
3. 点击列表行时使用 `id` 调用对应 `detail` 接口；详情字段只读展示。
4. 点击“刷新”只重新请求 MongoDB API；点击“立即同步”后轮询 `/api/sync/status`，同步结束再刷新列表和统计。
