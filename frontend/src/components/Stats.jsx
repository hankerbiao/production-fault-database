export function Stats({ children, className = '' }) { return <section className={`stats ${className}`}>{children}</section>; }
export function Stat({ icon, label, value, tone }) { return <div className="stat"><div className={`stat-icon ${tone}`}>{icon}</div><div><span>{label}</span><strong>{value}</strong></div></div>; }
