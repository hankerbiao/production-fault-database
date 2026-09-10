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
});
