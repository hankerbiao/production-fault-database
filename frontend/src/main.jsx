import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Check, ChevronDown, Copy, Download, ExternalLink, Search } from 'lucide-react';
import { Header } from './components/Header';
import { RepairsPage } from './pages/RepairsPage';
import { OrdersPage } from './pages/OrdersPage';
import { ViewDashboardPage } from './pages/ViewDashboardPage';
import { orderModels } from './api/orders';
import { startSync, syncStatus } from './api/sync';
import { ApiError } from './api/client';
import { viewConfigs } from './config/views';
import './styles.css';

export function App() {
  const [view, setView] = useState('repairs');
  const [connected, setConnected] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const [status, setStatus] = useState({ state: 'idle', message: '等待同步' });
  const [modelOptions, setModelOptions] = useState([]);
  useEffect(() => { syncStatus().then(setStatus).catch(() => {}); }, []);
  useEffect(() => { orderModels().then(result => setModelOptions(Array.isArray(result?.items) ? result.items : [])).catch(() => {}); }, []);
  useEffect(() => {
    if (status.state !== 'running') return undefined;
    const timer = setInterval(async () => {
      try { const next = await syncStatus(); setStatus(next); if (next.state !== 'running') setRefreshToken(token => token + 1); } catch { /* Keep polling while the backend recovers. */ }
    }, 2000);
    return () => clearInterval(timer);
  }, [status.state]);
  async function runSync() {
    try { setStatus(await startSync()); } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.payload?.status) { setStatus(error.payload.status); return; }
      setStatus({ state: 'failed', message: error.message || '同步启动失败' });
    }
  }
  const pageProps = { modelOptions, setConnected, setRefreshing, refreshToken };
  return <div className="app"><Header view={view} setView={setView} connected={connected} refreshing={refreshing} syncStatus={status} onSync={runSync} onRefresh={() => setRefreshToken(token => token + 1)} />
    {view === 'api-docs' ? <ApiDocs /> : view === 'repairs' ? <RepairsPage {...pageProps} /> : view === 'orders' ? <OrdersPage {...pageProps} /> : <ViewDashboardPage {...pageProps} config={viewConfigs[view]} />}
    <footer><span>Production Fault Gateway · v0.1.0</span><span>MongoDB 数据源</span></footer>
  </div>;
}

const apiGroups = [
  { key: 'health', label: '健康检查' }, { key: 'faults', label: '维修故障' },
  { key: 'orders', label: '销售订单' }, { key: 'views', label: 'HANA 视图' }, { key: 'sync', label: '同步与状态' },
];

function ApiDocs() {
  const [spec, setSpec] = useState(null); const [guide, setGuide] = useState(''); const [query, setQuery] = useState('');
  const [group, setGroup] = useState('all'); const [open, setOpen] = useState({}); const [copied, setCopied] = useState('');
  useEffect(() => { Promise.all([fetch('/api/openapi.json').then(response => response.ok ? response.json() : null).catch(() => null), fetch('/api/agent-guide.md').then(response => response.ok ? response.text() : '').catch(() => '')]).then(([nextSpec, nextGuide]) => { setSpec(nextSpec); setGuide(nextGuide); }); }, []);
  const operations = spec ? Object.entries(spec.paths || {}).flatMap(([path, methods]) => Object.entries(methods).filter(([method]) => ['get', 'post', 'put', 'delete'].includes(method)).map(([method, operation]) => ({ path, method: method.toUpperCase(), operation: resolveOpenApi(operation, spec), tag: operation.tags?.[0] || 'sync' }))) : [];
  const filtered = operations.filter(item => (group === 'all' || item.tag === group) && `${item.path} ${item.operation.summary || ''} ${item.operation.description || ''}`.toLowerCase().includes(query.toLowerCase()));
  const copy = async (text, key) => { try { await navigator.clipboard.writeText(text); setCopied(key); setTimeout(() => setCopied(''), 1500); } catch { /* Clipboard permission may be unavailable. */ } };
  return <main className="api-docs"><section className="hero api-hero"><div><p className="eyebrow">GATEWAY CONTRACT</p><h1>API 文档<em>中心</em></h1><p className="intro">面向开发者与 AI Agent 的统一数据网关契约。只读查询可直接编排，同步操作需人工确认。</p></div><div className="hero-meta"><span>契约版本</span><strong>{spec?.info?.version || '加载中...'}</strong><span>接口数量</span><strong>{operations.length || '-'}</strong></div></section><section className="api-reference"><div><span>基础地址</span><code>{spec?.servers?.[0]?.url || '/api'}</code></div><div><span>机器入口</span><a href="/api/openapi.json" target="_blank" rel="noreferrer">openapi.json <ExternalLink size={13} /></a></div><div><span>Agent 指南</span><a href="/api/agent-guide.md" target="_blank" rel="noreferrer">agent-guide.md <ExternalLink size={13} /></a></div><div><span>通用约定</span><code>pageSize ≤ 100 · 日期 YYYY-MM-DD</code></div></section><section className="api-workspace"><aside className="api-sidebar"><strong>业务分组</strong><button className={group === 'all' ? 'active' : ''} onClick={() => setGroup('all')}>全部接口 <span>{operations.length}</span></button>{apiGroups.map(item => <button key={item.key} className={group === item.key ? 'active' : ''} onClick={() => setGroup(item.key)}>{item.label} <span>{operations.filter(operation => operation.tag === item.key).length}</span></button>)}<div className="agent-note"><strong>Agent 快速参考</strong><p>优先使用稳定键：生产订单、销售订单、SN。分页查询适合交互，`/all` 上限 10,000 条，TSV 流适合导出。</p></div></aside><div className="api-list"><div className="api-toolbar"><div className="search-wrap"><Search size={16} /><input aria-label="搜索接口" placeholder="搜索路径、摘要或说明" value={query} onChange={event => setQuery(event.target.value)} /></div><a className="download-contract" href="/api/openapi.json" download><Download size={15} />下载契约</a></div>{!spec && <div className="table-loading" role="status">正在加载 API 契约</div>}{spec && !filtered.length && <div className="empty"><p>没有匹配的接口</p></div>}{filtered.map((item, index) => <ApiOperation key={`${item.method}-${item.path}`} item={item} index={index} isOpen={!!open[index]} toggle={() => setOpen(value => ({ ...value, [index]: !value[index] }))} copy={copy} copied={copied} />)}</div></section>{guide && <details className="agent-guide"><summary>查看 Agent 任务编排指南</summary><pre>{guide}</pre></details>}</main>;
}

