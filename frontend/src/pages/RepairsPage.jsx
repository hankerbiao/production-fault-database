import React, { useCallback, useState } from 'react';
import { AlertTriangle, CheckCircle2, ClipboardList, Search, SlidersHorizontal } from 'lucide-react';
import { listFaults, faultDetail, faultStats } from '../api/faults';
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
import { repairExportColumns } from '../utils/repairExport';
import { formatBusinessDate, formatDate, formatDateRange, formatNumber } from '../utils/formatters';

const initialFilters = { keyword: '', hostBarcode: '', nonCriticalMaterialSerial: '', defectResponsibility: '', ngStation: '', salesOrder: '', productionOrder: '', productModel: '', dateFrom: '', dateTo: '', timeField: 'planned', company5000: '' };
const initialStats = { total: 0, withError: 0, withRepairPerson: 0, salesOrders: 0, productionOrders: 0, hostBarcodes: 0, missingSalesOrder: 0, missingProductionOrder: 0, dataStartDate: '', dataEndDate: '', latestSyncedAt: '' };
const filterLabels = { productionOrder: '生产订单', salesOrder: '销售订单', nonCriticalMaterialSerial: '非关键件物料序号', productModel: '机型', dateFrom: '开始日期', dateTo: '结束日期', timeField: '时间字段', keyword: '关键字', hostBarcode: '主机条码', ngStation: 'NG工站', defectResponsibility: '责任分类', company5000: '5000公司' };

