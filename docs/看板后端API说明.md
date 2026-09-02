# 看板后端 API 说明

本文档描述产线故障数据库后端网关为看板提供的 HTTP API。接口只查询 MongoDB，不会在查询时访问 SAP 或 HANA；数据由同步任务写入 MongoDB 后供看板读取。

## 1. 基本约定

- 默认服务地址：`http://127.0.0.1:18080`
- 返回格式：`application/json; charset=utf-8`
- 请求方法：查询接口使用 `GET`；启动同步使用 `POST`
- 字符编码：UTF-8
- 所有查询参数均可省略；字符串参数会在后端 `TrimSpace` 后参与过滤
- 分页接口默认 `page=1`、`pageSize=20`；`pageSize` 超过 100 或小于 1 时按 20 处理
- 页码从 1 开始，排序由后端固定，调用方不需要传排序参数
- 关键字采用不区分大小写的正则匹配，后端会转义用户输入，不支持调用方注入正则表达式

统一错误响应：

```json
{"error":"错误描述"}
```

常见状态码：`200` 成功，`202` 已接受异步任务，`400` 参数或视图 ID 无效，`404` 记录不存在，`409` 已有同步任务运行，`500` 数据库或服务端错误，`503` MongoDB 不可用。

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

`repairAt` 由维修日期/时间字段组合，无法解析时为零值时间。列表只返回看板摘要字段，完整源字段通过详情接口读取。

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

后端会把 MongoDB `_id` 复制为公开字段 `id`，但不会移除其他源字段。详情按钮应使用 `items[].id`。

#### `GET /api/views/{viewID}/stats`

接受 `keyword`、`from`、`to`，返回当前筛选结果统计：

```json
{
  "total": 1250,
  "dataStartDate": "20260101",
  "dataEndDate": "20260131",
  "latestSyncedAt": "2026-02-01T02:00:00Z"
}
```

无日期字段的 `ZSGV_ZPP_SERNOLIST` 只返回 `total`，日期和同步时间为空字符串。

#### `GET /api/views/{viewID}/detail?id={id}`

按 `_source_key` 或 `_id` 查询完整视图记录。响应结构：

```json
{"item":{"_id":"...","MBLNR":"50000001","id":"..."},"fields":[{"key":"MBLNR","label":"MBLNR","value":"50000001"}]}
```

`fields` 为所有非 null 字段，视图字段标签默认使用字段名。未知 `viewID` 返回 `400`，缺少 `id` 返回 `400`，记录不存在返回 `404`。

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
| `gstrsFrom` / `gstrsTo` | string | 按 `data.GSTRS` 过滤，建议 `YYYY-MM-DD`；按字符串包含边界比较 |

响应 `items[]` 字段：`id`（`{source}:{AUFNR}`）、`source`、`aufnr`、`salesOrder`、`customerId`、`finalUser`、`materialDescription`、`productionModel`、`inventoryLocation`、`plannedStartDate`、`orderQuantity`、`storageQuantity`、`recordCount`。

### `GET /api/orders/stats`

接受相同筛选参数，返回 `total`、去重销售订单数 `salesOrders`、来源订单数 `sg`/`kk`、数量汇总 `orderQuantity`/`storageQuantity`、`dataStartDate`、`dataEndDate` 和 `latestSyncedAt`。

### `GET /api/orders/detail?id={source}:{AUFNR}`

按订单文档 `_id` 查询完整字段，响应结构为 `{"order": <Order>, "fields": [...]}`。`fields` 同时包含订单文档字段（如 `sync_count`、`last_synced_at`）和源接口字段，源接口字段使用 `data.<字段名>` 作为 `key`。

## 5. 健康检查与同步状态

### `GET /api/health`

MongoDB 可连通时返回 `200`：`{"status":"ok"}`；不可用时返回 `503`：`{"status":"unhealthy","error":"..."}`。

### `POST /api/sync/incremental`

启动一次异步增量同步，后端按顺序执行销售订单、维修记录和三个 HANA 视图同步脚本。成功启动返回 `202` 及同步状态；已有任务运行时返回 `409`，响应同时包含当前 `status`。

### `GET /api/sync/status`

返回同步状态快照。典型字段如下：

```json
{
  "state": "idle",
  "message": "等待同步",
  "startedAt": "",
  "finishedAt": "",
  "summary": {
    "sync_sales_orders.py": {"success": true},
    "sync_zsgv_zsd124.py": {"success": true},
    "sync_zsgv_zpp_sernolist.py": {"success": true},
    "sync_z_v_zmes_t_001.py": {"success": true}
  }
}
```

`state` 通常为 `idle`、`running`、`success` 或 `failed`。任务刚启动时 `summary` 可能为空；任务结束后按脚本文件名记录各脚本输出摘要。前端在 `running` 时轮询此接口；状态回到非 `running` 后重新加载当前看板数据。

## 6. 前端推荐调用流程

1. 页面加载时并行请求列表接口和对应的 `stats` 接口。
2. 用户提交筛选条件时将页码重置为 1，并把相同筛选参数传给列表和统计接口。
3. 点击列表行时使用 `id` 调用对应 `detail` 接口；详情字段只读展示。
4. 点击“刷新”只重新请求 MongoDB API；点击“立即同步”后轮询 `/api/sync/status`，同步结束再刷新列表和统计。
