from __future__ import annotations

import fcntl
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import ExecutionTimeout, OperationFailure, ServerSelectionTimeoutError

from scripts.sources.hana.hana_view_sync import env, load_dotenv, mongo_client_options, mongo_uri
from scripts.sync.syncctl import SyncError, serialize, task_catalog, validate_request

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
STATE = Path(os.environ.get('SYNC_SERVICE_STATE_DIR', str(ROOT / '.run'))).resolve()
STATE.mkdir(parents=True, exist_ok=True)
DB = STATE / 'service.sqlite3'
LOGS = STATE / 'logs'
LOGS.mkdir(exist_ok=True)
ZONE = ZoneInfo('Asia/Shanghai')


@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    with connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, times TEXT NOT NULL, tasks TEXT NOT NULL);
            INSERT OR IGNORE INTO config VALUES (1, 1, '["08:00", "12:00", "15:00", "18:00", "22:00"]', '[]');
            CREATE TABLE IF NOT EXISTS launches (id TEXT PRIMARY KEY, source TEXT NOT NULL, requested_at TEXT NOT NULL, pid INTEGER, exit_code INTEGER);
            CREATE TABLE IF NOT EXISTS slots (slot TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS schedules (task_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, times TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule_queue (slot TEXT PRIMARY KEY, tasks TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
        ''')
        if not db.execute('SELECT 1 FROM schedules LIMIT 1').fetchone():
            old = db.execute('SELECT * FROM config WHERE id=1').fetchone()
            selected = json.loads(old['tasks']) or [task['id'] for task in task_catalog()]
            for task in task_catalog():
                db.execute('INSERT INTO schedules VALUES (?, ?, ?)', (task['id'], bool(old['enabled']) and task['id'] in selected, old['times'] if task['id'] in selected else '[]'))


class RunRequest(BaseModel):
    mode: str = 'incremental'
    taskIds: list[str] = Field(default_factory=list)
    startDate: str | None = None
    endDate: str | None = None
    confirmed: bool = False


class TaskSchedule(BaseModel):
    taskId: str
    enabled: bool = False
    times: list[str] = Field(default_factory=list)


class Schedule(BaseModel):
    items: list[TaskSchedule]


def mongo():
    return MongoClient(mongo_uri(), **mongo_client_options())


def collection(client):
    return client[env('MONGODB_DATABASE')]['sync_orchestrator_runs']


def config():
    with connect() as db:
        rows = db.execute('SELECT * FROM schedules ORDER BY task_id').fetchall()
    return {'items': [{'taskId': row['task_id'], 'enabled': bool(row['enabled']), 'times': json.loads(row['times'])} for row in rows]}


def start_run(request: RunRequest, source: str):
    if request.mode == 'full' and not request.confirmed:
        raise HTTPException(400, '全量同步需要确认')
    try:
        validate_request(request.mode, request.taskIds or None, request.startDate, request.endDate)
    except SyncError as exc:
        raise HTTPException(400, str(exc)) from exc
    run_id = uuid4().hex
    args = [sys.executable, str(ROOT / 'scripts/sync/syncctl.py'), 'run', '--mode', request.mode, '--source', source, '--run-id', run_id]
    if request.taskIds:
        args += ['--tasks', *request.taskIds]
    if request.startDate:
        args += ['--start-date', request.startDate]
    if request.endDate:
        args += ['--end-date', request.endDate]
    with connect() as db:
        db.execute('INSERT INTO launches (id, source, requested_at) VALUES (?, ?, ?)', (run_id, source, datetime.now(ZONE).isoformat()))
    try:
        with (LOGS / f'{run_id}.log').open('ab') as out:
            child = subprocess.Popen(args, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                     env={**os.environ, 'SYNC_PYTHON': sys.executable, 'SYNC_TASK_LOG_DIR': str(LOGS)})
        with connect() as db:
            db.execute('UPDATE launches SET pid=? WHERE id=?', (child.pid, run_id))
        def reap():
            code = child.wait()
            with connect() as db:
                db.execute('UPDATE launches SET exit_code=? WHERE id=?', (code, run_id))
        threading.Thread(target=reap, daemon=True).start()
    except OSError as exc:
        with connect() as db:
            db.execute('UPDATE launches SET exit_code=-1 WHERE id=?', (run_id,))
        raise HTTPException(500, '无法启动同步进程') from exc
    return {'runId': run_id}


def schedule_tick(now):
    slot = now.astimezone(ZONE).strftime('%Y-%m-%d %H:%M')
    selected = [item['taskId'] for item in config()['items'] if item['enabled'] and now.astimezone(ZONE).strftime('%H:%M') in item['times']]
    with connect() as db:
        if selected:
            db.execute('INSERT OR IGNORE INTO schedule_queue(slot,tasks) VALUES (?,?)', (slot, json.dumps(selected)))
        pending = db.execute("SELECT * FROM schedule_queue WHERE state='pending' ORDER BY slot LIMIT 1").fetchone()
        if not pending:
            return
        running = db.execute('SELECT pid FROM launches WHERE pid IS NOT NULL AND exit_code IS NULL').fetchall()
    for row in running:
        try:
            os.kill(row['pid'], 0)
            return
        except ProcessLookupError:
            pass
        except PermissionError:
            return
    try:
        with mongo() as client:
            stale_after = int(env('SYNC_ORCHESTRATOR_STALE_SECONDS', '300'))
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, stale_after))
            if collection(client).find_one({'state': 'running', 'heartbeat_at': {'$gte': cutoff}}):
                return
    except Exception as exc:
        print(f'读取同步状态失败，定时任务等待重试: {exc}', flush=True)
        return
    try:
        start_run(RunRequest(taskIds=json.loads(pending['tasks'])), 'independent_schedule')
        with connect() as db:
            db.execute("UPDATE schedule_queue SET state='started' WHERE slot=?", (pending['slot'],))
    except Exception as exc:
        print(f"定时同步启动失败 {pending['slot']}: {exc}", flush=True)


def scheduler(stop):
    while not stop.wait(10):
        try:
            schedule_tick(datetime.now(ZONE))
        except Exception as exc:
            print(f'定时调度异常: {exc}', flush=True)


@asynccontextmanager
async def lifespan(app):
    lock = (STATE / 'service.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise RuntimeError('同步服务已在运行') from exc
    init_db()
    stop = threading.Event()
    thread = threading.Thread(target=scheduler, args=(stop,), daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
        lock.close()


app = FastAPI(title='独立数据同步服务', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
data_times_lock = threading.Lock()


@app.get('/')
def index():
    return FileResponse(ROOT / 'index.html')


@app.get('/api/tasks')
def tasks():
    return {'items': task_catalog()}


@app.get('/api/data-times')
def data_times():
    with data_times_lock:
        return read_data_times(int(time.monotonic() // 60))


@lru_cache(maxsize=1)
def read_data_times(_minute):
    sources = {
        'sales_orders': (env('TARGET_COLLECTION', 'sales_orders_sap'), 'last_synced_at'),
        'station_records_sales_orders': (env('STATION_SALES_ORDER_COLLECTION', 'station_records_sales_orders_sap'), '_synced_at'),
        'repair_records': (env('REPAIR_COLLECTION', 'repair_records_sap'), '_synced_at'),
        'order_bom_postings': (env('BOM_COLLECTION', 'order_bom_postings_sap'), '_synced_at'),
        'scs_doa': (env('SCS_DOA_COLLECTION', 'scs_doa_records'), '_synced_at'),
        'scs_change': (env('SCS_CHANGE_COLLECTION', 'scs_change_records'), '_synced_at'),
    }
    def read(item):
        task_id, (name, field) = item
        try:
            with mongo() as client:
                doc = client[env('MONGODB_DATABASE')][name].find_one(
                    {field: {'$exists': True}}, {field: 1}, sort=[(field, -1)], max_time_ms=60000)
            return task_id, {'time': serialize(doc.get(field)) if doc else None, 'kind': 'data'}
        except Exception as exc:
            return task_id, {'time': None, 'kind': 'data', 'error': data_time_error(exc)}
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        items = dict(pool.map(read, sources.items()))
    try:
        with mongo() as client:
            stage_filter = {'task_id': 'order_bom_planned_start', 'state': 'success'}
            run = collection(client).find_one({'stages': {'$elemMatch': stage_filter}},
                {'stages': {'$elemMatch': stage_filter}}, sort=[('started_at', -1)], max_time_ms=60000)
        stage = run['stages'][0] if run else {}
        items['order_bom_planned_start'] = {'time': serialize(stage.get('finished_at')), 'kind': 'success'}
    except Exception as exc:
        items['order_bom_planned_start'] = {'time': None, 'kind': 'success', 'error': data_time_error(exc)}
    return {'items': items}


def data_time_error(exc):
    if isinstance(exc, ExecutionTimeout):
        return '查询超过 60 秒，可能需要为同步时间字段建立索引'
    if isinstance(exc, ServerSelectionTimeoutError):
        return '连接数据库超时，请检查数据库和网络'
    if isinstance(exc, OperationFailure) and exc.code in (13, 18):
        return '数据库认证或读取权限不足'
    return f'读取数据库失败（{type(exc).__name__}）'


@app.get('/api/runs')
def runs(limit: int = 30, offset: int = 0):
    if not 1 <= limit <= 100:
        raise HTTPException(400, 'limit 必须在 1 到 100 之间')
    if offset < 0:
        raise HTTPException(400, 'offset 不能为负数')
    try:
        with mongo() as client:
            items = list(collection(client).find().sort([('started_at', -1), ('_id', -1)]).skip(offset).limit(limit + 1))
        return {'items': serialize(items[:limit]), 'hasMore': len(items) > limit}
    except Exception as exc:
        raise HTTPException(503, '无法读取 MongoDB 同步记录') from exc


@app.get('/api/runs/recent')
def recent_runs():
    try:
        with mongo() as client:
            items = list(collection(client).find(
                {'started_at': {'$gte': datetime.now(timezone.utc) - timedelta(days=1)}},
                {'state': 1, 'mode': 1, 'source': 1, 'started_at': 1, 'message': 1,
                 'stages.task_id': 1, 'stages.label': 1, 'stages.state': 1, 'stages.error': 1, 'stages.message': 1},
            ).sort('started_at', -1))
        return {'items': serialize(items)}
    except Exception as exc:
        raise HTTPException(503, '无法读取 MongoDB 同步记录') from exc


@app.get('/api/launches')
def launches():
    with connect() as db:
        items = [dict(row) for row in db.execute('SELECT * FROM launches ORDER BY requested_at DESC LIMIT 30')]
    return {'items': items}


@app.get('/api/launches/{run_id}/log')
def launch_log(run_id: str):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise HTTPException(400, '无效的运行编号')
    path = LOGS / f'{run_id}.log'
    if not path.is_file():
        raise HTTPException(404, '日志不存在')
    with path.open('rb') as f:
        f.seek(max(0, path.stat().st_size - 128 * 1024))
        return {'log': f.read().decode('utf-8', 'replace')}


@app.post('/api/runs', status_code=202)
def create_run(request: RunRequest):
    return start_run(request, 'independent_api')


@app.post('/api/runs/{run_id}/retry', status_code=202)
def retry(run_id: str):
    with mongo() as client:
        run = collection(client).find_one({'_id': run_id})
    if not run:
        raise HTTPException(404, '同步运行不存在')
    if run.get('state') not in ('failed', 'abandoned'):
        raise HTTPException(400, '只能重试失败或中断的任务')
    return start_run(RunRequest(mode=run['mode'], taskIds=run.get('requested_task_ids') or [],
                                startDate=run.get('start_date'), endDate=run.get('end_date'), confirmed=True), 'independent_retry')


@app.get('/api/runs/{run_id}/logs/{task_id}')
def logs(run_id: str, task_id: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id) or task_id not in {t['id'] for t in task_catalog()}:
        raise HTTPException(400, '无效的运行或任务编号')
    with mongo() as client:
        run = collection(client).find_one({'_id': run_id})
    if not run:
        raise HTTPException(404, '同步运行不存在')
    stage = next((s for s in run.get('stages', []) if s.get('task_id') == task_id), None)
    if not stage or not stage.get('log_prefix'):
        raise HTTPException(404, '日志不存在')
    prefix = Path(stage['log_prefix']).resolve()
    allowed = [LOGS.resolve(), (ROOT / 'logs' / 'syncctl').resolve()]
    if not any(prefix.is_relative_to(p) for p in allowed):
        raise HTTPException(403, '日志不在本服务目录')
    result = {}
    for kind in ('stdout', 'stderr'):
        path = Path(f'{prefix}.{kind}.log')
        if path.is_file():
            with path.open('rb') as f:
                f.seek(max(0, path.stat().st_size - 128 * 1024))
                result[kind] = f.read().decode('utf-8', 'replace')
    return result


@app.get('/api/schedule')
def get_schedule():
    return config()


@app.get('/api/schedule/queue')
def schedule_queue():
    with connect() as db:
        rows = db.execute("SELECT slot, tasks FROM schedule_queue WHERE state='pending' ORDER BY slot").fetchall()
    return {'items': [{'slot': row['slot'], 'taskIds': json.loads(row['tasks'])} for row in rows]}


@app.put('/api/schedule')
def set_schedule(schedule: Schedule):
    known = {task['id'] for task in task_catalog()}
    ids = [item.taskId for item in schedule.items]
    if len(ids) != len(set(ids)) or set(ids) != known:
        raise HTTPException(400, '必须为每个任务提供唯一的调度配置')
    for item in schedule.items:
        if len(set(item.times)) != len(item.times) or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t) for t in item.times):
            raise HTTPException(400, f'{item.taskId}: 时间必须为不重复的 HH:MM')
        if item.enabled and not item.times:
            raise HTTPException(400, f'{item.taskId}: 启用时至少选择一个时间')
    with connect() as db:
        for item in schedule.items:
            db.execute('INSERT OR REPLACE INTO schedules VALUES (?, ?, ?)', (item.taskId, item.enabled, json.dumps(sorted(item.times))))
    return config()
