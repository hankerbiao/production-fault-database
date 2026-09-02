import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from './main.jsx';

const faultStats = { total: 1, withError: 1, withRepairPerson: 1, salesOrders: 1, productionOrders: 1, missingSalesOrder: 0, missingProductionOrder: 0, dataStartDate: '20260101', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' };
const orderStats = { total: 1, salesOrders: 1, sg: 1, kk: 0, orderQuantity: 3, machineQuantity: 3, storageQuantity: 2, dataStartDate: '20260101', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' };

function mockFetch({ fail = false } = {}) {
  return vi.fn(async (url, options = {}) => {
    if (fail) return { ok: false, status: 503, json: async () => ({ error: 'down' }) };
    if (url === '/api/sync/status') return { ok: true, json: async () => ({ state: 'idle', message: '等待同步' }) };
    if (String(url).startsWith('/api/orders/models')) return { ok: true, json: async () => ({ items: ['Model-A', 'Model-B'] }) };
    if (String(url).startsWith('/api/faults?')) return { ok: true, json: async () => ({ items: [{ id: 'r1', hostBarcode: 'PC-1', salesOrder: 'SO-1', productionOrder: 'PO-1', plannedStartDate: '20260101', materialDescription: '物料', faultDescription: '故障', ngStation: '站点' }], total: 1 }) };
    if (String(url).startsWith('/api/faults/stats')) return { ok: true, json: async () => faultStats };
    if (String(url).startsWith('/api/orders?')) return { ok: true, json: async () => ({ items: [{ id: 'SG:PO-1', source: 'SG', aufnr: 'PO-1', salesOrder: 'SO-1', customerId: 'C1', materialDescription: '物料', orderQuantity: 3, storageQuantity: 2, recordCount: 1 }], total: 1 }) };
    if (String(url).startsWith('/api/orders/stats')) return { ok: true, json: async () => orderStats };
    if (String(url).startsWith('/api/views/Z_V_ZMES_T_001?')) return { ok: true, json: async () => ({ items: [{ id: 'station-1', HISTROYID: 'H-1', PCODE: 'PC-1', OCODE: 'OC-1', AUFNR: 'PO-1', SPEC: 'OP-10', OPERATION: '装配', GSTRS: '20260102', ACTUAL_START_TIME: '2026-01-02 03:04:05', ACTUAL_END_TIME: '2026-01-02 03:05:05' }], total: 1 }) };
    if (String(url).startsWith('/api/views/Z_V_ZMES_T_001/stats')) return { ok: true, json: async () => ({ total: 1, missingSalesOrder: 3, missingProductionOrder: 2, dataStartDate: '20260102', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' }) };
    if (String(url).startsWith('/api/views/Z_V_ZMES_T_001/detail')) return { ok: true, json: async () => ({ fields: [{ key: 'PCODE', label: '主机序列号', value: 'PC-1' }, { key: 'PRODH', label: '产品层次', value: '00100' }] }) };
    if (String(url).startsWith('/api/views/ZSGV_ZPP_SERNOLIST?')) return { ok: true, json: async () => ({ items: [{ id: 'serial-1', ZCODE_HEAD: 'HEAD-1', ZCODE_ITEM: 'ITEM-1', AUFNR_HEAD: 'PO-H', AUFNR_ITEM: 'PO-I', PRODH: '00100' }], total: 1 }) };
    if (String(url).startsWith('/api/views/ZSGV_ZPP_SERNOLIST/stats')) return { ok: true, json: async () => ({ total: 1, dataStartDate: '', dataEndDate: '', latestSyncedAt: '' }) };
    if (String(url).startsWith('/api/views/ZSGV_ZPP_SERNOLIST/detail')) return { ok: true, json: async () => ({ fields: [{ key: 'ZCODE_HEAD', label: '大刀/机头序列号（ZCODE_HEAD）', value: 'HEAD-1' }] }) };
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
    expect(screen.getByRole('columnheader', { name: '计划开始时间' })).toBeInTheDocument();
    expect((await screen.findAllByText('PC-1')).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByTitle('查看完整数据库字段')[0]);
    expect(await screen.findByText('SN-1')).toBeInTheDocument();
    fireEvent.click(screen.getByTitle('关闭'));
    await waitFor(() => expect(screen.queryByText('SN-1')).not.toBeInTheDocument());
  });

  it('uses one period control for custom repair date ranges', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    await screen.findByRole('heading', { name: '维修故障记录' });
    fireEvent.click(screen.getByRole('button', { name: /维修时间周期/ }));
    fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2026-03-01' } });
    fireEvent.change(screen.getByLabelText('结束日期'), { target: { value: '2026-03-31' } });
    fireEvent.click(screen.getByRole('button', { name: /筛选/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('dateFrom=2026-03-01') && String(url).includes('dateTo=2026-03-31'))).toBe(true));
  });

  it('offers searchable model options from the order model list', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    await screen.findByRole('heading', { name: '维修故障记录' });
    const modelInput = screen.getByRole('textbox', { name: '机型 / ZJXMC' });
    fireEvent.focus(modelInput);
    fireEvent.change(modelInput, { target: { value: 'Model-B' } });
    expect(await screen.findByRole('option', { name: 'Model-B' })).toBeInTheDocument();
  });

  it('switches to orders and sends source filter', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.click(screen.getAllByRole('button', { name: /销售订单看板/ })[0]);
    expect(await screen.findByRole('heading', { name: '销售订单看板' })).toBeInTheDocument();
    expect(await screen.findByText('机器数量汇总：3')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'SG' } });
    fireEvent.click(screen.getByRole('button', { name: /筛选/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('source=SG'))).toBe(true));
  });

  it('exports all filtered order rows as a CSV download', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:orders');
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    render(<App />);
    fireEvent.click(screen.getAllByRole('button', { name: /销售订单看板/ })[0]);
    await screen.findByRole('heading', { name: '销售订单看板' });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'SG' } });
    fireEvent.click(screen.getByRole('button', { name: /导出数据/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('source=SG') && String(url).includes('pageSize=100'))).toBe(true));
    expect(createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:orders');
    createObjectURL.mockRestore();
    revokeObjectURL.mockRestore();
    click.mockRestore();
  });

  it('shows bilingual station headers and documented detail labels', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /工位记录/ }));
    expect(await screen.findByRole('heading', { name: '工位记录' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '主机序列号（PCODE）' })).toBeInTheDocument();
    expect(screen.getByText('销售订单为空')).toBeInTheDocument();
    expect(screen.getByText('生产订单为空')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    fireEvent.click(screen.getAllByTitle('查看完整数据库字段')[0]);
    expect(await screen.findByText('主机序列号')).toBeInTheDocument();
    expect(screen.getByText('产品层次')).toBeInTheDocument();
  });

  it('shows bilingual serial binding headers and fields', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /序列号绑定/ }));
    expect(await screen.findByRole('heading', { name: '序列号绑定 / Serial Number Binding', exact: true })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '大刀/机头序列号（ZCODE_HEAD）' })).toBeInTheDocument();
    fireEvent.click(screen.getAllByTitle(/查看完整数据库字段/)[0]);
    expect(await screen.findByText('大刀/机头序列号（ZCODE_HEAD）')).toBeInTheDocument();
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
