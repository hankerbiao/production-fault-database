import { useState } from 'react';
import { Search } from 'lucide-react';
import { post } from '../api/client';

const sources = ['KK', 'SG'];
const fields = [['AUFNR', '生产订单'], ['VBELN', '销售订单'], ['MATNR', '物料编码'], ['GSTRS', '计划开始日期'], ['PRODH', '产品线']];

function SourceResult({ source, state }) {
  const [page, setPage] = useState(1);
  const items = state?.items || [];
  const current = Math.min(page, Math.max(1, Math.ceil(items.length / 20)));
  return <section className="sap-result" aria-label={`${source} 查询结果`}>
    <div className="sap-result-head"><div><span className={`source-pill ${source.toLowerCase()}`}>{source}</span><h3>{source} 来源</h3><span className="sap-source-detail">SAP 客户端 {source === 'KK' ? '600' : '800'} · 生产订单与销售订单信息</span></div><span>{state?.loading ? '正在查询…' : state?.error ? '查询失败' : state ? `匹配 ${items.length} 条 / 接口返回 ${state.fetched} 条` : '等待查询'}</span></div>
    {state?.error && <p className="sap-error" role="alert">{state.error}</p>}
    {state && !state.loading && !state.error && (items.length ? <><div className="sap-table-wrap"><table><thead><tr>{fields.map(([key, label]) => <th key={key}>{label}<small>{key}</small></th>)}<th>原始数据</th></tr></thead><tbody>{items.slice((current - 1) * 20, current * 20).map((row, index) => <tr key={`${current}-${index}`}>{fields.map(([key]) => <td key={key}>{row[key] == null || row[key] === '' ? '—' : String(row[key])}</td>)}<td><details><summary>查看字段</summary><pre>{JSON.stringify(row, null, 2)}</pre></details></td></tr>)}</tbody></table></div><div className="sap-pages"><button type="button" disabled={current <= 1} onClick={() => setPage(current - 1)}>上一页</button><span>第 {current} / {Math.max(1, Math.ceil(items.length / 20))} 页</span><button type="button" disabled={current * 20 >= items.length} onClick={() => setPage(current + 1)}>下一页</button></div></> : <p className="sap-empty">该来源没有匹配记录。</p>)}
  </section>;
}

export function SAPQueryPage() {
  const [form, setForm] = useState({ start: '', end: '', order: '' });
  const [results, setResults] = useState({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const update = key => event => setForm(previous => ({ ...previous, [key]: event.target.value }));
  async function submit(event) {
    event.preventDefault();
    const days = (Date.parse(`${form.end}T00:00:00Z`) - Date.parse(`${form.start}T00:00:00Z`)) / 86400000;
    if (!Number.isFinite(days) || days < 0 || days > 30) { setError('请选择有效的日期范围，最多 31 天。'); return; }
    setError(''); setLoading(true); setResults({ KK: { loading: true }, SG: { loading: true } });
    await Promise.all(sources.map(async source => {
      try { const result = await post('/api/sap/query', { ...form, source }); setResults(previous => ({ ...previous, [source]: result })); }
      catch (err) { setResults(previous => ({ ...previous, [source]: { error: err.message || '查询失败' } })); }
    }));
    setLoading(false);
  }
  return <main>
    <section className="hero"><div><p className="eyebrow">OPERATIONS / SAP LIVE QUERY</p><h1>SAP 数据查询</h1><p className="intro">实时查询生产订单及其销售订单明细，分别查看两个 SAP 来源的返回结果。</p></div><div className="hero-meta"><div><span>查询范围</span><strong>产品线 00100</strong></div><div><span>接口</span><strong>ZSIMS_CL_INBOUND_MO_PSINFO</strong></div></div></section>
    <section className="workspace"><div className="section-head"><div><p className="eyebrow">QUERY PARAMETERS</p><h2>查询条件</h2></div></div>
      <form className="filter-form" onSubmit={submit}>
        <div className="filter-form-body">
          <label className="filter-field"><span>变更开始日期 · CHDAT</span><input className="filter-text" type="date" required value={form.start} onChange={update('start')} /></label>
          <label className="filter-field"><span>变更结束日期 · CHDAT_TO</span><input className="filter-text" type="date" required value={form.end} onChange={update('end')} /></label>
          <label className="filter-field"><span>产品线 · PRODH_LIST</span><input className="filter-text" value="00100" readOnly /></label>
          <label className="filter-field"><span>生产订单 · AUFNR（可选）</span><input className="filter-text" placeholder="例如 30222644" value={form.order} onChange={update('order')} maxLength={40} /></label>
        </div><p className="sap-hint">日期筛选的是 SAP 变更日期，包含起止日期，最多 31 天；生产订单号在接口返回后精确筛选。不填写订单号时显示全部返回记录。</p>
        <div className="filter-actions"><button className="filter-btn" type="submit" disabled={loading}><Search size={16} />{loading ? '查询中…' : '查询 KK 和 SG'}</button></div>
        {error && <p className="sap-error" role="alert">{error}</p>}
      </form>
      <div className="section-head sap-results-title"><div><p className="eyebrow">LIVE RESULTS</p><h2>接口返回结果</h2></div><span className="record-count">来源分别查询，单个来源失败不会影响另一个来源</span></div>
      <div className="sap-results">{sources.map(source => <SourceResult key={source} source={source} state={results[source]} />)}</div>
    </section>
  </main>;
}
