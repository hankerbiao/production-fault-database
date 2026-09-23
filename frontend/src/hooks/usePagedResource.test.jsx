import { renderHook, waitFor } from '@testing-library/react';
import { act } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { usePagedResource } from './usePagedResource';

describe('usePagedResource pagination', () => {
  it('keeps a fixed page size and avoids reloading stats when changing pages', async () => {
    const loadPage = vi.fn(async (_filters, page, pageSize) => ({ items: [{ page }], total: 41, page, pageSize }));
    const loadStats = vi.fn(async () => ({ total: 41 }));
    const { result } = renderHook(() => usePagedResource({ loadPage, loadStats }));

    await waitFor(() => expect(loadPage).toHaveBeenCalledTimes(1));
    expect(loadPage).toHaveBeenLastCalledWith({}, 1, 20);
    expect(loadStats).toHaveBeenCalledTimes(1);

    act(() => result.current.setPage(2));
    await waitFor(() => expect(loadPage).toHaveBeenCalledTimes(2));
    expect(loadPage).toHaveBeenLastCalledWith({}, 2, 20);
    expect(loadStats).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(result.current.items).toEqual([{ page: 2 }]));
  });

  it('keeps draft filters separate until search is submitted', async () => {
    const loadPage = vi.fn(async (filters) => ({ items: [], total: filters.keyword ? 1 : 0 }));
    const loadStats = vi.fn(async () => ({ total: 0 }));
    const { result } = renderHook(() => usePagedResource({ loadPage, loadStats, initialFilters: { keyword: '' } }));

    await waitFor(() => expect(loadPage).toHaveBeenCalledTimes(1));
    act(() => result.current.setFilters({ keyword: 'AUFNR-1' }));
    expect(result.current.appliedFilters).toEqual({ keyword: '' });
    expect(loadPage).toHaveBeenCalledTimes(1);

    await act(async () => { await result.current.applyFilters({ keyword: 'AUFNR-1' }); });
    expect(result.current.appliedFilters).toEqual({ keyword: 'AUFNR-1' });
    expect(loadPage).toHaveBeenLastCalledWith({ keyword: 'AUFNR-1' }, 1, 20);
  });
});
