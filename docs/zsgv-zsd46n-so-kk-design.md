# ZSGV_ZSD46N_SO_KK 2026 同步与查询设计

## 目标

从 SAP HANA 视图 `"_SYS_BIC"."BW_LOCAL.SD/ZSGV_ZSD46N_SO_KK"` 同步 `ERDAT` 为 2026 年的销售订单交付明细至 MongoDB，并提供应用内的分页查询、筛选、详情和 CSV 导出。

## 数据范围与来源

- 业务日期：`ERDAT`，范围为 `20260101` 至 `20261231`，含首尾。
- HANA 连接：复用项目 `.env` 中 `HANA_ADDRESS`、`HANA_PORT`、`HANA_USER`、`HANA_PASSWORD` 配置。
- Mongo 集合：`sales_order_delivery_records_sap`。
- 同步 36 个源字段，并附加 `_source_view`、`_source_key`、`_sync_run_id`、`_synced_at`。
- Mongo 保留源视图全部字段；API 对外移除 `STRAS`（地址）、`TELF1`（电话）、`ORT01`（地点）。

## 同步方式

入口为 `scripts/sync/sales_order_delivery_records.py`，共用现有 HANA 查询、批次写入、checkpoint、运行记录、进程锁和 Mongo 租约机制。

```bash
# 只读预检
python scripts/sync/sales_order_delivery_records.py --mode full --dry-run

# 首次全量同步 2026 年数据
python scripts/sync/sales_order_delivery_records.py --mode full --apply

# 日常增量同步，按 ERDAT 水位线回看 7 天
python scripts/sync/sales_order_delivery_records.py --mode incremental --apply
```

任务注册在统一 `syncctl.py` 编排器和后端同步任务目录中，无前置依赖。脚本不接受外部日期覆盖，避免同步集合混入其他年份。全量模式成功后沿用公共同步逻辑清理本数据源不在本次范围内的记录并提交 checkpoint；失败时不推进 checkpoint。

同步键由 `VBELN`、`POSNR`、`VBELN_L`、`POSNR_L`、`SERNR`、`DATUM` 组成。全量 36 字段只读扫描发现，原五字段组合在当前 2026 数据中有 30,861 条碰撞，冲突行的 `DATUM` 不同；加入 `DATUM` 后于 2026-10-13 对 851,934 行完成校验，未发现重复键。任一键字段为空时，以全部源字段计算稳定哈希，避免不完整键行相互覆盖。全量写入前先对固定范围只读校验源键，发现冲突即停止且不写 Mongo；预检通过后脚本会再次读取 HANA 才开始写入。源视图未提供最后修改时间，增量模式只能按 `ERDAT` 水位线回看；已同步的旧订单若发生超过 7 天的历史修改，需要执行 2026 全量重建。

## 查询体验

新页面复用通用 HANA 视图仪表板和 `/api/views/{viewID}`、`/detail`、`/stats` 接口。列表默认展示创建日期、订单/行项目、交货单/行项目、物料、序列号、数量、工厂/库存地点、客户和交货日期，分页每页上限沿用 100。

支持创建日期范围及销售订单主筛选；高级筛选包含交货单、客户采购订单、客户编号、物料、序列号、工厂/库存地点、销售组织、销售办事处和关键字。日期 API 参数为 `YYYY-MM-DD`，Mongo 的 `ERDAT` 比较值转换成 `YYYYMMDD`。

详情可展示其他非敏感源字段。列表、详情、全量查询及 CSV 数据均不得返回 `STRAS`、`TELF1`、`ORT01`。默认排序和 Mongo 索引以 `ERDAT`、`VBELN`、`POSNR`、`SERNR` 为主，订单、交货单、序列号、物料和客户查询建立辅助索引。

## 验收

- 干跑仅读取 HANA，输出源行数，不写 Mongo。
- 全量后核对 Mongo 行数、2026 日期边界和若干订单/交货/序列号样本。
- 运行自动化测试，覆盖 SQL 日期范围、水位线回看、键生成、任务注册、Mongo 筛选、敏感字段脱敏、页面筛选及 CSV。
- 实际全量同步须由操作者在确认干跑结果后启动；本实现不自动触发生产数据写入。
