import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DateRangePicker } from './DateRangePicker';

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
});
