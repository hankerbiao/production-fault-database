import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DateRangePicker, getMonthRange, getShortcutYears } from './DateRangePicker';

describe('DateRangePicker', () => {
  it('fills a complete month range from the quick month buttons', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-20T12:00:00'));
    const onChange = vi.fn();

    render(<DateRangePicker onChange={onChange} label="订单时间周期" />);
    fireEvent.click(screen.getByRole('button', { name: /订单时间周期/ }));
    fireEvent.click(screen.getByRole('button', { name: '2月' }));

    expect(onChange).toHaveBeenCalledWith('2026-02-01', '2026-02-28');
    vi.useRealTimers();
  });

  it('allows the quick month section to be collapsed and expanded', () => {
    const onChange = vi.fn();
    render(<DateRangePicker onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: /时间周期/ }));

    fireEvent.click(screen.getByRole('button', { name: '收起月份' }));
    expect(screen.getByRole('button', { name: '展开月份' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '1月' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '展开月份' }));
    expect(screen.getByRole('button', { name: '1月' })).toBeInTheDocument();
  });

  it('keeps leap-day month ranges and exposes both years for cross-year ranges', () => {
    expect(getMonthRange(2024, 2)).toEqual(['2024-02-01', '2024-02-29']);
    expect(getShortcutYears('2025-12-01', '2026-01-31')).toEqual([2025, 2026]);
  });

  it('applies a month from the selected year in a cross-year range', () => {
    const onChange = vi.fn();
    render(<DateRangePicker from="2025-12-01" to="2026-01-31" onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: /时间周期/ }));
    fireEvent.click(screen.getByRole('button', { name: '2026 年' }));
    fireEvent.click(screen.getByRole('button', { name: '2月' }));
    expect(onChange).toHaveBeenCalledWith('2026-02-01', '2026-02-28');
  });

  it('uses the year from an end-only range for month shortcuts', () => {
    const onChange = vi.fn();
    render(<DateRangePicker to="2026-08-15" onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: /时间周期/ }));
    expect(screen.getByText('2026 年快捷月份')).toBeInTheDocument();
  });
});
