import { X } from 'lucide-react';

export function ActiveFilters({ filters, labels = {}, initialFilters = {}, onRemove, onClear }) {
  const entries = Object.entries(filters).filter(([key, value]) => value !== '' && value !== undefined && value !== null && value !== initialFilters[key]);
  if (!entries.length) return null;

  return <div className="active-filters" aria-label="已应用筛选条件">
    <span>已应用</span>
    <div>{entries.map(([key, value]) => <button key={key} type="button" onClick={() => onRemove(key)}>
      <b>{labels[key] || key}</b><em>{value === 'true' ? '是' : value}</em><X size={13} />
    </button>)}</div>
    <button className="clear-filters" type="button" onClick={onClear}>清除全部</button>
  </div>;
}
