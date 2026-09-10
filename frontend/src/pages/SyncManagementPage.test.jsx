import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SyncManagementPage } from './SyncManagementPage';

function response(payload) {
  return { ok: true, text: async () => JSON.stringify(payload) };
}

describe('SyncManagementPage', () => {
  it('requires confirmation and starts a selected full sync', async () => {
    const fetchMock = vi.fn(async (url, options = {}) => {
      if (url === '/api/sync/tasks') return response({ items: [{ id: 'sales_orders', label: '销售订单', dependencies: [], fullSupported: true }] });
      if (String(url).startsWith('/api/sync/runs?')) return response({ items: [] });
      if (url === '/api/sync/runs') return response({ runId: 'run-1', state: 'running' });
      return response({});
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<SyncManagementPage />);
    await screen.findByText('销售订单');
    fireEvent.click(screen.getByRole('button', { name: '全量' }));
    fireEvent.change(screen.getByLabelText('全量开始日期'), { target: { value: '2026-01-01' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '我确认执行全量同步及成功后的范围清理' }));
    fireEvent.click(screen.getByRole('button', { name: '执行全量同步' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/sync/runs', expect.objectContaining({ method: 'POST', body: JSON.stringify({ mode: 'full', taskIds: ['sales_orders'], startDate: '2026-01-01', endDate: undefined }) })));
  });
});
