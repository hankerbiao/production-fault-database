# SCS DOA 设备日期接口

`GET /api/scs/doa/devices` 返回有 DOA 关联的设备证据，供下游按设备分析。设备读取不按产品线字段过滤；下游应通过销售订单看板数据确认目标订单范围。接口不改变故障数、在保判断或来源数据；网关本身没有认证授权，部署时仍须使用现有可信网络或上游权限控制。

## 查询

`page` 从 1 开始，默认 1；`pageSize` 为 1–1000，默认 100。`company5000` 接受 true/false，`salesOrder`、`serviceOrder`、`sn` 为精确匹配，均可省略。过滤条件必须由同一条关联证据满足；过滤在设备分组后进行，返回该设备在可见范围内的所有证据，方便发现冲突。`serviceOrder` 匹配服务单号及详情中的别名，`salesOrder` 匹配设备订单或申报订单，`sn` 匹配产品条码或物料 SN。

响应包括 `items`、`page`、`pageSize`、`total`、`hasMore`、`scope`，其中 `scope=all` 表示读取时不按产品线过滤，不表示所有设备均属于销售订单看板。按 `deviceKey` 排序，通过 `hasMore` 翻页可读完当前范围。每页会重新读取和分组当前数据库，跨页期间如数据变化，结果不具备快照一致性；大量数据会增加内存和读取开销，调用方应控制并发。

每个设备含 `deviceKey`、`identityBasis`、`deviceSn`、`salesOrder`、`productionOrder`、`factoryDate`、`warrantyEndDate`、`evidence`。后四个解析字段含 `value`、`status`、`candidates` 和可选 `invalid`：`valid` 表示唯一有效值；`missing` 表示无值；`invalid` 表示存在无法解析的日期；`conflict` 表示存在多个不同有效值。`invalid` 或 `conflict` 不输出推定值。日期取产品详情和设备列表全部非空值，只有标准化后相同才视为唯一值；不会强行选择优先来源。订单仅来自设备产品详情，不将申报级销售订单赋给设备。

身份优先级为产品详情的“产品条码”、产品详情 context、设备列表物料 SN；缺少所有标识时以申报及设备序号组成不可跨申报合并的键。产品条码、物料 SN 可能代表不同对象，不能因为二者不同就判断数据错误；`snRelation` 标示 `same`、`different`、`missing`。缺少产品条码时，同一设备可能因来源标识不同而未合并。

`evidence` 保留每条申报与设备关联：`doaCode`、`judgment`、`doaStatus`、`serviceOrder`、`serviceOrderAliases`、`doaSalesOrder`、设备 `salesOrder` 和 `productionOrder`、`salesOrderRelation`、`productBarcode`、`materialSn`、`snRelation`、`contextId`、`model`、`product`、设备列表与产品详情原始日期、申报的 `company5000`、`source`、`detailPending`。`company5000` 是申报级分类，不能自动证明设备产品详情中的订单属于 5000 公司；`salesOrderRelation=different` 明确标示设备订单与申报订单不一致。型号/产品优先产品详情，缺失时使用设备列表。

脱敏示例（同一设备有两次申报且质保日期冲突）：

```json
{"items":[{"deviceKey":"product:SN-EXAMPLE","identityBasis":"product_barcode","deviceSn":"SN-EXAMPLE","salesOrder":{"value":"SO-EXAMPLE","status":"valid","candidates":["SO-EXAMPLE"]},"productionOrder":{"value":"PO-EXAMPLE","status":"valid","candidates":["PO-EXAMPLE"]},"factoryDate":{"value":"2026-08-24","status":"valid","candidates":["2026-08-24"]},"warrantyEndDate":{"value":"","status":"conflict","candidates":["2029-09-23","2030-09-23"]},"evidence":[{"doaCode":"DOA-1","judgment":"是","doaStatus":"已受理","serviceOrder":"SERVICE-1","serviceOrderAliases":["SERVICE-1"],"doaSalesOrder":"SO-EXAMPLE","salesOrder":"SO-EXAMPLE","salesOrderRelation":"same","productionOrder":"PO-EXAMPLE","productBarcode":"SN-EXAMPLE","materialSn":"PART-EXAMPLE","snRelation":"different","contextId":"CTX-1","model":"MODEL","product":"PRODUCT","factoryProduct":"2026-08-24","factoryEquipment":"2026-08-24","warrantyProduct":"2029-09-23 00:00","warrantyEquipment":"2029-09-23","company5000":true,"source":"scs","detailPending":false}]}],"page":1,"pageSize":100,"total":1,"hasMore":false,"scope":"all"}
```

示例为结构演示；真实响应的 `evidence` 将包含所有可见申报。

## 换上换下关联及覆盖

现有 `/api/views/SCS_CHANGE/stream` 已提供 `so_code`（服务单）、`device_sn`、`part_sn` 和 `is_5000_company`；该接口中的服务单可与设备证据的 `serviceOrderAliases` 关联，设备或部件 SN 可用于进一步核对，不能只按服务单假设一对一。当前公司分类依据 DOA 服务单关联，无法保证非 DOA 服务单可关联到销售订单。多个订单或多台设备需由下游按证据处理。

2026-10-29 只读统计（以下为当时历史表全量的只读统计）：3,829 条标记 5000 公司的 DOA 中，3,770 条有设备明细、5,174 条设备关联；按产品条码得到 3,193 个设备标识，473 个标识关联多条设备记录；27 个标识有出厂日期冲突、83 个标识有质保日期冲突。635 条设备关联的产品销售订单与申报销售订单不同，1,405 条产品条码与物料 SN 不同。不同标识可能属于不同层级，不应自动合并。

换上换下表 215,181 条中，7,687 条可按服务单关联 DOA，207,494 条未匹配；4,912 条匹配到唯一申报销售订单，20 条对应多个销售订单；标记 5000 公司的 3,851 条均可匹配 DOA 服务单。这些是历史原始数据的关联统计，不证明来源全覆盖，也不等同于可授权范围。

设备接口不依赖 `_prodh_scope` 标记。DOA 申报和设备产品详情可能对应不同销售订单，实际使用时应与销售订单看板核对；详情采集失败时，已有数据可能不完整，需通过现有补偿机制重新获取。

按设备产品详情的销售订单，与当前销售订单集合中 `data.VBELN` 和 `records[].VBELN` 去前导零后匹配：历史 5000 申报中有 162 个销售订单、377 个产品条码和 1,090 条设备关联匹配到当前销售订单；其中 30 个销售订单对应多组产品详情的出厂日期和质保日期组合。这些统计按设备产品订单计算，不能推断订单内所有设备的日期相同。

SCS 产品实体详情只在 DOA 关联的设备页面按 context 抓取；项目中未发现可直接列出全部未报修设备及其保修日期的同步源。工位、序列号绑定、BOM 和销售订单表也没有已核实的完整保修日期清单。需要另行确认 SCS 或 SAP 是否有具备覆盖证明的产品实体列表接口；本接口不能作为全部在保设备清单。
