import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { SAPQueryPage } from './SAPQueryPage';

it('queries both SAP sources independently and shows errors without hiding successful results', async () => {
  const fetchMock = vi.fn(async (_url, options) => {
    const body = JSON.parse(options.body);
    if (body.source === 'SG') return { ok: false, status: 502, json: async () => ({ error: 'SG 不可用' }) };
    return { ok: true, json: async () => ({ source: 'KK', fetched: 2, items: [{ AUFNR: '30222644', VBELN: 'SO-1' }] }) };
  });
  vi.stubGlobal('fetch', fetchMock);
  render(<SAPQueryPage />);
  fireEvent.change(screen.getByLabelText('变更开始日期 · CHDAT'), { target: { value: '2026-01-01' } });
  fireEvent.change(screen.getByLabelText('变更结束日期 · CHDAT_TO'), { target: { value: '2026-01-31' } });
  fireEvent.change(screen.getByLabelText('生产订单 · AUFNR（可选）'), { target: { value: '30222644' } });
  fireEvent.click(screen.getByRole('button', { name: '查询 KK 和 SG' }));
  expect(await within(screen.getByRole('region', { name: 'KK 查询结果' })).findByText('30222644')).toBeInTheDocument();
  expect(await within(screen.getByRole('region', { name: 'SG 查询结果' })).findByText('SG 不可用')).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ source: 'KK', start: '2026-01-01', end: '2026-01-31', order: '30222644' });
});
