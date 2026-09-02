import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AlertTriangle, CheckCircle2, ChevronLeft, ChevronRight, ClipboardList, Database, Eye, Filter, PackageSearch, RefreshCw, Search, SlidersHorizontal, UploadCloud, X, Boxes } from 'lucide-react';
import './styles.css';

const emptyFaultStats = { total: 0, withError: 0, withRepairPerson: 0, salesOrders: 0, productionOrders: 0, missingSalesOrder: 0, missingProductionOrder: 0, dataStartDate: '', dataEndDate: '', latestSyncedAt: '' };
const emptyOrderStats = { total: 0, salesOrders: 0, dataStartDate: '', dataEndDate: '', latestSyncedAt: '', sg: 0, kk: 0, orderQuantity: 0, storageQuantity: 0 };
const viewConfigs = {
  zsd124: { id: 'ZSGV_ZSD124', title: '订单 BOM 过账', source: 'SAP HANA 视图 ZSGV_ZSD124', detail: '查看物料过账、订单关联和客户信息。', dateField: 'BUDAT_MKPF', columns: ['MBLNR', 'MJAHR', 'ZEILE', 'MATNR', 'WERKS', 'BWART', 'MENGE_A', 'AUFNR_1', 'VBELN_EX', 'BUDAT_MKPF'], columnLabels: { MBLNR: '物料凭证号', MJAHR: '物料凭证年度', ZEILE: '物料凭证行项目', MATNR: '物料号', WERKS: '工厂', BWART: '移动类型', MENGE_A: '数量', AUFNR_1: '生产订单', VBELN_EX: '销售订单', BUDAT_MKPF: '过账日期' } },
  sernolist: { id: 'ZSGV_ZPP_SERNOLIST', title: '序列号绑定', source: 'SAP HANA 视图 ZSGV_ZPP_SERNOLIST', detail: '查看序列号、生产订单与产品层级绑定关系。', dateField: '', columns: ['ZCODE_HEAD', 'ZCODE_ITEM', 'AUFNR_HEAD', 'AUFNR_ITEM', 'PRODH'] },
  station: { id: 'Z_V_ZMES_T_001', title: '工位记录', source: 'SAP HANA 视图 Z_V_ZMES_T_001（BW_LOCAL.ZTRRI）', detail: '查看生产工位、工序和实际开始结束时间。', dateField: 'ACTUAL_START_TIME', columns: ['HISTROYID', 'PCODE', 'OCODE', 'AUFNR', 'SPEC', 'OPERATION', 'GSTRS', 'ACTUAL_START_TIME', 'ACTUAL_END_TIME'], columnLabels: { HISTROYID: '主键（HISTROYID）', PCODE: '主机序列号（PCODE）', OCODE: '客户序列号（OCODE）', AUFNR: '生产订单（AUFNR）', SPEC: '操作工序（SPEC）', OPERATION: '操作说明（OPERATION）', GSTRS: '计划开始日期（GSTRS）', ACTUAL_START_TIME: '实际开始时间（ACTUAL_START_TIME）', ACTUAL_END_TIME: '实际结束时间（ACTUAL_END_TIME）' } },
};

