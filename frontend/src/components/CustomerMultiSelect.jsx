import React, { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';

export function CustomerMultiSelect({ options = [], value = [], onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const selected = new Set(value);

  useEffect(() => {
    const close = event => {
      if (!ref.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, []);

  const toggle = customerID => {
    const next = selected.has(customerID) ? value.filter(item => item !== customerID) : [...value, customerID];
    onChange(next);
  };

  return <div className="customer-select" ref={ref}>
    <button type="button" className="customer-select-trigger" aria-label="客户 ID" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(current => !current)}>
      <span>{value.length ? `已选 ${value.length} 个客户` : '客户 ID（可多选）'}</span>
      <ChevronDown size={15} />
    </button>
    {open && <div className="customer-select-menu" role="listbox" aria-label="客户 ID 选项" aria-multiselectable="true">
      <button type="button" className="customer-select-clear" onClick={() => onChange([])} disabled={!value.length}>清除选择</button>
      {options.map(customerID => <button type="button" role="option" aria-selected={selected.has(customerID)} className={selected.has(customerID) ? 'selected' : ''} key={customerID} onClick={() => toggle(customerID)}>
        <span>{customerID}</span>{selected.has(customerID) && <Check size={14} />}
      </button>)}
      {!options.length && <span className="customer-select-empty">暂无客户 ID</span>}
    </div>}
  </div>;
}
