import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, History, Play, RefreshCw, RotateCw } from 'lucide-react';
import { ApiError } from '../api/client';
import { createSyncRun, retrySyncRun, syncRuns, syncTasks } from '../api/sync';

const terminal = new Set(['success', 'failed', 'abandoned']);

function formatTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-';
}

function StageList({ stages = [] }) {
  return <div className="sync-stage-list">{stages.map(stage => <div className={`sync-stage ${stage.state || 'pending'}`} key={stage.task_id}>
    <span className="sync-stage-dot" /><div><strong>{stage.label || stage.task_id}</strong><small>第 {stage.attempts || 0} 次尝试{stage.error?.message ? ` · ${stage.error.message}` : ''}</small></div><b>{stage.state || 'pending'}</b>
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

  const load = async () => {
    const [taskResult, runResult] = await Promise.all([syncTasks(), syncRuns()]);
    const nextTasks = taskResult?.items || [];
    setTasks(nextTasks);
    setRuns(runResult?.items || []);
    setSelected(current => current.size ? current : new Set(nextTasks.map(task => task.id)));
  };
  useEffect(() => { load().catch(() => setError('无法读取同步管理状态')); }, []);
  useEffect(() => {
    if (!runs.some(run => run.state === 'running')) return undefined;
    const timer = setInterval(() => load().catch(() => {}), 2000);
    return () => clearInterval(timer);
  }, [runs]);
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

  return <main className="sync-management"><section className="hero"><div><p className="eyebrow">SYNCHRONIZATION CONTROL</p><h1>同步管理</h1><p className="intro">统一编排数据源、依赖关系和可恢复的运行记录。</p></div><div className="hero-meta"><div><span>已注册任务</span><strong>{tasks.length}</strong></div><div><span>运行中</span><strong>{runs.filter(run => run.state === 'running').length}</strong></div></div></section>
    <section className="sync-workspace"><div className="sync-launch"><div className="section-head"><div><h2>新建同步</h2><p>选择目标任务后，系统会自动补齐必要依赖。</p></div><div className="sync-mode"><button className={mode === 'incremental' ? 'active' : ''} onClick={() => { setMode('incremental'); setConfirmed(false); }}>增量</button><button className={mode === 'full' ? 'active' : ''} onClick={() => setMode('full')}>全量</button></div></div>
      <div className="sync-task-grid">{tasks.map(task => <label className="sync-task" key={task.id}><input type="checkbox" checked={selected.has(task.id)} onChange={() => toggle(task.id)} /><span><strong>{task.label}</strong><small>{task.dependencies?.length ? `依赖：${task.dependencies.join('、')}` : '无前置依赖'}</small></span><em className={task.state || 'idle'}>{task.state || 'idle'}</em></label>)}</div>
      {mode === 'full' && <div className="sync-full-options"><label>开始日期<input aria-label="全量开始日期" type="date" value={startDate} onChange={event => setStartDate(event.target.value)} required /></label><label>结束日期<input aria-label="全量结束日期" type="date" value={endDate} onChange={event => setEndDate(event.target.value)} /></label><label className="sync-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我确认执行全量同步及成功后的范围清理</label></div>}
      {error && <p className="sync-form-error"><AlertTriangle size={15} />{error}</p>}<button className="sync-run-button" onClick={start} disabled={busy}><Play size={16} />{busy ? '正在提交' : `执行${mode === 'full' ? '全量' : '增量'}同步`}</button></div>
      <aside className="sync-history"><div className="section-head"><div><h2><History size={18} />运行历史</h2></div><button className="icon-button" title="刷新历史" onClick={() => load().catch(() => setError('刷新失败'))}><RefreshCw size={15} /></button></div>{!runs.length && <p className="sync-empty">尚无编排运行记录</p>}{runs.map(run => <article className="sync-run" key={run._id}><div className="sync-run-head"><div><strong>{run.mode === 'full' ? '全量同步' : '增量同步'}</strong><small>{formatTime(run.started_at)}</small></div><span className={`sync-run-state ${run.state}`}>{run.state}</span></div><StageList stages={run.stages} />{terminal.has(run.state) && run.state !== 'success' && <button className="sync-retry" onClick={() => retry(run._id)} disabled={busy}><RotateCw size={14} />重试</button>}</article>)}</aside></section></main>;
}