function resolveOpenApi(value, spec) {
  if (!value || typeof value !== 'object') return value;
  if (value.$ref && value.$ref.startsWith('#/')) {
    const resolved = value.$ref.slice(2).split('/').reduce((current, key) => current?.[key.replace(/~1/g, '/').replace(/~0/g, '~')], spec);
    return resolved ? resolveOpenApi(resolved, spec) : value;
  }
  if (Array.isArray(value)) return value.map(item => resolveOpenApi(item, spec));
  return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, resolveOpenApi(child, spec)]));
}

function ApiOperation({ item, index, isOpen, toggle, copy, copied }) {
  const parameters = item.operation.parameters || []; const responses = item.operation.responses || {}; const curl = `curl -sS "${item.path}"`;
  return <article className={`api-operation ${isOpen ? 'expanded' : ''}`}><button className="api-operation-head" onClick={toggle} aria-expanded={isOpen}><span className={`http-method ${item.method.toLowerCase()}`}>{item.method}</span><code>{item.path}</code><span className="api-summary">{item.operation.summary || item.operation.description?.split('。')[0] || item.operation.operationId}</span>{item.path.includes('/sync/') && <mark>需人工确认</mark>}<ChevronDown size={17} /></button>{isOpen && <div className="api-operation-body"><p>{item.operation.description || '网关接口'}</p><div className="api-code"><div><span>示例请求</span><button title="复制 curl" onClick={() => copy(curl, `curl-${index}`)}>{copied === `curl-${index}` ? <Check size={14} /> : <Copy size={14} />}</button></div><pre>{curl}</pre></div>{parameters.length > 0 && <div className="api-params"><h4>参数</h4>{parameters.map((parameter, parameterIndex) => { const schema = parameter.schema || {}; const detail = [schema.type, schema.default !== undefined ? `默认 ${schema.default}` : '', schema.enum?.length ? `可选值：${schema.enum.join(' / ')}` : ''].filter(Boolean).join(' · '); return <div className="api-param" key={parameterIndex}><code>{parameter.name || parameter.$ref || '未命名参数'}</code><span>{parameter.in || 'query'}</span><strong>{parameter.required ? '必填' : '可选'}</strong><small>{[parameter.description, detail].filter(Boolean).join(' · ') || '无额外说明'}</small></div>; })}</div>}<div className="api-responses"><h4>响应</h4>{Object.entries(responses).map(([status, response]) => <div key={status}><b>{status}</b><span>{response.description || 'JSON'}</span></div>)}</div></div>}</article>;
}

if (document.getElementById('root')) createRoot(document.getElementById('root')).render(<App />);
