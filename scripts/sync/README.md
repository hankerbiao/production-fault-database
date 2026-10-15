# 同步任务

此目录是项目唯一的同步任务入口。`syncctl.py` 是唯一的流水线编排器；共享 HANA、SAP HTTP 和 MongoDB 实现位于 `scripts/sources/` 与 `scripts/maintenance/`，不应直接作为定时任务执行。

| 脚本 | 目标数据 | 日常执行 |
|---|---|---|
| `sync_sales_orders.py` | 销售订单 `sales_orders_sap` | `python scripts/sync/sync_sales_orders.py` |
| `station_records.py` | 工位记录 `station_records_sap` | `python scripts/sync/station_records.py --mode incremental --apply` |
| `增量同步和清洗维修故障记录.py` | 维修记录 `repair_records_sap` | `python scripts/sync/增量同步和清洗维修故障记录.py --apply` |
| `order_bom_postings.py` | BOM 过账 `order_bom_postings_sap` | `python scripts/sync/order_bom_postings.py --mode incremental --apply` |
| `../maintenance/backfill_order_bom_planned_start.py` | BOM 计划开始时间 | `python scripts/maintenance/backfill_order_bom_planned_start.py --apply` |
| `serial_bindings.py` | 序列号绑定 `serial_bindings_sap` | `python scripts/sync/serial_bindings.py --mode incremental --apply` |
| `../sources/scs_doa_sync.py` | SCS DOA | `python scripts/sources/scs_doa_sync.py` |
| `../sources/scs_change_sync.py` | SCS 换上换下 | `python scripts/sources/scs_change_sync.py` |

## 执行顺序

`syncctl.py run --mode incremental` 按注册表串行运行销售订单、工位、维修、BOM、BOM 计划开始时间回填、序列号、SCS DOA 和 SCS 换上换下；会自动补齐选中任务的依赖。Python 与 Go 共用 `backend/internal/syncmanifest/tasks.json`，修改任务时只维护这一份目录。维修任务在该入口下完整刷新当月及前两个月，其他任务继续使用各自的增量机制。运行状态、阶段摘要和失败原因存入 MongoDB 的 `sync_orchestrator_runs`。维修清洗使用销售订单和工位参考数据；SCS DOA 直接按自身销售订单号是否以 `XNC` 开头判定 5000 公司，不读取交付明细或销售订单参考表。换上换下继续依赖 DOA，以服务单号继承公司归属。

```bash
.venv/bin/python scripts/sync/syncctl.py run --mode incremental
.venv/bin/python scripts/sync/syncctl.py run --mode full --tasks repair_records --start-date 2026-01-01
.venv/bin/python scripts/sync/syncctl.py tasks
```

自 2026-10-15 起停用销售订单交付明细，任务注册、同步脚本、HANA 视图配置、查询 API、页面和物化汇总均已移除。升级时先重新构建并重启 Go 后端，停止旧版本的汇总监听，再清理数据库：

```bash
.venv/bin/python scripts/maintenance/remove_sales_order_delivery.py --dry-run
.venv/bin/python scripts/maintenance/remove_sales_order_delivery.py --apply
```

清理脚本只删除交付明细及其归档、物化汇总、汇总元数据，以及共享水位、运行记录和源版本集合中属于该视图的记录。正式执行持有交付集合资源锁和原同步入口的进程锁；默认仅预览，可重复执行，不访问 HANA。历史编排器运行记录保留。

DOA 判定从详情中的合同/订单号提取 `sales_order`，详情无有效订单号时沿用已有 `sales_order`；去除首尾空白后，以大小写不敏感的 `XNC` 前缀判定，其他值及空值均为 false，不再对订单号补位。同步列表写入、历史详情补偿和历史归属重算使用同一判定函数。

刷新已有 DOA 及其关联换上换下记录，无需重新抓取 SCS 或同步交付明细：

```bash
.venv/bin/python scripts/maintenance/refresh_scs_5000_company.py
.venv/bin/python scripts/maintenance/refresh_scs_5000_company.py --apply
```

默认只读预览输出两类记录的预计变更数。`--apply` 持有两个目标集合的资源锁，先更新 DOA，再按更新后的 DOA 服务单集合重算换上换下标记；只写入变化值，重复执行应为零变更。

销售订单全量默认只在 `start-date` 不晚于 `2026-01-01` 且结束日期覆盖当天时清理范围外旧单。从更晚日期重建只会 upsert，不会删除历史月份。强制整表替换需直接调用脚本并同时传入 `--replace-collection --confirm-delete`。

