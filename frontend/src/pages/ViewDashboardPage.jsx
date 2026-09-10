import React, { useCallback, useState } from 'react';
import { AlertTriangle, Boxes, ClipboardList, Filter, SlidersHorizontal } from 'lucide-react';
import { listView, viewDetail, viewStats } from '../api/views';
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
import { formatCell, formatDate, formatDateRange, formatNumber } from '../utils/formatters';

const emptyStats = { total: 0, salesOrders: 0, productionOrders: 0, missingSalesOrder: 0, missingProductionOrder: 0, missingProductionOrderDistinct: 0, dataStartDate: '', dataEndDate: '', latestSyncedAt: '' };
const initialFilters = { keyword: '', productionOrder: '', salesOrder: '', productModel: '', from: '', to: '', dateFrom: '', dateTo: '', stationCode: '', sn: '', base: '', materialCode: '', headOrder: '', itemOrder: '', doaCode: '', status: '', judgment: '', customer: '', serviceOrder: '', changeType: '', partNumber: '', partSn: '', operator: '', needReturn: '', revoked: '', company5000: '' };
const filterLabels = { keyword: '关键字', productionOrder: '生产订单', salesOrder: '销售订单', productModel: '机型', from: '开始时间', to: '结束时间', stationCode: '工位', sn: '序列号', base: '基地', materialCode: '物料号', headOrder: '机头订单', itemOrder: '小刀订单', doaCode: 'DOA单号', status: '处理状态', judgment: '判定结果', customer: '客户', serviceOrder: '服务单', changeType: '操作类型', partNumber: '备件PN', partSn: '备件SN', operator: '操作人', needReturn: '需归还', revoked: '已撤销', company5000: '5000公司' };
const hasActiveFilters = filters => Object.values(filters).some(value => value !== '' && value !== false && value !== undefined && value !== null);

