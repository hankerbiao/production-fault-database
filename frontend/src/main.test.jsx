import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from './main.jsx';

const faultStats = { total: 1, withError: 1, withRepairPerson: 1, salesOrders: 1, productionOrders: 1, missingSalesOrder: 0, missingProductionOrder: 0, dataStartDate: '20260101', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' };
const orderStats = { total: 1, salesOrders: 1, sg: 1, kk: 0, orderQuantity: 3, storageQuantity: 2, dataStartDate: '20260101', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' };

function mockFetch({ fail = false } = {}) {
  return vi.fn(async (url, options = {}) => {
    if (fail) return { ok: false, status: 503, json: async () => ({ error: 'down' }) };
    if (url === '/api/sync/status') return { ok: true, json: async () => ({ state: 'idle', message: '等待同步' }) };
    if (String(url).startsWith('/api/faults?')) return { ok: true, json: async () => ({ items: [{ id: 'r1', hostBarcode: 'PC-1', salesOrder: 'SO-1', productionOrder: 'PO-1', materialDescription: '物料', faultDescription: '故障', ngStation: '站点' }], total: 1 }) };
    if (String(url).startsWith('/api/faults/stats')) return { ok: true, json: async () => faultStats };
    if (String(url).startsWith('/api/orders?')) return { ok: true, json: async () => ({ items: [{ id: 'SG:PO-1', source: 'SG', aufnr: 'PO-1', salesOrder: 'SO-1', customerId: 'C1', materialDescription: '物料', orderQuantity: 3, storageQuantity: 2, recordCount: 1 }], total: 1 }) };
    if (String(url).startsWith('/api/orders/stats')) return { ok: true, json: async () => orderStats };
    if (String(url).startsWith('/api/views/Z_V_ZMES_T_001?')) return { ok: true, json: async () => ({ items: [{ id: 'station-1', HISTROYID: 'H-1', PCODE: 'PC-1', OCODE: 'OC-1', AUFNR: 'PO-1', SPEC: 'OP-10', OPERATION: '装配', GSTRS: '20260102', ACTUAL_START_TIME: '2026-01-02 03:04:05', ACTUAL_END_TIME: '2026-01-02 03:05:05' }], total: 1 }) };
    if (String(url).startsWith('/api/views/Z_V_ZMES_T_001/stats')) return { ok: true, json: async () => ({ total: 1, dataStartDate: '20260102', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' }) };
    if (String(url).startsWith('/api/views/Z_V_ZMES_T_001/detail')) return { ok: true, json: async () => ({ fields: [{ key: 'PCODE', label: '主机序列号', value: 'PC-1' }, { key: 'PRODH', label: '产品层次', value: '00100' }] }) };
    if (url.includes('/detail')) return { ok: true, json: async () => ({ fault: { serialNumber: 'SN-1', hostBarcode: 'PC-1' }, fields: [{ key: 'PCODE', label: '主机条码', value: 'PC-1' }] }) };
    if (options.method === 'POST') return { ok: true, json: async () => ({ state: 'running', message: '正在执行增量同步' }) };
    return { ok: true, json: async () => ({}) };
  });
}

describe('operations workbench', () => {
  it('loads repair records and opens detail drawer', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    expect(await screen.findByRole('heading', { name: '维修故障记录' })).toBeInTheDocument();
    expect(screen.getByText('SAP HANA 视图 ZSGV_ZZT_WLJL', { exact: false })).toBeInTheDocument();
    expect((await screen.findAllByText('PC-1')).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByTitle('查看完整数据库字段')[0]);
    expect(await screen.findByText('SN-1')).toBeInTheDocument();
    fireEvent.click(screen.getByTitle('关闭'));
    await waitFor(() => expect(screen.queryByText('SN-1')).not.toBeInTheDocument());
  });

  it('switches to orders and sends source filter', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.click(screen.getAllByRole('button', { name: /销售订单看板/ })[0]);
    expect(await screen.findByRole('heading', { name: '销售订单看板' })).toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'SG' } });
    fireEvent.click(screen.getByRole('button', { name: /筛选/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('source=SG'))).toBe(true));
  });

  it('shows bilingual station headers and documented detail labels', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /工位记录/ }));
    expect(await screen.findByRole('heading', { name: '工位记录' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '主机序列号（PCODE）' })).toBeInTheDocument();
    fireEvent.click(screen.getAllByTitle('查看完整数据库字段')[0]);
    expect(await screen.findByText('主机序列号')).toBeInTheDocument();
    expect(screen.getByText('产品层次')).toBeInTheDocument();
  });

  it('shows a loading state while a view is fetching', async () => {
    const baseFetch = mockFetch();
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    vi.stubGlobal('fetch', vi.fn((url, options) => String(url).startsWith('/api/views/ZSGV_ZSD124?') ? pending : baseFetch(url, options)));
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /订单过账/ }));
    expect(await screen.findByRole('status')).toHaveTextContent('正在加载订单 BOM 过账数据');
    expect(screen.queryByText('没有找到匹配的数据')).not.toBeInTheDocument();
    release({ ok: true, json: async () => ({ items: [], total: 0 }) });
  });

  it('shows disconnected state when gateway fails', async () => {
    vi.stubGlobal('fetch', mockFetch({ fail: true }));
    render(<App />);
    expect(await screen.findByText('无法连接 MongoDB 网关')).toBeInTheDocument();
    expect(screen.getByText('MongoDB 未连接')).toBeInTheDocument();
  });

  it('starts an incremental sync from the header', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    await screen.findByRole('heading', { name: '维修故障记录' });
    fireEvent.click(screen.getByRole('button', { name: /立即同步/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(true));
    expect(screen.getByRole('button', { name: /同步中/ })).toBeDisabled();
  });

  it('keeps the running state when another client already started sync', async () => {
    const baseFetch = mockFetch();
    const fetchMock = vi.fn((url, options = {}) => options.method === 'POST'
      ? Promise.resolve({ ok: false, status: 409, json: async () => ({ error: '已有同步任务运行中', status: { state: 'running', message: '正在执行增量同步' } }) })
      : baseFetch(url, options));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    await screen.findByRole('heading', { name: '维修故障记录' });
    fireEvent.click(screen.getByRole('button', { name: /立即同步/ }));
    expect(await screen.findByRole('button', { name: /同步中/ })).toBeDisabled();
  });
});
