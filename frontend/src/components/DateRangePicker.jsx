import React, { useEffect, useState } from 'react';
import { CalendarDays, ChevronDown, ChevronUp } from 'lucide-react';

const months = Array.from({ length: 12 }, (_, index) => index + 1);

const short = value => String(value || '').replace(/^(\d{4})-(\d{2})-(\d{2})$/, '$1/$2/$3');

const localDate = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;

const dateYear = value => {
  const match = String(value || '').match(/^(\d{4})-/);
  return match ? Number(match[1]) : null;
};

export const getShortcutYears = (from, to, currentYear = new Date().getFullYear()) => {
  const years = [dateYear(from), dateYear(to)].filter(year => Number.isInteger(year) && year > 0);
  return years.length ? [...new Set(years)] : [currentYear];
};

export const getMonthRange = (year, month) => {
  const start = new Date(year, month - 1, 1);
  const end = new Date(year, month, 0);
  return [localDate(start), localDate(end)];
};

const getInitialYear = (from, to) => getShortcutYears(from, to)[0];

function DateRangeDisplay({ from, to, bilingual }) {
  if (from && to) return `${short(from)} 至 ${short(to)}`;
  if (from) return `${short(from)} 起`;
  if (to) return `截至 ${short(to)}`;
  return bilingual ? '不限 / Any time' : '不限';
}

function MonthShortcuts({ from, to, year, onYearChange, onMonthChange }) {
  const [monthsOpen, setMonthsOpen] = useState(true);
  const years = getShortcutYears(from, to);

  useEffect(() => {
    if (!years.includes(year)) onYearChange(years[0]);
  }, [onYearChange, year, years]);

  const isSelectedMonth = month => {
    const [monthStart, monthEnd] = getMonthRange(year, month);
    return from === monthStart && to === monthEnd;
  };

  return (
    <section className="date-range-months" aria-label="快捷月份">
      <div className="date-range-months-head">
        <strong>{year} 年快捷月份</strong>
        <button type="button" aria-expanded={monthsOpen} onClick={() => setMonthsOpen(value => !value)}>
          {monthsOpen ? '收起月份' : '展开月份'}
          {monthsOpen ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
        </button>
      </div>
      {years.length > 1 && (
        <div className="date-range-year-switcher" aria-label="快捷月份年份">
          {years.map(option => (
            <button key={option} type="button" aria-pressed={option === year} onClick={() => onYearChange(option)}>
              {option} 年
            </button>
          ))}
        </div>
      )}
      {monthsOpen && (
        <div className="date-range-month-grid">
          {months.map(month => (
            <button key={month} type="button" aria-pressed={isSelectedMonth(month)} className={isSelectedMonth(month) ? 'selected' : ''} onClick={() => onMonthChange(getMonthRange(year, month))}>
              {month}月
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

export function DateRangePicker({ from = '', to = '', onChange, label = '时间周期', bilingual = false }) {
  const [open, setOpen] = useState(false);
  const [shortcutYear, setShortcutYear] = useState(() => getInitialYear(from, to));

  const applyRecent = days => {
    const end = new Date();
    const start = new Date();
    start.setDate(end.getDate() - days);
    onChange(localDate(start), localDate(end));
  };

  return (
    <div className="date-range-picker">
      <button className={`date-range-trigger${from || to ? ' has-value' : ''}`} type="button" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(value => !value)}>
        <CalendarDays size={16} />
        <span><small>{label}</small><strong><DateRangeDisplay from={from} to={to} bilingual={bilingual} /></strong></span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="date-range-panel" role="dialog" aria-label={label}>
          <div className="date-range-presets">
            <button type="button" onClick={() => applyRecent(6)}>最近 7 天</button>
            <button type="button" onClick={() => applyRecent(29)}>最近 30 天</button>
          </div>
          <MonthShortcuts from={from} to={to} year={shortcutYear} onYearChange={setShortcutYear} onMonthChange={([monthFrom, monthTo]) => onChange(monthFrom, monthTo)} />
          <div className="date-range-fields">
            <label><span>开始日期</span><input type="date" aria-label="开始日期" value={from} onChange={event => onChange(event.target.value, to)} /></label>
            <span>至</span>
            <label><span>结束日期</span><input type="date" aria-label="结束日期" value={to} onChange={event => onChange(from, event.target.value)} /></label>
          </div>
          <div className="date-range-footer">
            <button type="button" onClick={() => onChange('', '')}>清除</button>
            <button type="button" onClick={() => setOpen(false)}>完成</button>
          </div>
        </div>
      )}
    </div>
  );
}
