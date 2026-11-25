import { useEffect, useRef, useState } from 'react';
import { Eye, Search } from 'lucide-react';
import { downloadKKDeliveries, getKKDeliveryDetail, listKKDeliveries } from '../api/kkDeliveries';
import { DateRangePicker } from '../components/DateRangePicker';
import { DetailPanel } from '../components/DetailPanel';
import { Hero } from '../components/Hero';
import { Pagination } from '../components/Pagination';
import { formatBusinessDate, formatNumber } from '../utils/formatters';

const emptyFilters = { dateFrom: '', dateTo: '', salesOrder: '', materialNumber: '', customerNumber: '', plant: '', sn: '' };
const canExport = filters => {
  if (!filters.dateFrom || !filters.dateTo) return false;
  const from = Date.parse(`${filters.dateFrom}T00:00:00Z`);
  const to = Date.parse(`${filters.dateTo}T00:00:00Z`);
  return Number.isFinite(from) && Number.isFinite(to) && to >= from && to - from <= 30 * 24 * 60 * 60 * 1000;
};

export function KKDeliveriesPage({ setConnected, setRefreshing, refreshToken } = {}) {
  const [filters, setFilters] = useState(emptyFilters);
  const [appliedFilters, setAppliedFilters] = useState(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [collectionEmpty, setCollectionEmpty] = useState(false);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');
  const [error, setError] = useState('');
  const [selectedRecord, setSelectedRecord] = useState(null);
  const [detail, setDetail] = useState(null);
  const initialRefresh = useRef(true);
  const started = appliedFilters !== null;

  useEffect(() => {
    if (!selectedRecord) return;
    const controller = new AbortController();
    getKKDeliveryDetail(selectedRecord.id, controller.signal)
      .then(record => {
        if (controller.signal.aborted) return;
        const fields = Object.entries(record).map(([key, value]) => ({
          key,
          label: key,
          value: typeof value === 'string' && value !== '' ? value : JSON.stringify(value),
        }));
        setDetail({ fields, raw: record });
      })
      .catch(error => {
        if (!controller.signal.aborted) setDetail({ error: error.message || '无法读取交付详情' });
      });
    return () => controller.abort();
  }, [selectedRecord]);

  const openDetail = item => {
    setDetail(null);
    setSelectedRecord({ id: item.id, order: item.VBELN });
  };

  useEffect(() => {
    if (!appliedFilters) return undefined;
    let active = true;
    setLoading(true);
    setRefreshing?.(true);
    setError('');
    listKKDeliveries(appliedFilters, page, pageSize)
      .then(result => {
        if (!active) return;
        setItems(Array.isArray(result?.items) ? result.items : []);
        setTotal(Number.isFinite(result?.total) ? result.total : 0);
        setCollectionEmpty(Boolean(result?.collectionEmpty));
        setConnected?.(true);
      })
      .catch(requestError => {
        if (!active) return;
        setItems([]);
        setTotal(0);
        setCollectionEmpty(false);
        setError(requestError?.message || '查询 KK 交付数据失败');
        setConnected?.(false);
      })
      .finally(() => {
        if (!active) return;
        setLoading(false);
        setRefreshing?.(false);
      });
    return () => { active = false; };
  }, [appliedFilters, page, pageSize]);

  useEffect(() => {
    if (initialRefresh.current) {
      initialRefresh.current = false;
      return;
    }
    if (!appliedFilters) return;
    setAppliedFilters(current => current ? { ...current } : current);
    setPage(1);
  }, [refreshToken]);

  const update = (key, value) => setFilters(current => ({ ...current, [key]: value }));
  const search = event => {
    event.preventDefault();
    setAppliedFilters({ ...filters });
    setPage(1);
  };
  const reset = event => {
    event.preventDefault();
    event.stopPropagation();
    setFilters(emptyFilters);
    setAppliedFilters(null);
    setPage(1);
    setItems([]);
    setTotal(0);
    setCollectionEmpty(false);
    setError('');
    setExportError('');
  };

  const exportAll = async () => {
    if (!canExport(appliedFilters) || exporting) return;
    setExportError('');
    setExporting(true);
    try {
      await downloadKKDeliveries(appliedFilters);
      setConnected?.(true);
    } catch (exportRequestError) {
      setExportError(exportRequestError?.message || '导出失败');
      setConnected?.(false);
    } finally {
      setExporting(false);
    }
  };

  let emptyTitle = '筛选无结果';
  let emptyDetail = '调整筛选条件后重新查询。';
  if (collectionEmpty) {
    emptyTitle = '集合暂时无数据';
    emptyDetail = '同步数据尚未回填到集合，完成回填后即可查询。';
  }

  return <main>
    <Hero title="KK 销售订单交付明细" detail="按 SN 去重，每个 SN 显示 DATUM 最新的一条记录；SN 为空的记录分别保留。订单号、物料号、客户号和设备序列号按字符串展示，保留前导零。" meta={[{ label: '数据来源', value: 'BW_LOCAL.SD / ZSGV_ZSD46N_SO_KK' }, { label: 'MongoDB 集合', value: 'sales_order_deliveries_kk_sap' }, ...(started ? [{ label: '命中记录', value: loading ? '查询中' : formatNumber(total) }] : [])]} />
    <section className="workspace kk-deliveries-workspace">
      <div className="section-head"><div><p className="eyebrow">KK SALES ORDER DELIVERY</p><h2>交付记录</h2></div>{started && <span className="record-count">{loading ? '正在查询' : `共 ${formatNumber(total)} 条命中`}</span>}</div>
      <form className="filters filter-form" aria-label="KK 交付记录筛选条件" onSubmit={search}>
        <div className="filter-form-body">
          <div className="filter-field filter-field-date">
            <span>交付日期</span>
            <DateRangePicker
              label="交付日期"
              from={filters.dateFrom}
              to={filters.dateTo}
              onChange={(dateFrom, dateTo) => setFilters(current => ({ ...current, dateFrom, dateTo }))}
            />
          </div>
          <label className="filter-field"><span>销售订单号</span><input className="filter-text" aria-label="销售订单号" inputMode="numeric" placeholder="VBELN" value={filters.salesOrder} onChange={event => update('salesOrder', event.target.value)} /></label>
          <label className="filter-field"><span>物料号</span><input className="filter-text" aria-label="物料号" placeholder="MATNR" value={filters.materialNumber} onChange={event => update('materialNumber', event.target.value)} /></label>
          <label className="filter-field"><span>客户号</span><input className="filter-text" aria-label="客户号" inputMode="numeric" placeholder="KUNNR" value={filters.customerNumber} onChange={event => update('customerNumber', event.target.value)} /></label>
          <label className="filter-field"><span>工厂</span><input className="filter-text" aria-label="工厂" placeholder="WERKS" value={filters.plant} onChange={event => update('plant', event.target.value)} /></label>
          <label className="filter-field"><span>SN</span><input className="filter-text" aria-label="SN" placeholder="精确匹配 SN" value={filters.sn} onChange={event => update('sn', event.target.value)} /></label>
        </div>
        <div className="filter-actions"><button className="filter-btn" type="submit" disabled={loading}><Search size={16} />{loading ? '查询中' : '查询'}</button><button className="reset-btn" type="button" onClick={reset} disabled={loading}>重置</button></div>
      </form>

      <section className="record-table kk-deliveries-table" aria-label="KK 销售订单交付明细结果">
        {started && <div className="table-toolbar"><span>{loading ? '正在更新结果' : `显示 ${items.length} 条当前结果`}</span>{!loading && !error && <>
          <button className="export-btn" type="button" disabled={!canExport(appliedFilters) || exporting} title="导出需设置完整的交付日期范围，最多 31 天（含首尾）" onClick={exportAll}>{exporting ? '导出中' : '导出全部结果'}</button>
          {!canExport(appliedFilters) && <span>导出需设置完整的交付日期范围，最多 31 天（含首尾）</span>}
          {exportError && <span role="alert">{exportError}</span>}
        </>}</div>}
        <div className="table-scroll" tabIndex="0" aria-label="交付记录表格，可横向滚动">
          <table><thead><tr><th>销售订单号</th><th>物料号</th><th>交付日期</th><th>数据时间（DATUM）</th><th>客户号</th><th>工厂</th><th>设备序列号（SERNR）</th><th>SN</th><th>详情</th></tr></thead>
            <tbody>{items.map((item, index) => <tr className={item.id ? 'record-row' : undefined} key={item.id ?? index} onClick={item.id ? () => openDetail(item) : undefined}>
              <td><code>{item.VBELN || '-'}</code></td><td><code>{item.MATNR || '-'}</code></td><td>{formatBusinessDate(item.WADAT)}</td><td>{formatBusinessDate(item.DATUM)}</td><td><code>{item.KUNNR || '-'}</code></td><td><code>{item.WERKS || '-'}</code></td><td><code>{item.SERNR || '—'}</code></td><td><code>{item.SN || '—'}</code></td>
              <td><button className="icon-button" type="button" title="查看原始数据" aria-label="查看原始数据" disabled={!item.id} onClick={event => { event.stopPropagation(); openDetail(item); }}><Eye size={16} /></button></td>
            </tr>)}</tbody>
          </table>
          {loading && <div className="table-loading" role="status" aria-live="polite"><span className="loading-spinner" aria-hidden="true" /><p>正在读取 KK 交付记录</p></div>}
          {!loading && error && <div className="empty-state" role="alert"><strong>查询失败</strong><p>{error}</p></div>}
          {!started && <div className="empty-state"><strong>设置条件后查询</strong><p>选择交付日期范围或填写筛选条件，然后点击查询查看交付记录。</p></div>}
          {started && !loading && !error && !items.length && <div className="empty-state"><strong>{emptyTitle}</strong><p>{emptyDetail}</p></div>}
        </div>
      </section>
      {started && !loading && !error && <Pagination page={page} total={total} pageSize={pageSize} setPage={setPage} setPageSize={value => { setPageSize(Number(value)); setPage(1); }} />}
    </section>
    {selectedRecord && <DetailPanel
      title="KK 交付原始数据"
      heading={detail?.fields ? `${selectedRecord.order || '完整数据库字段'} · ${detail.fields.length} 个字段` : selectedRecord.order || '完整数据库字段'}
      detail={detail}
      rawData={detail?.raw}
      defaultView="raw"
      loading={!detail}
      close={() => setSelectedRecord(null)}
    />}
  </main>;
}
