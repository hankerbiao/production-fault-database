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

`syncctl.py run --mode incremental` 按注册表串行运行销售订单、工位、维修、BOM、BOM 计划开始时间回填、SCS DOA 和 SCS 换上换下；会自动补齐选中任务的依赖。序列号绑定暂时从编排中暂停，包括显式选择和重试的任务列表；单独脚本入口仍可使用。Python 与 Go 共用 `backend/internal/syncmanifest/tasks.json`。维修任务完整刷新当月及前两个月。运行状态、阶段摘要和失败原因存入 MongoDB 的 `sync_orchestrator_runs`。各下游来源通过已验证的销售明细确定产品归属；DOA 的 `XNC` 公司分类与产品准入独立，换上换下按已验证 DOA 的服务单关系准入。

编排器同时维护 `sync_publications` 的 `prodh-00100` 发布账本。销售订单、工位和维修三个核心任务全部成功后才标记 `publicationState=ready`；核心任务失败或仅执行部分任务时，保留上一版 `active_run_id`，不会把半套数据标记为看板可用。非 SCS 看板读取已关联销售订单的版本 2 标记；SCS 沿用原规则。质量看板应优先读取 `/api/data-status` 的 `publicationReady` 和 `publicationState`。

```bash
.venv/bin/python scripts/sync/syncctl.py run --mode incremental
.venv/bin/python scripts/sync/syncctl.py run --mode full --tasks repair_records --start-date 2026-01-01
.venv/bin/python scripts/sync/syncctl.py tasks
```

同步 2025 全年且排除 SCS 时，使用 `--skip-scs`：

```bash
.venv/bin/python scripts/sync/syncctl.py run --mode full --start-date 2025-01-01 \
  --end-date 2025-12-31 --skip-scs --source 2025-full
```

`source=cron` 不允许跳过 SCS，避免 DOA/MFR 看板在 SCS 停更时被误判为完整同步。只有临时运维核查才可显式设置 `SYNC_ALLOW_CRON_SKIP_SCS=true`，正式定时任务应移除 `--skip-scs`。

销售相关入口固定 `PRODH_LIST=["00100"]`。`--all-prodh` 明确报错；`--prodh-list` 和 `SOURCE_PRODH_LIST` 只接受 `00100`。销售及状态请求始终发送该过滤。SAP 已确认会执行过滤且响应可能不含 `PRODH`，销售订单不增加范围标记；若响应明确含有其他 `PRODH`，仍不写入。销售集合必须只包含经过该过滤的订单。

工位、维修、BOM 和序列号以已同步销售订单的生产订单号或销售订单号判定公司归属，不使用来源自身的产品层次或 BOM 的 CPX 判定。未知归属保存在 `sync_scope_pending`，后续运行自动重试；BOM 不按 CPX 删除。订单字段仅在参考关系明确时补全。销售订单表为空时，下游同步失败并保留水位。维修 SAP 序列号补全暂不可用，待确认接口支持不带产品层次查询后恢复。

升级前应核实销售订单集合的准确性。非 SCS 看板只显示已按新规则标记的数据；历史数据暂不迁移，未被后续同步重新读取的旧记录不会出现在新看板中。

销售先将所有日期窗口暂存到 SQLite，重复明细去重，同键冲突失败。全量按本次源结果覆盖同一订单的明细，源未返回的其他订单不删除；增量合并返回的明细并保留未返回的旧明细。订单不写入 `_prodh_scope`、`_scope_rule_version` 或 `_source_hash`。

销售检查点绑定产品范围、方法和源地址的摘要；工位和 BOM 分别使用原有的 `hana_views:Z_V_ZMES_T_001` 和 `hana_views:ZSGV_ZSD124` 检查点。工位和 BOM 缺少水位时必须显式指定增量起始日期，避免无时间下限的读取。序列号无日期字段，仍需读取全表筛选。

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

销售、HANA 和 SCS 全量默认仅更新，不按“本次未返回”物理删除。当前来源没有提供严格覆盖证据，`--replace-collection` 即使带 `--confirm-delete` 也被拒绝。`--allow-large-cleanup` 不会恢复缺少依据的删除。来源已消失的记录需另行核实；摘要明确标为 `cleanup=disabled_unverified_coverage`。

