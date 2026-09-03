import React, { useCallback, useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, ClipboardList, Eye, Filter, SlidersHorizontal } from 'lucide-react';
import { listFaults, faultStats, faultDetail } from '../api/faults';
import { usePagedResource } from '../hooks/usePagedResource';
import { useDetail } from '../hooks/useDetail';
import { DateRangePicker } from '../components/DateRangePicker';
import { DetailPanel } from '../components/DetailPanel';
import { ExportButton } from '../components/ExportButton';
import { Hero } from '../components/Hero';
import { ModelSelect } from '../components/ModelSelect';
import { Pagination } from '../components/Pagination';
import { Stat, Stats } from '../components/Stats';
import { downloadCsv } from '../utils/export';
import { formatBusinessDate, formatDate, formatDateRange } from '../utils/formatters';

const initialFilters = { keyword: '', hostBarcode: '', defectResponsibility: '', ngStation: '', salesOrder: '', productionOrder: '', productModel: '', dateFrom: '', dateTo: '', timeField: 'planned' };
const initialStats = { total: 0, withError: 0, withRepairPerson: 0, salesOrders: 0, productionOrders: 0, hostBarcodes: 0, missingSalesOrder: 0, missingProductionOrder: 0, dataStartDate: '', dataEndDate: '', latestSyncedAt: '' };