销售订单日常增量完成后，默认额外按 7 天窗口读取 `2026-01-01` 至本次结束日期的 SG/KK 历史数据，刷新已有订单的 `data.ZSTAT` 及对应 `records` 明细状态。明细按 `MANDT/AUFNR/VBELN/POSNR/ETENR/MATNR` 匹配，来源和生产订单号共同限定订单。完整读取两来源并校验状态快照后才写入；只修改状态及独立的 `_status_checked_at`、`_status_changed_at`、`_status_run_id` 元数据，不导入或删除历史订单，不覆盖数量等其他字段。空字符串是明确的源状态，允许写入；状态字段缺失或 null 时保留原值。源中未返回的本地订单、状态缺失或明细未匹配均报告数量并使任务失败，不推进本次销售订单水位。写入阶段并非事务，失败后可重跑；已成功核对的订单可能已更新。

`--status-start-date` 可调整历史状态范围；`--skip-status-refresh` 可显式跳过该核对。全量同步直接覆盖所选范围内的源字段，不重复执行状态读取。`--dry-run` 也预览历史状态差异，但不写入元数据或水位。

仅刷新已有订单状态，使用维护脚本（不推进增量水位）：

```bash
.venv/bin/python scripts/maintenance/refresh_sales_order_status.py
.venv/bin/python scripts/maintenance/refresh_sales_order_status.py --apply
```

维护脚本默认只读预览，支持 `--start-date`、`--end-date`、`--prodh-list` 和 `--all-prodh`；正式写入与日常销售订单同步共用集合锁。输出已有数量、匹配数量、未返回数量、预计及实际变更数和状态迁移统计，正式运行摘要写入 `sync_runs`。

## 运行与恢复

所有正式入口按实际集合持有 `dataset:<集合名>` 的续租锁，BOM 回填与 BOM 同步共享同一资源锁；编排器另持有流程锁。`SYNC_MONGO_LOCK_TTL_SECONDS` 默认 120 秒，续租异常或持有者变化会阻止后续写入、归档和提交。升级前应等待旧版本同步进程结束，旧脚本的锁名与新资源锁不兼容。

编排器等待子任务时持续更新运行心跳。`SYNC_TASK_TIMEOUT_SECONDS` 默认 21600 秒（6 小时）；超时、失锁或终止信号会终止并回收子进程组。瞬时连接错误、请求超时等最多尝试 3 次；失败后剩余阶段标记 `skipped`。stdout/stderr 实时写入 `logs/syncctl/<运行ID>.<任务>.<尝试次数>.*.log`，可通过 `SYNC_TASK_LOG_DIR` 修改目录。运行记录保存 `log_prefix`。日志及归档没有自动过期策略，需要按部署容量定期保留或清理。

HANA 和 SCS 先把完整源结果落入临时 SQLite，再校验业务键。相同内容重复只保留一份，同键不同内容会在写库前失败。全量清理仅在读取日期窗口内执行，先归档到 `<集合>_archive`，归档失败不删除。空源且本地存在数据、或待删除超过 100 条且超过本地范围的一半时默认阻断。BOM 的 CPX 过滤、工位的订单准入和序列号精确去重属于明确业务规则，同样先归档，但不使用快照收缩比例限制。

核实合法的源数据收缩后，可直接调用 HANA 表入口或 SCS 入口并显式传入 `--allow-large-cleanup`；此参数仅放开数量保护，不绕过分页、身份或页面结构校验，也不跳过归档。编排器不传此参数。销售订单仍使用已有的 `--confirm-delete` 确认机制。

预览读取与正式执行相同的检查点，不创建索引、数据库锁或运行记录。工位、BOM 和维修在本次源快照上计算清洗及回填候选。预览仍需 MongoDB 读取权限、源访问权限和临时磁盘空间。源校验和目标写入分阶段执行，整个窗口并非数据库事务；中途失败可能留下部分 upsert，检查点和清理不会越过失败步骤，重跑会重新读取窗口并幂等覆盖。

SCS 不按页码或行偏移跳过旧记录；重跑从窗口第一页读取，用业务键去重。分页错误、登录页和异常表结构均视为失败。DOA 详情失败保留旧有效详情，以 `_detail_pending`、`_detail_error` 和 `_detail_attempted_at` 记录补偿状态；每次额外补偿最多 `SCS_DETAIL_RETRY_LIMIT` 条历史待补记录，默认 100。两个 SCS 任务重算历史公司归属，只有值变化才写入。

