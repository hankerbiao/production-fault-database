import { Check, Columns3, Eye, Rows3 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';

const preferenceKey = pageId => `fault-workbench-table:${pageId}`;

function readPreferences(pageId, columns) {
  try {
    const value = JSON.parse(window.localStorage.getItem(preferenceKey(pageId)) || '{}');
    const available = new Set(columns.map(column => column.key));
    return {
      density: value.density === 'compact' ? 'compact' : 'comfortable',
      hidden: Array.isArray(value.hidden) ? value.hidden.filter(key => available.has(key)) : [],
    };
  } catch { return { density: 'comfortable', hidden: [] }; }
}

export function DataTable({ pageId, columns, items, loading, error, onOpen, emptyMessage = '没有找到匹配的数据', loadingMessage = '正在加载数据', bilingual = false }) {
  const initial = useMemo(() => readPreferences(pageId, columns), [pageId, columns]);
  const [density, setDensity] = useState(initial.density);
  const [hidden, setHidden] = useState(initial.hidden);
  const [columnsOpen, setColumnsOpen] = useState(false);
  const menuRef = useRef(null);
  const visibleColumns = columns.filter(column => !hidden.includes(column.key));

  useEffect(() => {
    try { window.localStorage.setItem(preferenceKey(pageId), JSON.stringify({ density, hidden })); } catch { /* Storage may be unavailable. */ }
  }, [pageId, density, hidden]);
  useEffect(() => {
    const close = event => { if (!menuRef.current?.contains(event.target)) setColumnsOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, []);

  const toggleColumn = key => setHidden(current => current.includes(key) ? current.filter(item => item !== key) : [...current, key]);
  return <section className={`record-table ${pageId === 'ZSGV_ZSD124' ? 'bom-table-shell' : ''} ${density === 'compact' ? 'is-compact' : ''}`}>
    <div className="table-toolbar">
      <span>{loading ? '正在更新结果' : `显示 ${items.length} 条当前结果`}</span>
      <div className="table-toolbar-actions">
        <button type="button" className={density === 'compact' ? 'active' : ''} onClick={() => setDensity(value => value === 'compact' ? 'comfortable' : 'compact')} title="切换表格密度" aria-pressed={density === 'compact'}><Rows3 size={15} />{density === 'compact' ? '紧凑' : '舒适'}</button>
        <div className="column-menu" ref={menuRef}>
          <button type="button" onClick={() => setColumnsOpen(value => !value)} aria-expanded={columnsOpen} title="选择显示列"><Columns3 size={15} />列</button>
          {columnsOpen && <div className="column-menu-panel" role="menu" aria-label="选择显示列">{columns.map(column => <label key={column.key}><input type="checkbox" checked={!hidden.includes(column.key)} onChange={() => toggleColumn(column.key)} />{column.label}</label>)}</div>}
        </div>
      </div>
    </div>
    <div className="table-scroll" tabIndex="0" aria-label="记录表格，可横向滚动">
      <table><thead><tr>{visibleColumns.map(column => <th className={column.className} key={column.key}>{column.label}</th>)}<th>{bilingual ? '详情 / Details' : '详情'}</th></tr></thead>
        <tbody>{items.map(item => <tr className="record-row" key={item.id} onClick={() => onOpen(item.id)}>{visibleColumns.map(column => <td className={column.className} key={column.key}>{column.render(item)}</td>)}<td><button className="icon-button" title={bilingual ? '查看完整数据库字段 / View all database fields' : '查看完整数据库字段'} onClick={event => { event.stopPropagation(); onOpen(item.id); }}><Eye size={16} /></button></td></tr>)}</tbody>
      </table>
      {loading && <div className="table-loading" role="status" aria-live="polite"><span className="loading-spinner" aria-hidden="true" /><span className="loading-bars" aria-hidden="true"><i /><i /><i /><i /></span><span className="loading-grid" aria-hidden="true"><i /><i /><i /><i /><i /><i /><i /><i /><i /></span><p>{loadingMessage}</p></div>}
      {!loading && !items.length && <div className="empty-state"><strong>{error ? '暂时无法读取数据' : '没有匹配结果'}</strong><p>{error || emptyMessage}</p></div>}
    </div>
    {!loading && items.length > 0 && <div className="mobile-record-list">{items.map(item => <button className="mobile-record" type="button" key={item.id} onClick={() => onOpen(item.id)}>{visibleColumns.slice(0, 4).map((column, index) => <span key={column.key} className={index === 0 ? 'mobile-primary' : ''}><small>{column.label}</small><b>{column.render(item)}</b></span>)}<Eye size={16} /></button>)}</div>}
  </section>;
}

export function DetailCopyButton({ copied, onClick }) {
  return <button className="field-copy" type="button" title="复制字段值" onClick={onClick}>{copied ? <Check size={14} /> : '复制'}</button>;
}