export function RepairsPage({ modelOptions = [], setConnected, setRefreshing, refreshToken = 0 }) {
  const loadPage = useCallback((filters, page, size) => listFaults(filters, page, size), []);
  const loadStats = useCallback(filters => faultStats(filters), []);
  const resource = usePagedResource({ loadPage, loadStats, initialFilters, initialStats, refreshToken, onConnectionChange: setConnected, onRefreshing: setRefreshing });
  const detail = useDetail(faultDetail, '无法读取该记录的详细字段');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const update = (key, value) => resource.setFilters(prev => ({ ...prev, [key]: value }));
  const submit = event => { event.preventDefault(); resource.setPage(1); resource.load(1, resource.filters); };
  const reset = () => resource.reset(initialFilters);
  const exportData = async () => {
    setExporting(true);
    try { await downloadCsv({ endpoint: '/api/faults', filters: resource.filters, filename: '维修故障明细.csv', columns: [
      { key: 'hostBarcode', label: '主机条码' }, { key: 'salesOrder', label: '销售订单' }, { key: 'productionOrder', label: '生产订单' },
      { key: 'plannedStartDate', label: '计划生产时间', value: item => formatBusinessDate(item.plannedStartDate) }, { key: 'materialDescription', label: '物料描述' },
      { key: 'faultDescription', label: '故障描述', value: item => item.faultDescription || item.errorDescription || item.reviewProblem }, { key: 'ngStation', label: 'NG工站' }, { key: 'repairAt', label: '维修日期时间', value: item => formatDate(item.repairAt) },
    ] }); } catch { resource.setFilters(prev => prev); } finally { setExporting(false); }
  };
  const stats = resource.stats;
  return <main><Hero title="维修故障记录" detail="逐条查看来源维修数据的完整字段、错误信息和修复记录。" meta={[{ label: '数据来源', value: 'SAP HANA 视图 ZSGV_ZZT_WLJL' }, { label: '数据时间区间', value: formatDateRange(stats.dataStartDate, stats.dataEndDate) }, { label: '最新数据时间', value: formatDate(stats.latestSyncedAt) }]} />
    <Stats className="repair-stats"><Stat icon={<AlertTriangle />} label="维修记录总数" value={stats.total} tone="blue" /><Stat icon={<AlertTriangle />} label="含错误码或错误码描述" value={stats.withError} tone="red" /><Stat icon={<CheckCircle2 />} label="已登记维修人员" value={stats.withRepairPerson} tone="green" /><Stat icon={<ClipboardList />} label="销售订单数量（去重）" value={stats.salesOrders} tone="orange" /><Stat icon={<ClipboardList />} label="生产订单数量（去重）" value={stats.productionOrders} tone="green" /><Stat icon={<ClipboardList />} label="主机条码数量（去重）" value={stats.hostBarcodes} tone="blue" /><Stat icon={<ClipboardList />} label="销售订单为空" value={stats.missingSalesOrder} tone="red" /><Stat icon={<ClipboardList />} label="生产订单为空" value={stats.missingProductionOrder} tone="red" /></Stats>
    <section className="workspace"><div className="section-head"><div><p className="eyebrow">REPAIR RECORDS</p><h2>维修故障明细</h2></div><span className="record-count">共 {resource.total} 条记录</span></div>
      <form className="filters repair-filters" onSubmit={submit}><input className="filter-text" placeholder="生产订单 / AUFNR" value={resource.filters.productionOrder} onChange={e => update('productionOrder', e.target.value)} /><input className="filter-text" placeholder="销售订单 / VBELN" value={resource.filters.salesOrder} onChange={e => update('salesOrder', e.target.value)} /><ModelSelect options={modelOptions} value={resource.filters.productModel} onChange={value => update('productModel', value)} placeholder="机型 / ZJXMC" />
        <div className="time-field-selector" role="group" aria-label="筛选时间字段"><button className={resource.filters.timeField === 'planned' ? 'active' : ''} type="button" aria-pressed={resource.filters.timeField === 'planned'} onClick={() => update('timeField', 'planned')}>计划生产时间</button><button className={resource.filters.timeField === 'repair' ? 'active' : ''} type="button" aria-pressed={resource.filters.timeField === 'repair'} onClick={() => update('timeField', 'repair')}>维修时间</button></div>
        <DateRangePicker from={resource.filters.dateFrom} to={resource.filters.dateTo} onChange={(from, to) => resource.setFilters(prev => ({ ...prev, dateFrom: from, dateTo: to }))} label={resource.filters.timeField === 'planned' ? '计划生产时间周期' : '维修时间周期'} /><button className="filter-btn" type="submit"><Filter size={16} />筛选</button><button className="advanced-btn" type="button" onClick={() => setAdvancedOpen(value => !value)} aria-expanded={advancedOpen}><SlidersHorizontal size={16} />高级条件{advancedOpen ? '收起' : '展开'}</button><button className="reset-btn" type="button" onClick={reset}>重置</button><ExportButton exporting={exporting} onClick={exportData} />
        {advancedOpen && <div className="advanced-filters"><input className="filter-text" placeholder="关键字：序列号、描述或订单号" value={resource.filters.keyword} onChange={e => update('keyword', e.target.value)} /><input className="filter-text" placeholder="主机条码 / PCODE" value={resource.filters.hostBarcode} onChange={e => update('hostBarcode', e.target.value)} /><input className="filter-text" placeholder="NG工站 / ZNGGZ" value={resource.filters.ngStation} onChange={e => update('ngStation', e.target.value)} /><input className="filter-text" placeholder="缺陷责任分类 / ZZRFL" value={resource.filters.defectResponsibility} onChange={e => update('defectResponsibility', e.target.value)} /></div>}
      </form><div className="table-shell"><table className="repairs-table"><thead><tr>{['主机条码', '销售订单', '生产订单', '计划生产时间', '物料描述', '故障描述', 'NG工站', '维修日期时间', '详情'].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{resource.items.map(item => <tr className="record-row" key={item.id} onClick={() => detail.open(item.id)}><td><span className="code">{item.hostBarcode || '-'}</span></td><td><span className="code">{item.salesOrder || '-'}</span></td><td><span className="code">{item.productionOrder || '-'}</span></td><td>{formatBusinessDate(item.plannedStartDate)}</td><td>{item.materialDescription || '-'}</td><td>{item.faultDescription || item.errorDescription || item.reviewProblem || '-'}</td><td>{item.ngStation || '-'}</td><td>{formatDate(item.repairAt)}</td><td><button className="icon-button" title="查看完整数据库字段" onClick={e => { e.stopPropagation(); detail.open(item.id); }}><Eye size={17} /></button></td></tr>)}</tbody></table>{resource.loading ? <div className="table-loading" role="status">正在加载维修故障记录数据</div> : !resource.items.length && <div className="empty"><p>{resource.error || '没有找到匹配的维修记录'}</p></div>}</div><Pagination page={resource.page} total={resource.total} pageSize={20} setPage={resource.setPage} /></section>{(detail.loading || detail.detail) && <DetailPanel detail={detail.detail} loading={detail.loading} close={detail.close} title="维修记录详情" heading={detail.detail?.fault?.serialNumber || '维修记录详情'} />}</main>;
}
