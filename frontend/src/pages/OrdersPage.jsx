import React, { useCallback, useState } from 'react';
import { Boxes, ClipboardList, Filter, PackageSearch, SlidersHorizontal } from 'lucide-react';
import { listOrders, orderDetail, orderStats } from '../api/orders';
import { usePagedResource } from '../hooks/usePagedResource';
import { useDetail } from '../hooks/useDetail';
import { ActiveFilters } from '../components/ActiveFilters';
import { DataTable } from '../components/DataTable';
import { DateRangePicker } from '../components/DateRangePicker';
import { DetailPanel } from '../components/DetailPanel';
import { ExportButton } from '../components/ExportButton';
import { Hero } from '../components/Hero';
import { ModelSelect } from '../components/ModelSelect';
import { Pagination } from '../components/Pagination';
import { Stat, Stats } from '../components/Stats';
import { downloadCsv } from '../utils/export';
import { formatBusinessDate, formatDate, formatDateRange, formatNumber } from '../utils/formatters';

const initialFilters = { keyword: '', source: '', productionOrder: '', salesOrder: '', customer: '', productModel: '', dateFrom: '', dateTo: '' };
const initialStats = { total: 0, salesOrders: 0, dataStartDate: '', dataEndDate: '', latestSyncedAt: '', sg: 0, kk: 0, orderQuantity: 0, machineQuantity: 0, storageQuantity: 0 };
const filterLabels = { source: '来源', productionOrder: '生产订单', salesOrder: '销售订单', customer: '客户', productModel: '机型', dateFrom: '开始日期', dateTo: '结束日期', keyword: '关键字' };

export function OrdersPage({ modelOptions = [], setConnected, setRefreshing, refreshToken = 0 }) {
  const resource = usePagedResource({ loadPage: useCallback((filters, page, size) => listOrders(filters, page, size), []), loadStats: useCallback(filters => orderStats(filters), []), initialFilters, initialStats, refreshToken, onConnectionChange: setConnected, onRefreshing: setRefreshing });
  const detail = useDetail(orderDetail, '无法读取该记录的详细字段');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const update = (key, value) => resource.setFilters(previous => ({ ...previous, [key]: value }));
  const submit = event => { event.preventDefault(); resource.applyFilters(resource.filters); };
  const reset = () => resource.reset(initialFilters);
  const removeFilter = key => resource.applyFilters({ ...resource.filters, [key]: initialFilters[key] || '' });
  const columns = [
    { key: 'source', label: '来源', render: item => <span className={`source-pill ${String(item.source || '').toLowerCase()}`}>{item.source || '-'}</span> },
    { key: 'aufnr', label: '生产订单', render: item => <code>{item.aufnr || '-'}</code> }, { key: 'salesOrder', label: '销售订单', render: item => <code>{item.salesOrder || '-'}</code> },
    { key: 'customer', label: '客户', render: item => item.finalUser || item.customerId || '-' }, { key: 'materialDescription', label: '物料描述', render: item => item.materialDescription || '-' },
    { key: 'productionModel', label: '生产机型', render: item => item.productionModel || '-' }, { key: 'plannedStartDate', label: '计划开始日期', render: item => formatBusinessDate(item.plannedStartDate), exportValue: item => formatBusinessDate(item.plannedStartDate) },
    { key: 'orderQuantity', label: '订单数量', render: item => formatNumber(item.orderQuantity) }, { key: 'storageQuantity', label: '入库数量', render: item => formatNumber(item.storageQuantity) },
  ];
  const exportData = async () => { setExporting(true); try { await downloadCsv({ endpoint: '/api/orders', filters: resource.filters, filename: '销售订单明细.csv', columns: columns.map(column => ({ key: column.key, label: column.label, value: column.exportValue })) }); } finally { setExporting(false); } };
  const stats = resource.stats;
  return <main>
    <Hero title="销售订单看板" detail="集中查看订单、生产计划和数量状态。" meta={[{ label: '数据来源', value: 'SAP 销售订单同步' }, { label: '数据区间', value: formatDateRange(stats.dataStartDate, stats.dataEndDate) }, { label: '最新同步', value: formatDate(stats.latestSyncedAt) }]} />
    <Stats className="order-stats"><Stat icon={<Boxes />} label="订单记录" value={formatNumber(stats.total)} tone="blue" /><Stat icon={<ClipboardList />} label="销售订单（去重）" value={formatNumber(stats.salesOrders)} tone="orange" /><Stat icon={<PackageSearch />} label="SG 记录" value={formatNumber(stats.sg)} tone="green" /><Stat icon={<PackageSearch />} label="KK 记录" value={formatNumber(stats.kk)} tone="red" /><Stat icon={<ClipboardList />} label="订单数量" value={formatNumber(stats.orderQuantity)} tone="blue" /><Stat icon={<ClipboardList />} label="入库数量" value={formatNumber(stats.storageQuantity)} tone="orange" /></Stats>
    <section className="workspace"><div className="section-head"><div><p className="eyebrow">SALES ORDERS</p><h2>订单明细</h2></div><div className="section-head-meta"><span className="record-count">共 {formatNumber(resource.total)} 条</span><span className="record-summary">机器数量汇总：{formatNumber(stats.machineQuantity)}</span></div></div>
      <form className="filters order-filters" onSubmit={submit}><select aria-label="订单来源" value={resource.filters.source} onChange={event => update('source', event.target.value)}><option value="">全部来源</option><option value="SG">SG</option><option value="KK">KK</option></select><input className="filter-text" placeholder="生产订单" value={resource.filters.productionOrder} onChange={event => update('productionOrder', event.target.value)} /><input className="filter-text" placeholder="销售订单" value={resource.filters.salesOrder} onChange={event => update('salesOrder', event.target.value)} /><input className="filter-text" placeholder="客户 ID / 最终用户" aria-label="客户 ID / 最终用户" value={resource.filters.customer} onChange={event => update('customer', event.target.value)} /><ModelSelect options={modelOptions} value={resource.filters.productModel} onChange={value => update('productModel', value)} placeholder="机型 / Product model" /><DateRangePicker from={resource.filters.dateFrom} to={resource.filters.dateTo} onChange={(dateFrom, dateTo) => resource.setFilters(previous => ({ ...previous, dateFrom, dateTo }))} label="订单时间周期" /><button className="filter-btn" type="submit"><Filter size={16} />筛选</button><button className="advanced-btn" type="button" onClick={() => setAdvancedOpen(value => !value)} aria-expanded={advancedOpen}><SlidersHorizontal size={16} />高级条件{advancedOpen ? '收起' : '展开'}</button><button className="reset-btn" type="button" onClick={reset}>重置</button><ExportButton exporting={exporting} onClick={exportData} />{advancedOpen && <div className="advanced-filters"><input className="filter-text" placeholder="关键字" value={resource.filters.keyword} onChange={event => update('keyword', event.target.value)} /></div>}</form>
      <ActiveFilters filters={resource.filters} labels={filterLabels} initialFilters={initialFilters} onRemove={removeFilter} onClear={reset} />
      <DataTable pageId="orders" columns={columns} items={resource.items} loading={resource.loading} error={resource.error} onOpen={detail.open} />
      <Pagination page={resource.page} total={resource.total} pageSize={20} setPage={resource.setPage} />
    </section>
    {(detail.loading || detail.detail) && <DetailPanel detail={detail.detail} loading={detail.loading} close={detail.close} title="销售订单详情" heading={detail.detail?.order?.aufnr || '销售订单详情'} />}
  </main>;
}
