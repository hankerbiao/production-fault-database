# 同步任务

此目录是项目唯一的同步任务入口。`syncctl.py` 是唯一的流水线编排器；共享 HANA、SAP HTTP 和 MongoDB 实现位于 `scripts/sources/` 与 `scripts/maintenance/`，不应直接作为定时任务执行。

| 脚本 | 目标数据 | 日常执行 |
|---|---|---|
| `sync_sales_orders.py` | 销售订单 `sales_orders_sap` | `python scripts/sync/sync_sales_orders.py` |
| `station_records.py` | 工位记录 `station_records_sap` | `python scripts/sync/station_records.py --mode incremental --apply` |
| `增量同步和清洗维修故障记录.py` | 维修记录 `repair_records_sap` | `python scripts/sync/增量同步和清洗维修故障记录.py --apply` |
| `order_bom_postings.py` | BOM 过账 `order_bom_postings_sap` | `python scripts/sync/order_bom_postings.py --mode incremental --apply` |
| `serial_bindings.py` | 序列号绑定 `serial_bindings_sap` | `python scripts/sync/serial_bindings.py --mode incremental --apply` |
| `sales_order_delivery_records.py` | 2026 销售订单交付明细 `sales_order_delivery_records_sap` | `python scripts/sync/sales_order_delivery_records.py --mode incremental --apply` |

## 执行顺序

`syncctl.py run --mode incremental` 按注册表串行运行销售订单、工位、维修、BOM、序列号、SCS DOA 和 SCS 换上换下；会自动补齐选中任务的依赖。运行状态、阶段摘要和失败原因存入 MongoDB 的 `sync_orchestrator_runs`，并通过可续租租约锁防止 cron、API 和命令行并发。维修清洗依赖已同步的销售订单和工位数据。

```bash
.venv/bin/python scripts/sync/syncctl.py run --mode incremental
.venv/bin/python scripts/sync/syncctl.py run --mode full --tasks repair_records --start-date 2026-01-01
.venv/bin/python scripts/sync/syncctl.py run --mode full --tasks sales_order_delivery_records --start-date 2026-01-01 --end-date 2026-12-31
.venv/bin/python scripts/sync/syncctl.py tasks
```

销售订单交付明细任务的日期边界固定为 `ERDAT` 2026 全年，编排器的其他日期参数不会扩大或缩小该数据集。首次可先运行 `python scripts/sync/sales_order_delivery_records.py --mode full --dry-run` 核对读取量，再通过同步管理页执行全量写入；日常编排使用增量模式并回看 7 天。源视图没有修改时间字段，因此超过回看期的历史更正需通过全量任务重新对账。

销售订单全量默认只在 `start-date` 不晚于 `2026-01-01` 且结束日期覆盖当天时清理范围外旧单。从更晚日期重建只会 upsert，不会删除历史月份。强制整表替换需直接调用脚本并同时传入 `--replace-collection --confirm-delete`。

维修记录按月重同步直接调用维修脚本，不通过 `syncctl.py`：先确认该月销售订单和过站数据已就绪，再执行 `--mode monthly --start-date 2026-09-01 --sync-end-date 2026-09-30 --dry-run`，确认后将 `--dry-run` 改为 `--apply`。月度模式只清理该月内未被本次 HANA 结果覆盖的维修记录，且不会推进全局维修增量水位。

首次验证建议先对每个脚本使用 `--dry-run`。销售订单脚本的 `--dry-run` 只读；其余三个 HANA 视图脚本使用 `--mode incremental --dry-run`。维修数据仅重跑回填时使用 `--apply --skip-hana-sync`。

## Crontab

建议在 SAP 数据落库完成后的低峰时段运行一次完整串行任务。下例每天 02:15 运行，使用绝对路径、独立日志和 `flock` 防止 cron 重入。将 `/opt/production-fault-db` 和 Python 路径替换为实际部署值，并预先创建 `/var/log/production-fault-db`。

```cron
15 2 * * * /usr/bin/flock -n /tmp/production-fault-sync.cron.lock /bin/sh -lc 'cd /opt/production-fault-db && /opt/production-fault-db/.venv/bin/python scripts/sync/syncctl.py run --mode incremental --source cron' >> /var/log/production-fault-db/sync.log 2>&1
```

前端“立即同步”使用相同顺序。不要让 cron 与前端同步同时运行；各脚本同时持有本地锁和 MongoDB 租约锁，发生冲突时任务会失败且不会推进当前阶段水位线。