销售订单日常增量完成后，默认额外按 7 天窗口读取 `2026-01-01` 至本次结束日期的 SG/KK 历史数据，仅刷新源返回的已有订单的 `data.ZSTAT`、`data.AUART` 及对应 `records` 明细字段。来源和生产订单号共同限定本次订单范围，代表行和明细均按 `MANDT/AUFNR/VBELN/POSNR/ETENR/MATNR` 严格匹配，不使用单行兜底。完整读取两来源并校验状态快照后才写入；只修改状态、订单类型及独立的核对元数据，不导入或删除历史订单，不覆盖数量等其他字段。

同一订单的代表行或任一已有明细未匹配、状态或订单类型字段缺失或为 null 时，整单保留原值，不写核对元数据，也不计入预计变更或字段迁移；该阶段报告失败，不推进本次销售订单水位。空字符串是明确的源值，允许写入。写入阶段并非事务，失败后可重跑；其他完整匹配订单可能已更新。

源中未返回的本地订单不参与本次核对，以 `not_returned` 和最多 20 条 `not_returned_samples` 单独报告，替代旧的 `missing_orders` / `missing_order_samples` 字段，不使任务失败。摘要包含 `scope: returned_orders`；合法空响应表示本次没有可核对订单，`checked=0`。SAP 查询日期 `CHDAT` 与本地计划日期 `GSTRS` 没有已验证的等价关系，不能据此断言未返回订单丢失，也不能把任务成功解释为全部本地历史订单已完成核对。

状态快照保存于临时 SQLite，通过订单分组索引及行主键查询；处理时只保留当前订单和一个写入批次，不再构建全量历史状态字典。单个 SAP 日期窗口的 HTTP 响应仍由接口一次性返回；`FULL_WINDOW_DAYS` 控制请求窗口，`SYNC_BATCH_SIZE` 控制写入批次。

`--status-start-date` 可调整历史状态范围；`--skip-status-refresh` 可显式跳过该核对。全量不重复执行状态读取。`--dry-run` 也预览历史状态差异，但不写入元数据或水位。

默认保持每次核对全年。若业务允许历史状态最多约两周延迟，可显式设置：

```dotenv
STATUS_RECENT_DAYS=30
STATUS_HISTORY_WINDOWS=3
STATUS_HISTORY_MAX_INTERVAL_DAYS=14
SYNC_RUNS_PER_DAY=1
```

以 2026-10-27、7 天窗口为例，每来源 5 个近期窗口及 3 个历史窗口，两来源共 16 次状态请求，普通增量请求另计。预算不足以满足声明的最大间隔会报错。历史游标按来源和配置隔离，只在核对及写入成功后推进；各实际窗口独立保存核对时间。每日运行次数是配置承诺，需由调度保证。未返回的本地订单仍不代表已核对。

仅刷新已有订单状态，使用维护脚本（不推进增量水位）：

```bash
.venv/bin/python scripts/maintenance/refresh_sales_order_status.py
.venv/bin/python scripts/maintenance/refresh_sales_order_status.py --apply
```

维护脚本默认只读预览，支持 `--start-date`、`--end-date` 和仅限 `00100` 的 `--prodh-list`；`--all-prodh` 被拒绝。正式写入与日常销售订单同步共用集合锁。

## 运行与恢复

所有正式入口按实际集合持有 `dataset:<集合名>` 的续租锁，BOM 回填与 BOM 同步共享同一资源锁；编排器另持有流程锁。`SYNC_MONGO_LOCK_TTL_SECONDS` 默认 120 秒，续租异常或持有者变化会阻止后续写入、删除和提交。升级前应等待旧版本同步进程结束，旧脚本的锁名与新资源锁不兼容。

编排器等待子任务时持续更新运行心跳。`SYNC_TASK_TIMEOUT_SECONDS` 默认 21600 秒（6 小时）；超时、失锁或终止信号会终止并回收子进程组。瞬时连接错误、请求超时等最多尝试 3 次；失败后剩余阶段标记 `skipped`。stdout/stderr 实时写入 `logs/syncctl/<运行ID>.<任务>.<尝试次数>.*.log`，可通过 `SYNC_TASK_LOG_DIR` 修改目录。运行记录保存 `log_prefix`。日志没有自动过期策略，需要按部署容量定期保留或清理。