export function App() {
  const [view, setView] = useState('repairs');
  const [connected, setConnected] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const [syncStatus, setSyncStatus] = useState({ state: 'idle', message: '等待同步' });
  useEffect(() => {
    fetch('/api/sync/status').then(response => response.ok ? response.json() : null).then(status => status && setSyncStatus(status)).catch(() => {});
  }, []);
  useEffect(() => {
    if (syncStatus.state !== 'running') return undefined;
    const timer = setInterval(async () => {
      try {
        const response = await fetch('/api/sync/status');
        if (!response.ok) return;
        const status = await response.json();
        setSyncStatus(status);
        if (status.state !== 'running') setRefreshToken(token => token + 1);
      } catch { /* keep polling while the backend recovers */ }
    }, 2000);
    return () => clearInterval(timer);
  }, [syncStatus.state]);
  async function startSync() {
    try {
      const response = await fetch('/api/sync/incremental', { method: 'POST' });
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 409 && result.status) {
          setSyncStatus(result.status);
          return;
        }
        throw new Error(result.error || '同步启动失败');
      }
      setSyncStatus(result);
    } catch (error) {
      setSyncStatus({ state: 'failed', message: error.message || '同步启动失败' });
    }
  }
  return <div className="app"><Header view={view} setView={setView} connected={connected} refreshing={refreshing} syncStatus={syncStatus} onSync={startSync} onRefresh={() => setRefreshToken(token => token + 1)} />
    {view === 'repairs' ? <Repairs setConnected={setConnected} setRefreshing={setRefreshing} refreshToken={refreshToken} /> : view === 'orders' ? <Orders setConnected={setConnected} setRefreshing={setRefreshing} refreshToken={refreshToken} /> : <ViewDashboard config={viewConfigs[view]} setConnected={setConnected} setRefreshing={setRefreshing} refreshToken={refreshToken} />}
    <footer><span>Production Fault Gateway · v0.1.0</span><span>MongoDB 数据源</span></footer>
  </div>;
}

function Header({ view, setView, connected, refreshing, syncStatus, onSync, onRefresh }) {
  const syncing = syncStatus.state === 'running';
  return <header className="topbar"><div className="brand"><span className="brand-mark"><Database size={18} /></span><span>产线故障数据库</span></div><nav><button className={view === 'repairs' ? 'active' : ''} onClick={() => setView('repairs')}><ClipboardList size={15} />维修故障记录</button><button className={view === 'orders' ? 'active' : ''} onClick={() => setView('orders')}><PackageSearch size={15} />销售订单看板</button><button className={view === 'zsd124' ? 'active' : ''} onClick={() => setView('zsd124')}><Boxes size={15} />订单过账</button><button className={view === 'sernolist' ? 'active' : ''} onClick={() => setView('sernolist')}><Boxes size={15} />序列号绑定</button><button className={view === 'station' ? 'active' : ''} onClick={() => setView('station')}><Boxes size={15} />工位记录</button></nav><div className="connection"><span className={`dot ${connected ? 'online' : ''}`} />{connected ? 'MongoDB 已连接' : 'MongoDB 未连接'}<button className="header-action" onClick={onRefresh} disabled={refreshing} title="刷新当前数据"><RefreshCw size={15} className={refreshing ? 'spin' : ''} />刷新</button><button className="header-action sync-action" onClick={onSync} disabled={syncing} title="立即执行增量同步"><UploadCloud size={15} className={syncing ? 'spin' : ''} />{syncing ? '同步中' : '立即同步'}</button>{syncStatus.state === 'failed' && <span className="sync-error" title={syncStatus.message}>同步失败</span>}</div></header>;
}