序列号视图没有日期字段，`incremental` 仍读取全表并修复历史精确重复，但只在 `full` 时清理源中已消失记录。工位开始时间和 SCS 创建日期不能反映所有历史修改，超出回看期的更正需要指定范围的全量对账。SCS 换上换下继续固定 2026 年范围。

维修记录默认按 `ZDATE_WX` 维修日期刷新当月及前两个月。例如结束日期为 `2026-10-14` 时，窗口为 `2026-08-01` 至 `2026-10-14`（包含边界），最早不超过 `2026-01-01`。不再读取或推进维修 `ZDATE/ZTIME` 检查点，`--sync-lookback-days` 仅保留命令兼容。已有历史数据继续保留；首次导入全部历史仍需使用 `full`。

维修明细身份为 `MANDT + PCODE + ZWXDT + ZMCOD1 + ZMCOD2`，允许更换前后部件码为空；不包含读取日期和时间。相同维修时间可能对应多条部件明细，不能仅按主机和时间合并。身份缺失或同一身份对应不同源内容时，整次快照在 MongoDB 写入前失败。同内容源重复只保留一份。部件码更正会改变明细身份，但成功的窗口对账会归档清除旧明细。

脚本先把完整 HANA 结果保存到临时 SQLite 快照并校验，再按 `_repair_id` 写入和回填，最后核对正式集合的本次覆盖数量与快照一致后，归档清理该维修日期范围内未覆盖的旧记录。窗口之外和其他源视图不参与清理；本地销售订单仅用于匹配统计，参考数据为空不阻断同步，订单空值和未匹配的源维修记录保留并单独计数。旧文档优先原地迁移并保存 `_legacy_ids`，后端详情查询同时接受这些旧 ID。被清理的完整记录保存在 `<维修集合>_archive`，带有运行 ID、日期范围及归档原因。源内容无变化时只更新覆盖标记和最近核对时间，不改 `_synced_at`。

源内容变化时保留已回填的订单字段；源生产订单变化时清空原计划日期及不适用的销售订单，以便重新回填。日常回填只处理当前快照，工位查询按对应主机条码限制范围；`--skip-hana-sync` 保留单独回填历史未解决记录的入口。源读取、写入、回填或归档失败都不会进入后续清理；写入和清理不是整窗口原子操作，失败后可重跑恢复。

维修记录按月重同步直接调用维修脚本，不通过 `syncctl.py`：先确认该月销售订单和过站数据已就绪，再执行 `--mode monthly --start-date 2026-09-01 --sync-end-date 2026-09-30 --dry-run`，确认后将 `--dry-run` 改为 `--apply`。`full`、`monthly` 与默认滚动刷新均只对指定维修日期范围进行对账，均不推进维修水位。

维修脚本 `--dry-run` 读取源快照和本地数据，报告 `would_insert`、`would_update`、`unchanged`、`relinked`、`duplicate_source_rows` 和 `stale_candidates`，不写入 MongoDB、不创建数据库索引或获取 MongoDB 租约。若源快照为空但本地窗口有数据，或待清理数量超过 100 条且超过本地窗口的一半，默认阻断。核实后可在直接调用维修脚本时显式传入 `--allow-large-cleanup`；编排器默认不跳过此保护。

首次验证建议先对每个脚本使用 `--dry-run`。销售订单脚本的 `--dry-run` 只读；其余三个 HANA 视图脚本使用 `--mode incremental --dry-run`。维修数据仅重跑回填时使用 `--apply --skip-hana-sync`。

## Crontab

建议在 SAP 数据落库完成后的低峰时段运行一次完整串行任务。下例每天 02:15 运行，使用绝对路径、独立日志和 `flock` 防止 cron 重入。将 `/opt/production-fault-db` 和 Python 路径替换为实际部署值，并预先创建 `/var/log/production-fault-db`。

```cron
15 2 * * * /usr/bin/flock -n /tmp/production-fault-sync.cron.lock /bin/sh -lc 'cd /opt/production-fault-db && /opt/production-fault-db/.venv/bin/python scripts/sync/syncctl.py run --mode incremental --source cron' >> /var/log/production-fault-db/sync.log 2>&1
```

前端“立即同步”使用相同顺序。不要让 cron 与前端同步同时运行；各脚本同时持有本地锁和 MongoDB 租约锁，发生冲突时任务会失败且不会推进当前阶段水位线。
