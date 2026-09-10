import { useCallback, useState } from 'react';
import { BookOpen, Boxes, ClipboardList, Database, ListChecks, Menu, PackageSearch, PanelLeftClose, RefreshCw, Settings2, UploadCloud, X } from 'lucide-react';
import { ServiceConfigPanel } from './ServiceConfigPanel';
const links = [
  ['repairs', '维修故障记录', ClipboardList], ['orders', '销售订单看板', PackageSearch], ['zsd124', '订单过账', Boxes],
  ['sernolist', '序列号绑定 / Serial Number Binding', Boxes], ['station', '工位记录', Boxes], ['scsDoa', 'SCS DOA 申报', ClipboardList],
  ['scsChange', 'SCS 换上换下', Boxes], ['sync-management', '同步管理', ListChecks], ['api-docs', 'API 文档', BookOpen],
];

export function Header({ view, setView, connected, refreshing, syncStatus, onSync, onRefresh }) {
  const [configOpen, setConfigOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const closeConfig = useCallback(() => setConfigOpen(false), []);
  const syncing = syncStatus?.state === 'running';
  const navigate = key => { setView(key); setMenuOpen(false); };
  return <>
    <aside className={`app-sidebar ${collapsed ? 'is-collapsed' : ''} ${menuOpen ? 'is-open' : ''}`} aria-label="主导航">
      <div className="sidebar-brand"><span className="brand-mark"><Database size={18} /></span><strong>产线故障数据库</strong><button className="sidebar-collapse" type="button" title="收起导航" onClick={() => setCollapsed(value => !value)}><PanelLeftClose size={16} /></button><button className="sidebar-close" type="button" title="关闭导航" onClick={() => setMenuOpen(false)}><X size={18} /></button></div>
      <nav>{links.map(([key, label, Icon]) => <button key={key} className={view === key ? 'active' : ''} onClick={() => navigate(key)} title={collapsed ? label : undefined}><Icon size={17} /><span>{label}</span></button>)}</nav>
      <div className="sidebar-foot"><span className={`connection-dot ${connected ? 'online' : ''}`} /><span>{connected ? 'MongoDB 已连接' : 'MongoDB 未连接'}</span></div>
    </aside>
    {menuOpen && <button className="sidebar-scrim" type="button" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />}
    <header className="app-topbar"><button className="mobile-menu" type="button" aria-label="打开导航" onClick={() => setMenuOpen(true)}><Menu size={20} /></button><div className="topbar-title"><span>{links.find(link => link[0] === view)?.[1]}</span></div><div className="topbar-actions"><span className={`connection-status ${connected ? 'online' : ''}`}>{connected ? '已连接' : '未连接'}</span><button className="utility-button" onClick={onRefresh} disabled={refreshing} title="刷新当前数据"><RefreshCw size={16} className={refreshing ? 'spin' : ''} /><span>刷新</span></button><button className="sync-button" onClick={onSync} disabled={syncing} title="立即执行增量同步"><UploadCloud size={16} className={syncing ? 'spin' : ''} /><span>{syncing ? '同步中' : '立即同步'}</span></button><button className="utility-button icon-only" onClick={() => setConfigOpen(true)} title="查看服务配置" aria-label="查看服务配置"><Settings2 size={17} /></button>{syncStatus?.state === 'failed' && <span className="sync-error" title={syncStatus.message}>同步失败</span>}</div></header>
    <ServiceConfigPanel open={configOpen} onClose={closeConfig} />
  </>;
}