function Repairs({ setConnected, setRefreshing, refreshToken }) {
  const [filters, setFilters] = useState({ keyword: '', hostBarcode: '', defectResponsibility: '', ngStation: '', salesOrder: '', productionOrder: '' });
  const [items, setItems] = useState([]), [stats, setStats] = useState(emptyFaultStats), [total, setTotal] = useState(0), [page, setPage] = useState(1), [error, setError] = useState('');
  const [detail, setDetail] = useState(null), [detailLoading, setDetailLoading] = useState(false);
  const pageSize = 20;
  async function load(nextPage = page, nextFilters = filters) {
    setRefreshing(true); const params = new URLSearchParams({ ...nextFilters, page: nextPage, pageSize });
    try {
      const [records, summary] = await Promise.all([fetch(`/api/faults?${params}`), fetch(`/api/faults/stats?${new URLSearchParams(nextFilters)}`)]);
      if (!records.ok || !summary.ok) throw new Error();
      const data = await records.json(); setItems(data.items || []); setTotal(data.total || 0); setStats(await summary.json()); setConnected(true); setError('');
    } catch { setItems([]); setTotal(0); setStats(emptyFaultStats); setConnected(false); setError('无法连接 MongoDB 网关'); }
    finally { setRefreshing(false); }
  }
  useEffect(() => { load(); }, [page, refreshToken]);
  async function openDetail(id) {
    setDetailLoading(true); setDetail(null);
    try { const response = await fetch(`/api/faults/detail?id=${encodeURIComponent(id)}`); if (!response.ok) throw new Error(); setDetail(await response.json()); }
    catch { setDetail({ error: '无法读取该记录的详细字段' }); }
    finally { setDetailLoading(false); }
  }
  function submit(event) { event.preventDefault(); setPage(1); load(1, filters); }
  function reset() { const cleared = { keyword: '', hostBarcode: '', defectResponsibility: '', ngStation: '', salesOrder: '', productionOrder: '' }; setFilters(cleared); setPage(1); load(1, cleared); }
  return <main><Hero title="维修故障记录" detail="逐条查看来源维修数据的完整字段、错误信息和修复记录。" meta={[{ label: '数据来源', value: 'SAP HANA 视图 ZSGV_ZZT_WLJL' }, { label: '数据时间区间', value: formatDateRange(stats.dataStartDate, stats.dataEndDate) }, { label: '最新数据时间', value: formatDate(stats.latestSyncedAt) }]} />
    <section className="stats repair-stats"><Stat icon={<AlertTriangle />} label="维修记录总数" value={stats.total} tone="blue" /><Stat icon={<AlertTriangle />} label="含错误码或错误码描述" value={stats.withError} tone="red" /><Stat icon={<CheckCircle2 />} label="已登记维修人员" value={stats.withRepairPerson} tone="green" /><Stat icon={<ClipboardList />} label="关联销售订单" value={stats.salesOrders} tone="orange" /><Stat icon={<ClipboardList />} label="关联生产订单" value={stats.productionOrders} tone="green" /><Stat icon={<ClipboardList />} label="销售订单为空" value={stats.missingSalesOrder} tone="red" /><Stat icon={<ClipboardList />} label="生产订单为空" value={stats.missingProductionOrder} tone="red" /></section>
    <section className="workspace"><SectionHeader eyebrow="REPAIR RECORDS" title="维修故障明细" total={total} />
      <form className="filters" onSubmit={submit}><SearchInput value={filters.keyword} onChange={value => setFilters({ ...filters, keyword: value })} placeholder="搜索序列号、主机条码、描述或订单号" /><input className="filter-text" placeholder="销售订单 / VBELN" value={filters.salesOrder} onChange={event => setFilters({ ...filters, salesOrder: event.target.value })} /><input className="filter-text" placeholder="生产订单 / AUFNR" value={filters.productionOrder} onChange={event => setFilters({ ...filters, productionOrder: event.target.value })} /><input className="filter-text" placeholder="主机条码 / PCODE" value={filters.hostBarcode} onChange={event => setFilters({ ...filters, hostBarcode: event.target.value })} /><input className="filter-text" placeholder="NG工站 / ZNGGZ" value={filters.ngStation} onChange={event => setFilters({ ...filters, ngStation: event.target.value })} /><input className="filter-text" placeholder="缺陷责任分类 / ZZRFL" value={filters.defectResponsibility} onChange={event => setFilters({ ...filters, defectResponsibility: event.target.value })} /><button className="filter-btn" type="submit"><Filter size={16} />筛选</button><button className="reset-btn" type="button" onClick={reset}>重置</button></form>
      <div className="table-shell"><table className="repairs-table"><thead><tr><th>主机条码</th><th>销售订单</th><th>生产订单</th><th>物料描述</th><th>故障描述</th><th>NG工站</th><th>维修日期时间</th></tr></thead><tbody>{items.map(item => <tr className="record-row" key={item.id} onClick={() => openDetail(item.id)}><td><span className="code">{item.hostBarcode || '-'}</span></td><td><span className="code">{item.salesOrder || '-'}</span></td><td><span className="code">{item.productionOrder || '-'}</span></td><td><strong>{item.materialDescription || '-'}</strong></td><td><strong>{item.faultDescription || item.errorDescription || item.reviewProblem || '-'}</strong></td><td>{item.ngStation || '-'}</td><td>{formatDate(item.repairAt)}</td></tr>)}</tbody></table>{!items.length && <Empty message={error || '没有找到匹配的维修记录'} />}</div>
      <div className="mobile-records">{items.map(item => <RepairCard key={item.id} item={item} openDetail={openDetail} />)}</div>
      <Pagination page={page} total={total} pageSize={pageSize} setPage={setPage} />
    </section>{(detailLoading || detail) && <DetailPanel detail={detail} loading={detailLoading} close={() => { setDetail(null); setDetailLoading(false); }} />}</main>;
}