export function ViewDashboardPage({ config, modelOptions = [], setConnected, setRefreshing, refreshToken = 0 }) {
  const isStationPreview = filters => config.id === 'Z_V_ZMES_T_001' && !hasActiveFilters(filters);
  const resource = usePagedResource({ loadPage: useCallback((filters, page, size) => listView(config.id, filters, page, size, isStationPreview(filters)), [config.id]), loadStats: useCallback(filters => isStationPreview(filters) ? Promise.resolve(emptyStats) : viewStats(config.id, filters), [config.id]), initialFilters, initialStats: emptyStats, refreshToken, onConnectionChange: setConnected, onRefreshing: setRefreshing });
  const detail = useDetail(id => viewDetail(config.id, id), '无法读取该记录的详细字段');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const bilingual = config.bilingual;
  const update = (key, value) => resource.setFilters(previous => ({ ...previous, [key]: value }));
  const submit = event => { event.preventDefault(); resource.applyFilters(resource.filters); };
  const reset = () => resource.reset(initialFilters);
  const removeFilter = key => resource.applyFilters({ ...resource.filters, [key]: initialFilters[key] || '' });
  const columns = config.columns.map(key => ({
    key,
    label: config.columnLabels?.[key] || key,
    className: config.columnClasses?.[key],
    render: item => {
      const value = formatCell(item[key]);
      if (!config.compactStatusColumns?.includes(key) || !['是', '否'].includes(value)) return value;
      return <span className={`result-pill ${value === '是' ? 'is-yes' : 'is-no'}`}>{value}</span>;
    },
  }));
  const exportData = async (extraFilters = resource.filters, filename = `${config.title}.csv`) => { setExporting(true); try { await downloadCsv({ endpoint: `/api/views/${config.id}`, filters: extraFilters, filename, columns: columns.map(column => ({ key: column.key, label: column.label })) }); } finally { setExporting(false); } };
  const stats = resource.stats;
  return <main>
    <Hero title={config.title} detail={config.detail} meta={[{ label: bilingual ? '数据来源 / Data source' : '数据来源', value: config.source }, { label: bilingual ? '数据区间 / Data period' : '数据区间', value: formatDateRange(stats.dataStartDate, stats.dataEndDate) }, { label: bilingual ? '最新同步 / Latest sync' : '最新同步', value: formatDate(stats.latestSyncedAt) }]} />
    {!resource.preview && <Stats className="view-stats"><Stat icon={<Boxes />} label={bilingual ? '记录总数 / Total records' : '记录总数'} value={formatNumber(stats.total)} tone="blue" />{(config.stats || []).map(item => <Stat key={item.key} icon={item.tone === 'red' || item.tone === 'orange' ? <AlertTriangle /> : <ClipboardList />} label={item.label} value={formatNumber(stats[item.key])} tone={item.tone} />)}</Stats>}
    <section className="workspace"><div className="section-head"><div><p className="eyebrow">{config.id}</p><h2>{bilingual ? `${config.title} 明细 / Details` : `${config.title}明细`}</h2></div><span className="record-count">{resource.preview ? `当前页 ${resource.items.length} 条` : `共 ${formatNumber(resource.total)} 条记录`}</span></div>
      <form className="filters dashboard-filters" onSubmit={submit}>{config.showProductionOrder && <input className="filter-text" placeholder="生产订单 / Production order" value={resource.filters.productionOrder} onChange={event => update('productionOrder', event.target.value)} />}{config.showSalesOrder && <input className="filter-text" placeholder="销售订单 / Sales order" value={resource.filters.salesOrder} onChange={event => update('salesOrder', event.target.value)} />}{config.showProductModel && <ModelSelect options={modelOptions} value={resource.filters.productModel} onChange={value => update('productModel', value)} placeholder={bilingual ? '产品层次 / Product hierarchy' : '机型 / Product model'} />}{config.dateField && <DateRangePicker from={resource.filters.from} to={resource.filters.to} onChange={(from, to) => resource.setFilters(previous => ({ ...previous, from, to }))} label={bilingual ? '时间周期 / Date period' : '时间周期'} bilingual={bilingual} />}<button className="filter-btn" type="submit"><Filter size={16} />{bilingual ? '筛选 / Filter' : '筛选'}</button><button className="advanced-btn" type="button" onClick={() => setAdvancedOpen(value => !value)} aria-expanded={advancedOpen}><SlidersHorizontal size={16} />{bilingual ? `高级条件${advancedOpen ? '收起' : '展开'} / More` : `高级条件${advancedOpen ? '收起' : '展开'}`}</button><button className="reset-btn" type="button" onClick={reset}>{bilingual ? '重置 / Reset' : '重置'}</button><ExportButton exporting={exporting} onClick={() => exportData()} bilingual={bilingual} />{config.allowMissingSalesOrderExport && <ExportButton exporting={exporting} onClick={() => exportData({ ...resource.filters, missingSalesOrder: true }, '订单BOM过账-销售订单为空.csv')} label="导出销售订单为空" title="导出销售订单为空的 BOM 数据" />}{advancedOpen && <div className="advanced-filters"><input className="filter-text" placeholder={bilingual ? '关键字 / Keyword' : '关键字'} value={resource.filters.keyword} onChange={event => update('keyword', event.target.value)} />{config.advancedFields?.map(field => field.type === 'select' ? <select key={field.key} aria-label={field.label} value={resource.filters[field.key] || ''} onChange={event => update(field.key, event.target.value)}><option value="">{field.placeholder}</option>{field.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select> : field.type === 'checkbox' ? <label className="filter-checkbox" key={field.key}><input type="checkbox" checked={resource.filters[field.key] === 'true'} onChange={event => update(field.key, event.target.checked ? 'true' : '')} /><span>{field.label}</span></label> : <input key={field.key} className="filter-text" placeholder={field.placeholder} value={resource.filters[field.key] || ''} onChange={event => update(field.key, event.target.value)} />)}</div>}</form>
      <ActiveFilters filters={resource.filters} labels={filterLabels} initialFilters={initialFilters} onRemove={removeFilter} onClear={reset} />
      <DataTable pageId={config.id} columns={columns} items={resource.items} loading={resource.loading} error={resource.error} onOpen={detail.open} loadingMessage={`正在加载${config.title}数据`} bilingual={bilingual} />
      <Pagination page={resource.page} total={resource.total} pageSize={20} setPage={resource.setPage} bilingual={bilingual} preview={resource.preview} hasMore={resource.hasMore} />
    </section>
    {(detail.loading || detail.detail) && <DetailPanel detail={detail.detail} loading={detail.loading} close={detail.close} title={bilingual ? '视图记录详情 / View Record Details' : '视图记录详情'} heading={bilingual ? '完整数据库字段 / All database fields' : '完整数据库字段'} />}
  </main>;
}