HANA、销售和 SCS 先暂存并校验源结果，相同内容重复只保留一份，同键不同内容在该来源写库前失败。SCS 检测重复页、超大页、登录页和异常表结构；短页只代表读取结束，不提供删除证明。销售各来源独立检查空返回，另一来源的正常数据不能抵消。

只保留有正向依据的清理：序列号精确去重，以及维修记录中字段兼容的重复项。BOM 和工位不会因 CPX 或订单参考缺失而删除。所有删除仍分批复核租约。

旧的 `enforce_prodh_scope.py` 使用旧范围规则，不能用于本次非 SCS 数据的公司归属判定。

历史归档集合可用以下维护脚本清理，默认仅预览。正式执行会先取得所有相关数据集资源锁，只删除已知数据集的 `_archive` 集合，保留正式集合、同步水位、运行记录和回填审计；可重复执行。

```bash
python scripts/maintenance/remove_sync_archives.py --dry-run
python scripts/maintenance/remove_sync_archives.py --apply
```

预览读取与正式执行相同的检查点，不创建索引、数据库锁或运行记录。工位、BOM 和维修在本次源快照上计算清洗及回填候选。预览仍需 MongoDB 读取权限、源访问权限和临时磁盘空间。源校验和目标写入分阶段执行，整个窗口并非数据库事务；中途失败可能留下部分 upsert，检查点和清理不会越过失败步骤，重跑会重新读取窗口并幂等覆盖。

SCS 重跑从窗口第一页读取。DOA 全量强制刷新详情；增量刷新新单、列表变化、待补、旧解析版本及过期详情。`SCS_DETAIL_TTL_DAYS` 默认 7 天；历史每次最多额外核对 `SCS_DETAIL_RETRY_LIMIT` 条，默认 100。解析规则为 v3：DOA 和产品页面必须包含订单标签，服务页面必须包含设备表；已有详情字段突然缺失视为解析不完整，显式空值与缺字段分开处理。只有实际成功抓取且通过校验才更新 `_detail_fetched_at` 和 `_detail_schema_version`。失败保留旧有效列表和详情，新列表行持久化待补；严格 full 返回失败，不推进检查点。若源页面正式删除或改名字段，需先核实并调整解析规则，不能自动用残缺页面覆盖。

DOA 和换上换下按批次查询已有记录；增量内容 hash 无变化时跳过业务写入。DOA 修改分类或服务单关系前，先登记旧、新服务单到 `sync_classification_pending`。换上换下在两集合锁内重算当前所有关联，成功写入后按 generation 确认；中断可幂等重试。正式分类维护入口也登记标记。迁移期保留原有全历史分类兜底频率，尚未将它的开销隐藏为已消除。

序列号 `incremental` 仍读取全表并修复已验证范围内的精确重复，不按缺失清理。工位开始时间和 SCS 创建日期不能反映所有历史修改，超出回看期的更正需要指定范围补读。SCS 换上换下继续固定 2026 年范围。

维修记录默认按 `ZDATE_WX` 维修日期刷新当月及前两个月。例如结束日期为 `2026-10-14` 时，窗口为 `2026-08-01` 至 `2026-10-14`（包含边界），最早不超过 `2025-01-01`。不再读取或推进维修 `ZDATE/ZTIME` 检查点，`--sync-lookback-days` 仅保留命令兼容。`ZWXDT` 支持 `YYYYMMDDHHMMSS` 和 HANA 返回的 `YYYY/MM/DD HHMMSS` 两种形式，写入前统一规范为 14 位日期时间。已有历史数据继续保留；首次导入全部历史仍需使用 `full`。

维修明细身份为 `MANDT + PCODE + ZWXDT + ZMCOD1 + ZMCOD2 + ZRCOD1 + ZRCOD2`，允许更换前后部件码为空；不包含读取日期和时间。身份缺失或同身份源内容冲突在写库前失败。部件码更正会改变身份，旧身份需要另外核实，不能凭缺失自动删除。

维修源快照及现有目标索引均保存到临时 SQLite，内存只解码当前身份组和写入批次；同身份目标超过 10000 条时阻断。正式写入前必须取得可信产品关联，未知源记录仅存待核对。旧文档优先原地迁移并保存 `_legacy_ids`。源内容无变化时只更新覆盖标记和最近核对时间，不改 `_synced_at`。