function Orders({ setConnected, setRefreshing, refreshToken }) {
  const [filters, setFilters] = useState({ keyword: '', source: '' });
  const [items, setItems] = useState([]), [stats, setStats] = useState(emptyOrderStats), [total, setTotal] = useState(0), [page, setPage] = useState(1), [error, setError] = useState('');
  const [detail, setDetail] = useState(null), [detailLoading, setDetailLoading] = useState(false);
  const pageSize = 20;
  async function load(nextPage = page, nextFilters = filters) {
    setRefreshing(true); const params = new URLSearchParams({ ...nextFilters, page: nextPage, pageSize });
    try {
      const [records, summary] = await Promise.all([fetch(`/api/orders?${params}`), fetch(`/api/orders/stats?${new URLSearchParams(nextFilters)}`)]);
      if (!records.ok || !summary.ok) throw new Error();
      const data = await records.json(); setItems(data.items || []); setTotal(data.total || 0); setStats(await summary.json()); setConnected(true); setError('');
    } catch { setItems([]); setTotal(0); setStats(emptyOrderStats); setConnected(false); setError('无法连接 MongoDB 网关'); }
    finally { setRefreshing(false); }
  }
  useEffect(() => { load(); }, [page, refreshToken]);
  async function openDetail(id) {
    setDetailLoading(true); setDetail(null);
    try { const response = await fetch(`/api/orders/detail?id=${encodeURIComponent(id)}`); if (!response.ok) throw new Error(); setDetail(await response.json()); }
    catch { setDetail({ error: '无法读取该订单的详细字段' }); }
    finally { setDetailLoading(false); }
  }
  function submit(event) { event.preventDefault(); setPage(1); load(1, filters); }
  function reset() { const cleared = { keyword: '', source: '' }; setFilters(cleared); setPage(1); load(1, cleared); }
  return <main><Hero title="销售订单看板" detail="查看同步至 MongoDB 的生产订单、客户、物料及数量信息。" meta={[{ label: '数据来源', value: 'SAP HTTP 接口（SG / KK）' }, { label: '数据时间区间', value: formatDateRange(stats.dataStartDate, stats.dataEndDate) }, { label: '最新数据时间', value: formatDate(stats.latestSyncedAt) }]} />
    <section className="stats order-stats"><Stat icon={<ClipboardList />} label="生产订单" value={stats.total} tone="blue" /><Stat icon={<PackageSearch />} label="销售订单" value={stats.salesOrders} tone="green" /><Stat icon={<Database />} label="SG 来源" value={stats.sg} tone="green" /><Stat icon={<Database />} label="KK 来源" value={stats.kk} tone="orange" /><Stat icon={<PackageSearch />} label="订单数量" value={formatNumber(stats.orderQuantity)} tone="red" /><Stat icon={<PackageSearch />} label="入库数量" value={formatNumber(stats.storageQuantity)} tone="blue" /></section>
    <section className="workspace"><SectionHeader eyebrow="SALES ORDERS" title="销售订单明细" total={total} />
      <form className="filters" onSubmit={submit}><SearchInput value={filters.keyword} onChange={value => setFilters({ ...filters, keyword: value })} placeholder="搜索生产订单、销售订单、客户ID、最终用户、物料描述或生产机型" /><select value={filters.source} onChange={event => setFilters({ ...filters, source: event.target.value })}><option value="">全部来源</option><option value="SG">SG</option><option value="KK">KK</option></select><button className="filter-btn" type="submit"><Filter size={16} />筛选</button><button className="reset-btn" type="button" onClick={reset}>重置</button></form>
      <div className="table-shell"><table className="orders-table"><thead><tr><th>SAP来源</th><th>生产订单</th><th>销售订单</th><th>客户ID</th><th>最终用户</th><th>物料描述</th><th>生产机型</th><th>库存地点</th><th>计划开始时间</th><th>订单数量</th><th>入库数量</th><th>订单明细行数</th><th>详情</th></tr></thead><tbody>{items.map(item => <tr className="record-row" key={item.id} onClick={() => openDetail(item.id)}><td><span className={`source-chip ${item.source.toLowerCase()}`}>{item.source}</span></td><td><span className="code">{item.aufnr || '-'}</span></td><td>{item.salesOrder || '-'}</td><td>{item.customerId || '-'}</td><td>{item.finalUser || '-'}</td><td><strong>{item.materialDescription || '-'}</strong></td><td>{item.productionModel || '-'}</td><td>{item.inventoryLocation || '-'}</td><td>{formatBusinessDate(item.plannedStartDate)}</td><td>{formatNumber(item.orderQuantity)}</td><td>{formatNumber(item.storageQuantity)}</td><td>{item.recordCount}</td><td><button className="icon-button" title="查看完整数据库字段" onClick={event => { event.stopPropagation(); openDetail(item.id); }}><Eye size={17} /></button></td></tr>)}</tbody></table>{!items.length && <Empty message={error || '没有找到匹配的销售订单'} />}</div>
      <Pagination page={page} total={total} pageSize={pageSize} setPage={setPage} />
    </section>{(detailLoading || detail) && <OrderDetailPanel detail={detail} loading={detailLoading} close={() => { setDetail(null); setDetailLoading(false); }} />}</main>;
}

