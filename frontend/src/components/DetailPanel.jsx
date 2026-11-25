import { Copy, Check, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

export function DetailPanel({ detail, loading, close, title = '记录详情', heading = '完整数据库字段', fieldDescriptions = {}, rawData, defaultView = 'fields' }) {
  const closeRef = useRef(null);
  const [copied, setCopied] = useState('');
  const [view, setView] = useState(defaultView);
  useEffect(() => { closeRef.current?.focus(); }, []);
  useEffect(() => { const onKeyDown = event => { if (event.key === 'Escape') close(); }; document.addEventListener('keydown', onKeyDown); document.body.classList.add('drawer-open'); return () => { document.removeEventListener('keydown', onKeyDown); document.body.classList.remove('drawer-open'); }; }, [close]);
  const copy = async (value, key) => { try { await navigator.clipboard.writeText(String(value || '')); setCopied(key); setTimeout(() => setCopied(''), 1400); } catch { /* Clipboard may be unavailable. */ } };
  const hasRawData = rawData !== undefined && rawData !== null;
  return <div className="drawer-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) close(); }}>
    <aside className="detail-panel" role="dialog" aria-modal="true" aria-label={title}>
      <div className="detail-head">
        <div><p className="eyebrow">{title}</p><h2>{loading ? '读取字段中' : heading}</h2></div>
        <button className="icon-button" ref={closeRef} title="关闭" onClick={close}><X size={20} /></button>
      </div>
      {loading ? <div className="detail-loading" role="status">正在读取完整字段...</div> : detail?.error ? <div className="empty-state"><strong>无法读取详情</strong><p>{detail.error}</p></div> : <>
        {hasRawData && <div className="detail-view-switch" role="tablist" aria-label="详情显示方式">
          <button type="button" role="tab" aria-selected={view === 'raw'} className={view === 'raw' ? 'active' : ''} onClick={() => setView('raw')}>完整原始数据</button>
          <button type="button" role="tab" aria-selected={view === 'fields'} className={view === 'fields' ? 'active' : ''} onClick={() => setView('fields')}>字段列表（{detail?.fields?.length || 0}）</button>
          {view === 'raw' && <button type="button" className="detail-copy-all" onClick={() => copy(JSON.stringify(rawData, null, 2), '__all__')}>{copied === '__all__' ? <Check size={14} /> : <Copy size={14} />}{copied === '__all__' ? '已复制' : '复制全部 JSON'}</button>}
        </div>}
        {hasRawData && view === 'raw' ? <pre className="detail-raw-json">{JSON.stringify(rawData, null, 2)}</pre> : <div className="field-grid">{(detail?.fields || []).map(field => <div className="field" key={field.key}><div><span>{field.label}</span><button type="button" title="复制字段值" onClick={() => copy(field.value, field.key)}>{copied === field.key ? <Check size={14} /> : <Copy size={14} />}</button></div><strong>{field.value || '-'}</strong><small>{field.key}</small>{fieldDescriptions[field.key] && <small>{fieldDescriptions[field.key]}</small>}</div>)}</div>}
      </>}
    </aside>
  </div>;
}