同步及回填成功并验证覆盖数量后，在本次 `target_range` 内按 `PCODE + ZWXDT + ZMCOD1 + ZRCOD1 + MATNR` 分组清理：相同非空字段一致或一方为空时补全；任何业务字段存在不同的非空值时整组保留。缺少主机条码或有效维修时间的记录保留，部件码和物料号为空时按空值匹配。只处理包含本次同步记录的组；合并后 v3 身份与其他记录冲突的组保留。多个本次源身份合并后保留别名，下次同步仍分别处理各源身份。清理结果报告合并、删除、冲突和无效键计数；`--dry-run` 不执行或预估此清理。全表预览使用 `python scripts/maintenance/cleanup_repair_duplicates.py`；加 `--apply` 会先备份整个集合再执行。各组写入及删除不是事务，失败后需重试核对。

源内容变化时，仅保留经当前参考组合与主机关联重新核验的回填字段；失效的旧订单与派生计划日期清空，以便重新回填。日常回填处理本次写入，工位查询按对应主机条码限制范围；`--skip-hana-sync` 保留单独回填历史未解决记录的入口。维修待核对项独立按更新时间轮转，每次最多重试 `REPAIR_SCOPE_RETRY_LIMIT` 条（默认 100），不因离开滚动窗口而失去恢复机会；只恢复 2025 年起、不晚于本次结束日期的数据。本地目标索引和覆盖核验纳入这些历史身份，摘要以 `target_range` 标明，源查询窗口不变。范围外项核实后清除待核对标记。源读取、写入或回填失败都不会进入后续清理；清理失败不提交水位。写入和清理不是整窗口原子操作，失败后可重跑恢复。

维修记录按月重同步直接调用维修脚本，不通过 `syncctl.py`：先确认该月销售订单和过站数据已就绪，再执行 `--mode monthly --start-date 2026-09-01 --sync-end-date 2026-09-30 --dry-run`，确认后将 `--dry-run` 改为 `--apply`。`full`、`monthly` 与默认滚动刷新均读取指定源窗口，并限额恢复历史待核对项，均不推进维修水位。

维修 `--dry-run` 报告 `would_insert`、`would_update`、`unchanged`、`relinked`、`duplicate_source_rows`、`stale_candidates` 及范围计数，不创建 MongoDB 索引、租约或待处理数据。`stale_candidates` 是未覆盖候选数量，不是已授权删除数量；`--allow-large-cleanup` 不能把它转为删除依据。

摘要中的 `fetched/source_rows` 是实际源读取量，`in_scope/out_of_scope/scope_pending` 包含本次重试处理量；同时报告持久化积压 `scope_backlog`。SCS 提供 HTTP 次数、批量查询次数和耗时，维修提供索引处理耗时及 SQLite 主文件分配字节数 `sqlite_bytes`（不含临时 journal）。临时 SQLite 需要与源和目标窗口规模相称的磁盘空间；单次 SAP 响应仍驻内存。没有真实环境测量前不承诺整体提速倍数。

首次验证建议先对每个脚本使用 `--dry-run`。销售订单脚本的 `--dry-run` 只读；其余三个 HANA 视图脚本使用 `--mode incremental --dry-run`。维修数据仅重跑回填时使用 `--apply --skip-hana-sync`。

## Crontab

建议在 SAP 数据落库完成后的低峰时段运行一次完整串行任务。下例每天 02:15 运行，使用绝对路径、独立日志和 `flock` 防止 cron 重入。将 `/opt/production-fault-db` 和 Python 路径替换为实际部署值，并预先创建 `/var/log/production-fault-db`。

```cron
15 2 * * * /usr/bin/flock -n /tmp/production-fault-sync.cron.lock /bin/sh -lc 'cd /opt/production-fault-db && /opt/production-fault-db/.venv/bin/python scripts/sync/syncctl.py run --mode incremental --source cron' >> /var/log/production-fault-db/sync.log 2>&1
```

前端“立即同步”使用相同顺序。不要让 cron 与前端同步同时运行；各脚本同时持有本地锁和 MongoDB 租约锁，发生冲突时任务会失败且不会推进当前阶段水位线。
