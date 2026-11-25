import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, Search } from 'lucide-react';
export function ModelSelect({ options = [], value = '', onChange, placeholder = '机型', toggleLabel = '展开机型选项', onOpen }) {
  const [open, setOpen] = useState(false); const [query, setQuery] = useState(value); const ref = useRef(null);
  const show = () => { if (!open) onOpen?.(); setOpen(true); };
  useEffect(() => setQuery(value), [value]);
  useEffect(() => { const close = event => { if (!ref.current?.contains(event.target)) setOpen(false); }; document.addEventListener('mousedown', close); return () => document.removeEventListener('mousedown', close); }, []);
  const filtered = options.filter(item => item.toLowerCase().includes(query.trim().toLowerCase())).slice(0, 50);
  return <div className="model-select" ref={ref}><div className="model-select-input"><Search size={16} /><input aria-label={placeholder} placeholder={placeholder} value={query} onFocus={show} onChange={event => { setQuery(event.target.value); onChange(event.target.value); setOpen(true); }} /><button type="button" className="model-select-toggle" aria-label={toggleLabel} onClick={() => open ? setOpen(false) : show()}><ChevronDown size={15} /></button></div>{open && <div className="model-select-menu" role="listbox">{filtered.map(item => <button type="button" role="option" key={item} onClick={() => { onChange(item); setQuery(item); setOpen(false); }}>{item}</button>)}</div>}</div>;
}
