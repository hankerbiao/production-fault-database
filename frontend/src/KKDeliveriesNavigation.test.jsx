import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { App } from './main.jsx';

it('opens KK delivery details from the existing main navigation', async () => {
  const fetchMock = vi.fn(async url => {
    if (url === '/api/sync/status') return { ok: true, json: async () => ({ state: 'idle', message: '等待同步' }) };
    if (url === '/api/config') return { ok: true, json: async () => ({ database: { status: '已连接' } }) };
    if (String(url).startsWith('/api/kk-sales-order-deliveries')) {
      return { ok: true, json: async () => ({ items: [], page: 1, pageSize: 20, total: 0, collectionEmpty: true }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  vi.stubGlobal('fetch', fetchMock);
  render(<App />);

  const navigation = screen.getByRole('complementary', { name: '主导航' });
  fireEvent.click(within(navigation).getByRole('button', { name: /KK 销售订单交付明细/ }));
  expect(await screen.findByRole('heading', { name: 'KK 销售订单交付明细' })).toBeInTheDocument();
  expect(await screen.findByText('设置条件后查询')).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalledWith(expect.stringMatching(/^\/api\/kk-sales-order-deliveries/), expect.any(Object));
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  expect(await screen.findByText('集合暂时无数据')).toBeInTheDocument();
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/^\/api\/kk-sales-order-deliveries/), expect.any(Object)));
});
