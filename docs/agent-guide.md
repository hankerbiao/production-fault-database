# 产线故障数据库 Agent 指南

这是 MongoDB 数据网关的只读数据接口说明。默认地址：`http://127.0.0.1:18080`。

## 调用规则

- 所有查询接口返回 JSON；`page` 从 1 开始，`pageSize` 默认 20，最大 100。
- 字符串会去除首尾空格；关键字是不区分大小写的安全匹配。
- 日期优先使用 `YYYY-MM-DD`，SAP 原始日期也可能是 `YYYYMMDD`。
- 详情接口使用列表返回的 `id`：`GET /api/faults/detail?id=...`、`GET /api/orders/detail?id=...` 或 `/api/views/{viewID}/detail?id=...`。
- `items[]` 返回稳定摘要字段和 `raw` 源文档。需要完整字段时调用详情接口，不要假设 `raw` 字段集合固定。
- 错误统一为 `{ "error": "..." }`。查询失败不要根据空数组推断数据库为空，应检查 HTTP 状态码。

## 按任务选择接口

### 按生产订单查维修故障

```text
GET /api/faults/by-orders?productionOrders=PO001,PO002&page=1&pageSize=100
```

也可以使用 `GET /api/faults?productionOrder=PO001`。结果逐条返回，不按序列号去重；可叠加 `dateFrom`、`dateTo`、`station` 和 `timeField`。

### 按序列号查故障

```text
GET /api/faults/by-sns?sns=SN001,SN002&page=1&pageSize=100
```

需要 POST 批量提交时使用 `/api/faults/lookup` 或 `/api/faults/by-sns` 的 JSON 请求体；单次 SN 数量不要超过 10,000。

### 查询订单及原始 SAP 字段

```text
GET /api/orders?productionOrder=PO001&source=SG
GET /api/orders/detail?id=SG:PO001
```

订单全量查询使用 `GET /api/orders/all` 或 `/api/orders?all=true`，最多返回 10,000 条；超过上限时按日期分段查询。

### 查询序列号绑定

```text
GET /api/views/ZSGV_ZPP_SERNOLIST?headSn=HEAD001
GET /api/views/ZSGV_ZPP_SERNOLIST?itemSn=ITEM001
```

### 查询工位记录

```text
GET /api/views/Z_V_ZMES_T_001?sn=SN001&dateFrom=2026-01-01&dateTo=2026-01-31
```

### 查询 BOM 过账

```text
GET /api/views/ZSGV_ZSD124?productionOrder=PO001&dateFrom=2026-01-01
```

缺失销售订单排查使用 `missingSalesOrder=true`。BOM 和工位数据支持 TSV 流式导出：`GET /api/views/{viewID}/stream`，当前仅适用于 `ZSGV_ZSD124` 和 `Z_V_ZMES_T_001`。

### 判断数据是否最新

先调用 `GET /api/data-status` 查看各集合同步时间；需要查看当前任务进度时调用 `GET /api/sync/status`。

## 人工确认接口

`POST /api/sync/incremental` 会启动销售订单、维修、工位、BOM 和序列号同步任务。Agent 不应自动调用，必须获得人工确认；收到 `202` 后轮询 `/api/sync/status`，收到 `409` 表示已有任务运行。

## 数据域

| 数据域 | 主要接口 | 关键字段 |
| --- | --- | --- |
| 维修故障 | `/api/faults*` | `PCODE`, `AUFNR`, `VBELN`, `ERROR_CODE`, `ERROR_MSG`, `ZDATE_WX` |
| 销售订单 | `/api/orders*` | `AUFNR`, `VBELN`, `KID`, `MAKTX_TH`, `GSTRS`, `GAMNG`, `WMENG` |
| BOM 过账 | `/api/views/ZSGV_ZSD124*` | `MATNR`, `AUFNR_1`, `VBELN_EX`, `MENGE_A`, `BUDAT_MKPF` |
| 序列号绑定 | `/api/views/ZSGV_ZPP_SERNOLIST*` | `ZCODE_HEAD`, `ZCODE_ITEM`, `AUFNR_HEAD`, `AUFNR_ITEM`, `PRODH` |
| 工位记录 | `/api/views/Z_V_ZMES_T_001*` | `PCODE`, `OCODE`, `AUFNR`, `SPEC`, `ACTUAL_START_TIME`, `ACTUAL_END_TIME` |

机器契约：`GET /api/openapi.json`。
