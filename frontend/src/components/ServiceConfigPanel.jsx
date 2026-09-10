import { useEffect, useRef, useState } from 'react';
import { Check, CheckCircle2, CircleAlert, Copy, Database, Layers3, Server, ShieldCheck, X } from 'lucide-react';
import { serviceConfig } from '../api/config';

function ConfigRow({ label, value, onCopy, copied }) {
  const copyValue = typeof value === 'string' ? value : '';
  return <div className="config-row"><div><span>{label}</span>{copyValue && <button type="button" title={`复制${label}`} onClick={() => onCopy(copyValue, label)}>{copied === label ? <Check size={13} /> : <Copy size={13} />}</button>}</div><strong>{value || '未设置'}</strong></div>;
}

export function ServiceConfigPanel({ open, onClose }) {
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState('');
  const closeRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    closeRef.current?.focus();
    document.body.classList.add('drawer-open');
    setLoading(true);
    setError('');
    serviceConfig().then(setConfig).catch(() => setError('无法读取当前服务配置')).finally(() => setLoading(false));
    const closeOnEscape = event => { if (event.key === 'Escape') onClose(); };
    document.addEventListener('keydown', closeOnEscape);
    return () => { document.removeEventListener('keydown', closeOnEscape); document.body.classList.remove('drawer-open'); };
  }, [open, onClose]);

  if (!open) return null;
  const database = config?.database;
  const copy = async (value, label) => { try { await navigator.clipboard.writeText(value); setCopied(label); setTimeout(() => setCopied(''), 1400); } catch { /* Clipboard may be unavailable. */ } };
  return <div className="config-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <aside className="config-panel" role="dialog" aria-modal="true" aria-labelledby="service-config-title">
      <div className="config-panel-head"><div><p className="eyebrow">RUNTIME DETAILS</p><h2 id="service-config-title">服务配置</h2></div><button className="icon-button" ref={closeRef} title="关闭服务配置" aria-label="关闭服务配置" onClick={onClose}><X size={18} /></button></div>
      {loading && <div className="config-state" role="status">正在读取服务配置</div>}
      {!loading && error && <div className="config-state config-error"><CircleAlert size={17} />{error}</div>}
      {!loading && !error && config && <div className="config-panel-body">
        <section className="config-section"><h3><Server size={17} />服务</h3><div className="config-grid"><ConfigRow label="服务名称" value={config.serviceName} onCopy={copy} copied={copied} /><ConfigRow label="版本" value={config.version} onCopy={copy} copied={copied} /><ConfigRow label="运行环境" value={config.environment} onCopy={copy} copied={copied} /><ConfigRow label="运行时" value={config.runtime} onCopy={copy} copied={copied} /><ConfigRow label="监听地址" value={config.listenAddress} onCopy={copy} copied={copied} /><ConfigRow label="前端地址" value={config.frontendURL} onCopy={copy} copied={copied} /><ConfigRow label="API 根路径" value={config.apiBasePath} onCopy={copy} copied={copied} /></div></section>
        <section className="config-section"><h3><Database size={17} />MongoDB</h3><div className="config-grid"><ConfigRow label="连接地址" value={database?.address} onCopy={copy} copied={copied} /><ConfigRow label="数据库" value={database?.name} onCopy={copy} copied={copied} /><ConfigRow label="认证源" value={database?.authSource} onCopy={copy} copied={copied} /><ConfigRow label="副本集" value={database?.replicaSet} onCopy={copy} copied={copied} /><ConfigRow label="认证凭据" value={database?.authenticationConfigured ? '已配置（已隐藏）' : '未配置'} onCopy={copy} copied={copied} /><ConfigRow label="TLS" value={database?.tls ? '已启用' : '未启用'} onCopy={copy} copied={copied} /><ConfigRow label="连接状态" value={<span className={`config-status ${database?.status === '已连接' ? 'is-online' : 'is-offline'}`}><i />{database?.status || '未知'}</span>} onCopy={copy} copied={copied} /></div><p className="config-note"><ShieldCheck size={14} />完整连接 URI、用户名和密码不会在前端展示。</p></section>
        <section className="config-section"><h3><Layers3 size={17} />数据集合</h3><div className="collection-list">{config.collections?.map(item => <div className="collection-item" key={`${item.label}-${item.name}`}><span>{item.label}</span><code>{item.name}</code></div>)}</div></section>
        <section className="config-section"><h3><CheckCircle2 size={17} />服务能力</h3><div className="feature-list">{config.features?.map(item => <span className={item.enabled ? 'feature-enabled' : 'feature-disabled'} key={item.label}><i />{item.label}</span>)}</div></section>
      </div>}
    </aside>
  </div>;
}
