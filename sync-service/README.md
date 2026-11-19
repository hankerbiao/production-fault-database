# 独立数据同步服务

这是原仓库同步脚本的副本，复制来源为工作区状态（Git 基准 `219f6befae9112600ab751904b74398304b2b5ea`，包含当时未提交修改）。副本需单独维护，切换前应比较原脚本的后续修改。

使用 Python 3.10+，在本目录创建 `.venv`，安装 `requirements.txt`，让本目录 `.env` 指向项目原配置（`ln -s ../.env .env`），确认 MongoDB、HANA、SAP 和 SCS 均指向目标环境。线上服务目录为 `/data/production-sync-service`，其 `.env` 应链接到 `/data/production-fault-database/.env`，以复用线上 HANA、MongoDB 和凭据配置。不要将凭据提交到仓库。

```sh
cd sync-service
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 18081 --workers 1
```

线上服务监听 `10.17.150.235:18081`，可通过 `http://10.17.150.235:18081/` 访问。服务没有鉴权，能访问该地址的人可修改调度并启动生产同步，应通过网络访问控制限制访问范围。`SYNC_SERVICE_STATE_DIR` 指定持久化的 SQLite、进程锁和日志目录，默认 `.run`；请对该目录定期备份。MongoDB 仍保存任务运行历史、水位线和锁。独立服务沿用原 `SYNC_CHECKPOINT_COLLECTION` 的检查点和 `SYNC_LOCK_COLLECTION` 的租约，不创建新的水位线；各脚本的检查点 ID、成功后推进规则保持不变。

自动调度由服务内置调度器执行，不需要 crontab。每个数据源默认启用，可分别配置多个每日时间；新建环境默认时间为 08:00、12:00、15:00、18:00、22:00（已有配置不变）；使用上海时间，包含周末。相同时刻的任务合并编排；已有同步运行或 MongoDB 不可用时，已到期任务保存在 SQLite 队列中等待。保存配置或关闭开关不会取消已入队任务。服务不运行时错过的时段不会补跑。编排任务通过现有 MongoDB 租约互斥；切换时仍应关闭旧调度。SQLite 使用单实例锁，不能用多 worker。

旧后端通过 `SYNC_SCHEDULER_ENABLED=false` 关闭内置同步调度。切换前记录旧服务时刻表，等待现有任务完成，在隔离数据源验证后停用旧调度，再启用新服务调度。出现问题时先停用新调度并等待任务结束，再恢复旧调度。不要同时启用两套调度。
