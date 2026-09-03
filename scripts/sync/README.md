# 同步任务

此目录是项目唯一的同步任务入口。共享 HANA、SAP HTTP 和 MongoDB 实现位于 `scripts/sources/` 与 `scripts/maintenance/`，不应直接作为定时任务执行。

| 脚本 | 目标数据 | 日常执行 |
|---|---|---|
| `sync_sales_orders.py` | 销售订单 `sales_orders_sap` | `python scripts/sync/sync_sales_orders.py` |
| `station_records.py` | 工位记录 `station_records_sap` | `python scripts/sync/station_records.py --mode incremental --apply` |
| `增量同步和清洗维修故障记录.py` | 维修记录 `repair_records_sap` | `python scripts/sync/增量同步和清洗维修故障记录.py --apply` |
| `order_bom_postings.py` | BOM 过账 `order_bom_postings_sap` | `python scripts/sync/order_bom_postings.py --mode incremental --apply` |
| `serial_bindings.py` | 序列号绑定 `serial_bindings_sap` | `python scripts/sync/serial_bindings.py --mode incremental --apply` |

## 执行顺序

必须按销售订单、工位、维修、BOM、序列号绑定的顺序串行执行。维修清洗依赖已同步的销售订单和工位数据；维修脚本默认执行 HANA 增量同步和完整订单回填，不删除维修记录。

首次验证建议先对每个脚本使用 `--dry-run`。销售订单脚本的 `--dry-run` 只读；其余三个 HANA 视图脚本使用 `--mode incremental --dry-run`。维修数据仅重跑回填时使用 `--apply --skip-hana-sync`。

## Crontab

建议在 SAP 数据落库完成后的低峰时段运行一次完整串行任务。下例每天 02:15 运行，使用绝对路径、独立日志和 `flock` 防止 cron 重入。将 `/opt/production-fault-db` 和 Python 路径替换为实际部署值，并预先创建 `/var/log/production-fault-db`。

```cron
15 2 * * * /usr/bin/flock -n /tmp/production-fault-sync.cron.lock /bin/sh -lc 'cd /opt/production-fault-db && /opt/production-fault-db/.venv/bin/python scripts/sync/sync_sales_orders.py && /opt/production-fault-db/.venv/bin/python scripts/sync/station_records.py --mode incremental --apply && /opt/production-fault-db/.venv/bin/python scripts/sync/增量同步和清洗维修故障记录.py --apply --no-progress --log-level ERROR && /opt/production-fault-db/.venv/bin/python scripts/sync/order_bom_postings.py --mode incremental --apply && /opt/production-fault-db/.venv/bin/python scripts/sync/serial_bindings.py --mode incremental --apply' >> /var/log/production-fault-db/sync.log 2>&1
```

前端“立即同步”使用相同顺序。不要让 cron 与前端同步同时运行；各脚本同时持有本地锁和 MongoDB 租约锁，发生冲突时任务会失败且不会推进当前阶段水位线。