function ViewDashboard({ config, setConnected, setRefreshing, refreshToken }) {
  const [filters, setFilters] = useState({ keyword: '', from: '', to: '' });
  const [items, setItems] = useState([]), [stats, setStats] = useState({ total: 0, dataStartDate: '', dataEndDate: '', latestSyncedAt: '' }), [total, setTotal] = useState(0), [page, setPage] = useState(1), [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState(null), [detailLoading, setDetailLoading] = useState(false);
  const pageSize = 20;
  async function load(nextPage = page, nextFilters = filters) {
    setLoading(true); setRefreshing(true);
    const query = new URLSearchParams({ ...nextFilters, page: nextPage, pageSize });
    const statsQuery = new URLSearchParams(nextFilters);
    try {
      const [records, summary] = await Promise.all([fetch(`/api/views/${config.id}?${query}`), fetch(`/api/views/${config.id}/stats?${statsQuery}`)]);
      if (!records.ok || !summary.ok) throw new Error();
      const data = await records.json(); setItems(data.items || []); setTotal(data.total || 0); setStats(await summary.json()); setConnected(true); setError('');
    } catch { setItems([]); setTotal(0); setStats({ total: 0, dataStartDate: '', dataEndDate: '', latestSyncedAt: '' }); setConnected(false); setError('无法连接 MongoDB 网关'); }
    finally { setLoading(false); setRefreshing(false); }
  }
  useEffect(() => { load(); }, [page, refreshToken, config.id]);
  async function openDetail(id) {
    setDetailLoading(true); setDetail(null);
    try { const response = await fetch(`/api/views/${config.id}/detail?id=${encodeURIComponent(id)}`); if (!response.ok) throw new Error(); setDetail(await response.json()); }
    catch { setDetail({ error: '无法读取该记录的详细字段' }); }
    finally { setDetailLoading(false); }
  }
  function submit(event) { event.preventDefault(); setPage(1); load(1, filters); }
  function reset() { const cleared = { keyword: '', from: '', to: '' }; setFilters(cleared); setPage(1); load(1, cleared); }
  return <main><Hero title={config.title} detail={config.detail} meta={[{ label: '数据来源', value: config.source }, { label: '数据时间区间', value: formatDateRange(stats.dataStartDate, stats.dataEndDate) }, { label: '最新数据时间', value: formatDate(stats.latestSyncedAt) }]} />
    <section className="stats order-stats"><Stat icon={<Boxes />} label="记录总数" value={formatNumber(stats.total)} tone="blue" /><Stat icon={<Database />} label="数据起始" value={formatBusinessDate(stats.dataStartDate)} tone="green" /><Stat icon={<RefreshCw />} label="数据截止" value={formatBusinessDate(stats.dataEndDate)} tone="orange" /></section>
    <section className="workspace"><SectionHeader eyebrow={config.id} title={`${config.title}明细`} total={total} />
      <form className="filters" onSubmit={submit}><SearchInput value={filters.keyword} onChange={value => setFilters({ ...filters, keyword: value })} placeholder="搜索视图字段、订单号、条码或物料" />{config.dateField && <><input className="filter-text" type="date" aria-label="开始日期" value={filters.from} onChange={event => setFilters({ ...filters, from: event.target.value })} /><input className="filter-text" type="date" aria-label="结束日期" value={filters.to} onChange={event => setFilters({ ...filters, to: event.target.value })} /></>}<button className="filter-btn" type="submit"><Filter size={16} />筛选</button><button className="reset-btn" type="button" onClick={reset}>重置</button></form>
      <div className="table-shell"><table className="orders-table"><thead><tr>{config.columns.map(column => <th key={column}>{config.columnLabels?.[column] || column}</th>)}<th>详情</th></tr></thead><tbody>{items.map(item => <tr className="record-row" key={item.id} onClick={() => openDetail(item.id)}>{config.columns.map(column => <td key={column}>{formatCell(item[column])}</td>)}<td><button className="icon-button" title="查看完整数据库字段" onClick={event => { event.stopPropagation(); openDetail(item.id); }}><Eye size={17} /></button></td></tr>)}</tbody></table>{loading ? <Loading message={`正在加载${config.title}数据...`} /> : !items.length && <Empty message={error || '没有找到匹配的数据'} />}</div><Pagination page={page} total={total} pageSize={pageSize} setPage={setPage} />
    </section>{(detailLoading || detail) && <ViewDetailPanel detail={detail} loading={detailLoading} close={() => { setDetail(null); setDetailLoading(false); }} />}</main>;
}

function formatCell(value) { if (value === null || value === undefined || value === '') return '-'; if (typeof value === 'object') return JSON.stringify(value); return String(value); }
function ViewDetailPanel({ detail, loading, close }) { return <div className="drawer-backdrop" onClick={close}><aside className="detail-panel" onClick={event => event.stopPropagation()}><div className="detail-head"><div><p className="eyebrow">视图记录详情</p><h2>{loading ? '读取字段中' : '完整数据库字段'}</h2></div><button className="icon-button" title="关闭" onClick={close}><X size={20} /></button></div>{loading ? <div className="detail-loading">正在读取完整字段...</div> : detail?.error ? <Empty message={detail.error} /> : <div className="field-grid">{detail.fields.map(field => <div className="field" key={field.key}><span>{field.label}</span><strong>{field.value || '-'}</strong><small>{field.key}</small></div>)}</div>}</aside></div>; }

function Hero({ title, detail, meta = [{ label: '数据更新时间', value: new Date().toLocaleString('zh-CN', { hour12: false }) }] }) { return <section className="hero"><div><p className="eyebrow">OPERATIONS / MONGODB DATA</p><h1>{title}</h1><p className="intro">{detail}</p></div><div className="hero-meta">{meta.map(item => <div key={item.label}><span>{item.label}</span><strong>{item.value}</strong></div>)}</div></section>; }
function SectionHeader({ eyebrow, title, total }) { return <div className="section-head"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2></div><span className="record-count">共 {total} 条记录</span></div>; }
function SearchInput({ value, onChange, placeholder }) { return <div className="search-wrap"><Search size={18} /><input placeholder={placeholder} value={value} onChange={event => onChange(event.target.value)} /></div>; }
function Stat({ icon, label, value, tone }) { return <div className="stat"><div className={`stat-icon ${tone}`}>{icon}</div><div><span>{label}</span><strong>{value}</strong></div></div>; }
function Empty({ message }) { return <div className="empty"><SlidersHorizontal size={28} /><p>{message}</p></div>; }
function Loading({ message }) { return <div className="table-loading" role="status"><RefreshCw size={25} className="spin" /><p>{message}</p></div>; }
function Pagination({ page, total, pageSize, setPage }) { return <div className="pagination"><span>{total ? `显示 ${(page - 1) * pageSize + 1}-${Math.min(page * pageSize, total)} 条` : '暂无记录'}</span><div><button disabled={page === 1} onClick={() => setPage(page - 1)} title="上一页"><ChevronLeft size={17} /></button><b>{page}</b><button disabled={page * pageSize >= total} onClick={() => setPage(page + 1)} title="下一页"><ChevronRight size={17} /></button></div></div>; }
function DetailPanel({ detail, loading, close }) { return <div className="drawer-backdrop" onClick={close}><aside className="detail-panel" onClick={event => event.stopPropagation()}><div className="detail-head"><div><p className="eyebrow">维修记录详情</p><h2>{loading ? '读取字段中' : detail?.fault?.serialNumber || '维修记录详情'}</h2></div><button className="icon-button" title="关闭" onClick={close}><X size={20} /></button></div>{loading ? <div className="detail-loading">正在读取完整字段...</div> : detail?.error ? <Empty message={detail.error} /> : <><div className="detail-summary"><div><span>主机条码</span><strong>{detail.fault.hostBarcode || '-'}</strong></div><div><span>缺陷责任分类</span><strong>{detail.fault.defectResponsibility || '-'}</strong></div><div><span>维修人员</span><strong>{detail.fault.repairPerson || '-'}</strong></div></div><div className="field-grid">{detail.fields.map(field => <div className="field" key={field.key}><span>{field.label}</span><strong>{field.value || '-'}</strong><small>{field.key}</small></div>)}</div></>}</aside></div>; }
function OrderDetailPanel({ detail, loading, close }) { return <div className="drawer-backdrop" onClick={close}><aside className="detail-panel" onClick={event => event.stopPropagation()}><div className="detail-head"><div><p className="eyebrow">销售订单详情</p><h2>{loading ? '读取字段中' : detail?.order?.aufnr || '销售订单详情'}</h2></div><button className="icon-button" title="关闭" onClick={close}><X size={20} /></button></div>{loading ? <div className="detail-loading">正在读取完整字段...</div> : detail?.error ? <Empty message={detail.error} /> : <><div className="detail-summary"><div><span>销售订单</span><strong>{detail.order.salesOrder || '-'}</strong></div><div><span>订单数量</span><strong>{formatNumber(detail.order.orderQuantity)}</strong></div><div><span>入库数量</span><strong>{formatNumber(detail.order.storageQuantity)}</strong></div></div><div className="field-grid">{detail.fields.map(field => <div className="field" key={field.key}><span>{field.label}</span><strong>{field.value || '-'}</strong><small>{field.key}</small></div>)}</div></>}</aside></div>; }
function RepairCard({ item, openDetail }) { return <article className="repair-card" onClick={() => openDetail(item.id)}><div className="repair-card-head"><div><span className="code">{item.hostBarcode || '-'}</span><small>{formatDate(item.repairAt)}</small></div><button className="icon-button" title="查看完整数据库字段" onClick={event => { event.stopPropagation(); openDetail(item.id); }}><Eye size={17} /></button></div><div className="record-pairs"><RecordPair label="销售订单" value={item.salesOrder} /><RecordPair label="生产订单" value={item.productionOrder} /><RecordPair label="物料描述" value={item.materialDescription} /><RecordPair label="故障描述" value={item.faultDescription || item.errorDescription || item.reviewProblem} /><RecordPair label="NG工站" value={item.ngStation} /></div></article>; }
function RecordPair({ label, value }) { return <div><span>{label}</span><strong>{value || '-'}</strong></div>; }
function formatDate(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-'; }
function formatDateRange(start, end) { return start && end ? `${formatBusinessDate(start)} 至 ${formatBusinessDate(end)}` : '-'; }
function formatBusinessDate(value) { const raw = String(value || '').trim(); return /^\d{8}$/.test(raw) ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6)}` : raw.slice(0, 10) || '-'; }
function formatNumber(value) { return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value || 0); }
if (document.getElementById('root')) createRoot(document.getElementById('root')).render(<App />);