export function RepairsPage({ modelOptions = [], setConnected, setRefreshing, refreshToken = 0 }) {
  const resource = usePagedResource({ loadPage: useCallback((filters, page, size) => listFaults(filters, page, size), []), loadStats: useCallback(filters => faultStats(filters), []), initialFilters, initialStats, refreshToken, onConnectionChange: setConnected, onRefreshing: setRefreshing });
  const detail = useDetail(faultDetail, '无法读取该记录的详细字段');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const update = (key, value) => resource.setFilters(previous => ({ ...previous, [key]: value }));
  const submit = event => { event.preventDefault(); resource.applyFilters({ ...resource.filters }); };
  const reset = () => resource.reset(initialFilters);
  const removeFilter = key => { const next = { ...resource.appliedFilters, [key]: initialFilters[key] || '' }; resource.applyFilters(next); };
  const columns = [
    { key: 'hostBarcode', label: '主机条码', render: item => <code>{item.hostBarcode || '-'}</code> },
    { key: 'salesOrder', label: '销售订单', render: item => <code>{item.salesOrder || '-'}</code> },
    { key: 'productionOrder', label: '生产订单', render: item => <code>{item.productionOrder || '-'}</code> },
    { key: 'plannedStartDate', label: '计划生产时间', render: item => formatBusinessDate(item.plannedStartDate) },
    { key: 'materialCode', label: '物料号', render: item => <code>{item.materialCode || '-'}</code> },
    { key: 'materialDescription', label: '物料描述', render: item => item.materialDescription || '-' },
    { key: 'faultDescription', label: '故障描述', render: item => item.faultDescription || item.errorDescription || item.reviewProblem || '-' },
    { key: 'ngStation', label: 'NG工站', render: item => item.ngStation || '-' },
    { key: 'repairAt', label: '维修日期时间', render: item => formatDate(item.repairAt) },
  ];
  const exportData = async () => { setExporting(true); try { await downloadCsv({ endpoint: '/api/faults', filters: resource.appliedFilters, filename: '维修故障明细.csv', resolveColumns: repairExportColumns }); } finally { setExporting(false); } };
  const stats = resource.stats;
  return <main>
    <Hero title="维修故障记录" detail="追踪错误信息、维修措施与订单关联。" meta={[{ label: '数据来源', value: 'SAP HANA 视图 ZSGV_ZZT_WLJL' }, { label: '数据区间', value: formatDateRange(stats.dataStartDate, stats.dataEndDate) }, { label: '最新同步', value: formatDate(stats.latestSyncedAt) }]} />
    <Stats className="repair-stats"><Stat icon={<ClipboardList />} label="维修记录总数" value={formatNumber(stats.total)} tone="blue" /><Stat icon={<AlertTriangle />} label="含错误码或错误码描述" value={formatNumber(stats.withError)} tone="red" /><Stat icon={<CheckCircle2 />} label="已登记维修人员" value={formatNumber(stats.withRepairPerson)} tone="green" /><Stat icon={<ClipboardList />} label="销售订单数量（去重）" value={formatNumber(stats.salesOrders)} tone="orange" /><Stat icon={<ClipboardList />} label="生产订单数量（去重）" value={formatNumber(stats.productionOrders)} tone="green" /><Stat icon={<ClipboardList />} label="主机条码数量（去重）" value={formatNumber(stats.hostBarcodes)} tone="blue" /><Stat icon={<AlertTriangle />} label="销售订单为空" value={formatNumber(stats.missingSalesOrder)} tone="red" /><Stat icon={<AlertTriangle />} label="生产订单为空" value={formatNumber(stats.missingProductionOrder)} tone="red" /></Stats>
    <section className="workspace"><div className="section-head"><div><p className="eyebrow">REPAIR RECORDS</p><h2>维修故障明细</h2></div><span className="record-count">共 {formatNumber(resource.total)} 条记录</span></div>
      <form className="filters repair-filters filter-form" aria-label="维修记录搜索条件" onSubmit={submit}>
        <div className="filter-form-body">
          <label className="filter-field"><span>生产订单</span><input className="filter-text" aria-label="生产订单" placeholder="AUFNR" value={resource.filters.productionOrder} onChange={event => update('productionOrder', event.target.value)} /></label>
          <label className="filter-field"><span>销售订单</span><input className="filter-text" aria-label="销售订单" placeholder="VBELN" value={resource.filters.salesOrder} onChange={event => update('salesOrder', event.target.value)} /></label>
          <label className="filter-field"><span>非关键件物料序号</span><input className="filter-text" aria-label="非关键件物料序号" placeholder="RECORD01REPAIRM" value={resource.filters.nonCriticalMaterialSerial} onChange={event => update('nonCriticalMaterialSerial', event.target.value)} /></label>
          <div className="filter-field"><span>机型</span><ModelSelect options={modelOptions} value={resource.filters.productModel} onChange={value => update('productModel', value)} placeholder="机型 / ZJXMC" /></div>
          <div className="filter-field"><span>时间字段</span><div className="time-field-selector" role="group" aria-label="筛选时间字段"><button className={resource.filters.timeField === 'planned' ? 'active' : ''} type="button" onClick={() => update('timeField', 'planned')}>计划生产时间</button><button className={resource.filters.timeField === 'repair' ? 'active' : ''} type="button" onClick={() => update('timeField', 'repair')}>维修时间</button></div></div>
          <div className="filter-field filter-field-compact"><span>5000 公司</span><div className="company-filter-selector" role="group" aria-label="是否为5000公司"><button className={resource.filters.company5000 === '' ? 'active' : ''} type="button" onClick={() => update('company5000', '')}>全部</button><button className={resource.filters.company5000 === 'yes' ? 'active' : ''} type="button" onClick={() => update('company5000', 'yes')}>是</button><button className={resource.filters.company5000 === 'no' ? 'active' : ''} type="button" onClick={() => update('company5000', 'no')}>否</button></div></div>
          <div className="filter-field filter-field-date"><span>{resource.filters.timeField === 'planned' ? '计划生产时间周期' : '维修时间周期'}</span><DateRangePicker from={resource.filters.dateFrom} to={resource.filters.dateTo} onChange={(dateFrom, dateTo) => resource.setFilters(previous => ({ ...previous, dateFrom, dateTo }))} label={resource.filters.timeField === 'planned' ? '计划生产时间周期' : '维修时间周期'} /></div>
        </div>
        <div className="filter-actions"><button className="filter-btn" type="submit" aria-label="搜索 / 筛选" disabled={resource.loading}><Search size={16} />{resource.loading ? '搜索中' : '搜索'}</button><button className="advanced-btn" type="button" onClick={() => setAdvancedOpen(value => !value)} aria-expanded={advancedOpen}><SlidersHorizontal size={16} />高级条件{advancedOpen ? '收起' : '展开'}</button><button className="reset-btn" type="button" onClick={reset} disabled={resource.loading}>重置</button><ExportButton exporting={exporting} onClick={exportData} /></div>
        {advancedOpen && <div className="advanced-filters"><div className="advanced-filters-head"><strong>更多筛选</strong><span>支持关键字、条码、工站和责任分类</span></div><div className="advanced-filters-grid"><input className="filter-text" aria-label="关键字" placeholder="关键字：序列号、描述或订单号" value={resource.filters.keyword} onChange={event => update('keyword', event.target.value)} /><input className="filter-text" aria-label="主机条码" placeholder="主机条码 / PCODE" value={resource.filters.hostBarcode} onChange={event => update('hostBarcode', event.target.value)} /><input className="filter-text" aria-label="NG工站" placeholder="NG工站 / ZNGGZ" value={resource.filters.ngStation} onChange={event => update('ngStation', event.target.value)} /><input className="filter-text" aria-label="缺陷责任分类" placeholder="缺陷责任分类 / ZZRFL" value={resource.filters.defectResponsibility} onChange={event => update('defectResponsibility', event.target.value)} /></div></div>}
      </form>
      <ActiveFilters filters={resource.appliedFilters} labels={filterLabels} initialFilters={initialFilters} onRemove={removeFilter} onClear={reset} />
      <DataTable pageId="repairs" columns={columns} items={resource.items} loading={resource.loading} error={resource.error} onOpen={detail.open} emptyMessage="没有找到匹配的维修记录" />
      <Pagination page={resource.page} total={resource.total} pageSize={20} setPage={resource.setPage} />
    </section>
    {(detail.loading || detail.detail) && <DetailPanel detail={detail.detail} loading={detail.loading} close={detail.close} title="维修记录详情" heading={detail.detail?.fault?.serialNumber || '维修记录详情'} />}
  </main>;
}
