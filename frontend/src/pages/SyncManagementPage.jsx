import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, History, Play, RefreshCw, RotateCw } from 'lucide-react';
import { ApiError } from '../api/client';
import { createSyncRun, retrySyncRun, syncRuns, syncTasks, dataCheckConfig, saveDataCheckConfig, startDataCheck, dataCheckRuns } from '../api/sync';

const terminal = new Set(['success', 'failed', 'abandoned']);

function formatTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-';
}

function StageList({ stages = [] }) {
  return <div className="sync-stage-list">{stages.map(stage => <div className={`sync-stage ${stage.state || 'pending'}`} key={stage.task_id}>
    <span className="sync-stage-dot" /><div><strong>{stage.label || stage.task_id}</strong><small>第 {stage.attempts || 0} 次尝试{stage.progress?.phase ? ` · ${stage.progress.phase}` : ''}{stage.progress?.total ? ` · ${stage.progress.scanned}/${stage.progress.total}（${stage.progress.percent}%）` : ''}{stage.error?.message ? ` · ${stage.error.message}` : ''}</small></div><b>{stage.state || 'pending'}</b>
  </div>)}</div>;
}

export function SyncManagementPage({ onStarted }) {
  const [tasks, setTasks] = useState([]);
  const [runs, setRuns] = useState([]);
  const [mode, setMode] = useState('incremental');
  const [selected, setSelected] = useState(new Set());
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [checkConfig, setCheckConfig] = useState({ enabled: false, dailyTime: '03:00', timezone: 'Asia/Shanghai' });
  const [checkRuns, setCheckRuns] = useState([]);
  const [checkBusy, setCheckBusy] = useState(false);
  const [checkError, setCheckError] = useState('');
  const checkConfigDirty = useRef(false);
  const checkConfigRequest = useRef(0);

  const load = async () => {
    const requestVersion = ++checkConfigRequest.current;
    const [taskResult, runResult, configResult, checkRunResult] = await Promise.all([syncTasks(), syncRuns(), dataCheckConfig(), dataCheckRuns()]);
    const nextTasks = taskResult?.items || [];
    setTasks(nextTasks);
    setRuns(runResult?.items || []);
    if (requestVersion === checkConfigRequest.current && !checkConfigDirty.current) {
      setCheckConfig(configResult?.config || { enabled: false, dailyTime: '03:00', timezone: 'Asia/Shanghai' });
    }
    setCheckRuns(checkRunResult?.items || []);
    setSelected(current => current.size ? current : new Set(nextTasks.map(task => task.id)));
  };
  useEffect(() => { load().catch(() => setError('无法读取同步管理状态')); }, []);
  useEffect(() => {
    if (!runs.some(run => run.state === 'running')) return undefined;
    const timer = setInterval(() => load().catch(() => {}), 2000);
    return () => clearInterval(timer);
  }, [runs]);
  useEffect(() => {
    if (!checkRuns.some(run => run.state === 'running')) return undefined;
    const timer = setInterval(() => load().catch(() => {}), 3000);
    return () => clearInterval(timer);
  }, [checkRuns]);
  const selectedIds = useMemo(() => [...selected], [selected]);
  const toggle = id => setSelected(current => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; });

  async function start() {
    if (!selectedIds.length) { setError('至少选择一个同步任务'); return; }
    if (mode === 'full' && (!startDate || !confirmed)) { setError('全量同步需要起始日期和二次确认'); return; }
    setBusy(true); setError('');
    try {
      const result = await createSyncRun({ mode, taskIds: selectedIds, startDate: mode === 'full' ? startDate : undefined, endDate: endDate || undefined });
      onStarted?.(result); await load();
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : '同步启动失败');
    } finally { setBusy(false); }
  }
  async function retry(id) {
    setBusy(true); setError('');
    try { const result = await retrySyncRun(id); onStarted?.(result); await load(); } catch (requestError) { setError(requestError.message || '重试启动失败'); } finally { setBusy(false); }
  }

  async function updateCheckConfig(next) {
    checkConfigDirty.current = true;
    ++checkConfigRequest.current;
    setCheckBusy(true); setCheckError('');
    try {
      const result = await saveDataCheckConfig({ ...checkConfig, ...next });
      setCheckConfig(result?.config || { ...checkConfig, ...next });
      checkConfigDirty.current = false;
      // Invalidate loads that started while the save was in flight. They may
      // still return a pre-save server snapshot.
      ++checkConfigRequest.current;
    } catch (requestError) {
      setCheckError(requestError.message || '保存比对设置失败');
    } finally { setCheckBusy(false); }
  }

  async function runCheck() {
    setCheckBusy(true); setCheckError('');
    try { await startDataCheck(); await load(); }
    catch (requestError) { setCheckError(requestError.message || '启动数据比对失败'); }
    finally { setCheckBusy(false); }
  }

  const latestCheck = checkRuns[0];
  const checkSummary = latestCheck?.summary || {};
  const checkStateLabel = { running: '运行中', success: '正常', warning: '发现差异', failed: '失败' };

  return <main className="sync-management"><section className="hero"><div><p className="eyebrow">SYNCHRONIZATION CONTROL</p><h1>同步管理</h1><p className="intro">HANA 增量同步由本机 crontab 每 2 小时错峰执行：工位偶数时 :05、BOM 偶数时 :45、维修奇数时 :25；其他看板任务保留原计划。</p></div><div className="hero-meta"><div><span>已注册任务</span><strong>{tasks.length}</strong></div><div><span>运行中</span><strong>{runs.filter(run => run.state === 'running').length}</strong></div></div></section>
    <section className="data-check-card"><div className="data-check-heading"><div><h2>数据源一致性比对</h2><p>每日比对最近 3 个月 HANA 工位过站记录与 MongoDB，并在发现差异时发送光圈提醒。</p></div><label className="data-check-toggle"><input type="checkbox" checked={checkConfig.enabled} disabled={checkBusy} onChange={event => updateCheckConfig({ enabled: event.target.checked })} /><span>{checkConfig.enabled ? '已开启' : '已关闭'}</span></label></div><div className="data-check-controls"><label>每日执行时间（上海）<input type="time" value={checkConfig.dailyTime || '03:00'} onChange={event => { checkConfigDirty.current = true; ++checkConfigRequest.current; setCheckConfig(current => ({ ...current, dailyTime: event.target.value })); }} /></label><button type="button" className="sync-retry" disabled={checkBusy} onClick={() => updateCheckConfig({ dailyTime: checkConfig.dailyTime })}>保存时间</button><button type="button" className="sync-run-button" disabled={checkBusy || checkRuns.some(run => run.state === 'running')} onClick={runCheck}><Play size={15} />{checkRuns.some(run => run.state === 'running') ? '正在比对' : '立即比对'}</button></div>{checkError && <p className="sync-form-error"><AlertTriangle size={15} />{checkError}</p>}<div className="data-check-latest"><strong>最近一次：{latestCheck ? (checkStateLabel[latestCheck.state] || latestCheck.state) : '尚未运行'}</strong>{latestCheck && <span>{formatTime(latestCheck.started_at)} · 源端 {checkSummary.source_rows ?? '—'} 条 / MongoDB {checkSummary.mongo_rows ?? '—'} 条 · 缺失 {checkSummary.missing_in_mongo ?? 0} · 多余 {checkSummary.extra_in_mongo ?? 0} · 字段差异 {checkSummary.field_mismatches ?? 0} · 重复 {checkSummary.duplicate_rows ?? 0}</span>}</div>{latestCheck?.summary?.notification?.success === false && <p className="data-check-notice-error">光圈通知未发送：{latestCheck.summary.notification.error}</p>}</section>
    <section className="sync-workspace"><div className="sync-launch"><div className="section-head"><div><h2>新建同步</h2><p>选择目标任务后，系统会自动补齐必要依赖。</p></div><div className="sync-mode"><button className={mode === 'incremental' ? 'active' : ''} onClick={() => { setMode('incremental'); setConfirmed(false); }}>增量</button><button className={mode === 'full' ? 'active' : ''} onClick={() => { setMode('full'); setStartDate(value => value || '2026-01-01'); }}>全量</button></div></div>
      <div className="sync-task-grid">{tasks.map(task => <label className="sync-task" key={task.id}><input type="checkbox" checked={selected.has(task.id)} onChange={() => toggle(task.id)} /><span><strong>{task.label}</strong><small>{task.dependencies?.length ? `依赖：${task.dependencies.join('、')}` : '无前置依赖'}</small><small>{task.nextSyncAt ? `下次计划：${formatTime(task.nextSyncAt)}（${task.schedule}）` : `下次同步：未安排（${task.schedule || '手动触发'}）`}</small>{task.id === 'repair_records' && <><small>最近运行：{task.lastRunAt ? formatTime(task.lastRunAt) : '尚无记录'} · 最近成功：{task.lastSuccessAt ? formatTime(task.lastSuccessAt) : '尚无记录'}</small><small>最新 HANA 数据时间：{task.latestDataAt || '尚无记录'}</small></>}</span><em className={task.state || 'idle'}>{task.state || 'idle'}</em></label>)}</div>
      {mode === 'full' && <div className="sync-full-options"><label>开始日期<input aria-label="全量开始日期" type="date" value={startDate} onChange={event => setStartDate(event.target.value)} required /></label><label>结束日期<input aria-label="全量结束日期" type="date" value={endDate} onChange={event => setEndDate(event.target.value)} /></label><label className="sync-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我确认执行全量同步。销售订单仅在 2026-01-01 至当天时清理范围外旧单，更晚的起始日期只补数不删历史</label></div>}
      {error && <p className="sync-form-error"><AlertTriangle size={15} />{error}</p>}<button className="sync-run-button" onClick={start} disabled={busy}><Play size={16} />{busy ? '正在提交' : `执行${mode === 'full' ? '全量' : '增量'}同步`}</button></div>
      <aside className="sync-history"><div className="section-head"><div><h2><History size={18} />运行历史</h2></div><button className="icon-button" title="刷新历史" onClick={() => load().catch(() => setError('刷新失败'))}><RefreshCw size={15} /></button></div>{!runs.length && <p className="sync-empty">尚无编排运行记录</p>}{runs.map(run => <article className="sync-run" key={run._id}><div className="sync-run-head"><div><strong>{run.mode === 'full' ? '全量同步' : '增量同步'}</strong><small>{formatTime(run.started_at)}</small></div><span className={`sync-run-state ${run.state}`}>{run.state}</span></div><StageList stages={run.stages} />{terminal.has(run.state) && run.state !== 'success' && <button className="sync-retry" onClick={() => retry(run._id)} disabled={busy}><RotateCw size={14} />重试</button>}</article>)}</aside></section></main>;
}
