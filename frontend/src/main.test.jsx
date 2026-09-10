import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from './main.jsx';

const faultStats = { total: 1, withError: 1, withRepairPerson: 1, salesOrders: 1, productionOrders: 1, hostBarcodes: 1, missingSalesOrder: 0, missingProductionOrder: 0, dataStartDate: '20260101', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' };
const orderStats = { total: 1, salesOrders: 1, sg: 1, kk: 0, orderQuantity: 3, machineQuantity: 3, storageQuantity: 2, dataStartDate: '20260101', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' };

function mockFetch({ fail = false } = {}) {
  return vi.fn(async (url, options = {}) => {
    if (fail) return { ok: false, status: 503, json: async () => ({ error: 'down' }) };
    if (url === '/api/sync/status') return { ok: true, json: async () => ({ state: 'idle', message: '等待同步' }) };
    if (url === '/api/config') return { ok: true, json: async () => ({ serviceName: 'Production Fault Gateway', version: '0.1.0', environment: 'test', runtime: 'go1.25 / darwin / arm64', listenAddress: ':18080', frontendURL: 'http://127.0.0.1:5173/', apiBasePath: '/api', database: { address: 'mongo.example:27017', name: 'faults', authSource: 'admin', replicaSet: 'rs0', authenticationConfigured: true, tls: true, status: '已连接' }, collections: [{ label: '维修故障', name: 'repairs' }], features: [{ label: '分页查询（每页 20 条）', enabled: true }] }) };
    if (url === '/api/openapi.json') return { ok: true, json: async () => ({ openapi: '3.0.3', info: { version: '1.0.0' }, servers: [{ url: '/api' }], paths: { '/api/health': { get: { tags: ['health'], operationId: 'health', summary: '健康检查', responses: { '200': { description: '正常' } } } }, '/api/faults': { get: { tags: ['faults'], operationId: 'listFaults', summary: '查询维修故障', parameters: [{ name: 'page', in: 'query', required: false, description: '页码' }], responses: { '200': { description: '分页结果' } } } }, '/api/views/SCS_DOA': { get: { tags: ['scs'], operationId: 'listSCSDoaRecords', summary: '查询 SCS DOA 申报', responses: { '200': { description: '分页结果' } } } }, '/api/sync/incremental': { post: { tags: ['sync'], operationId: 'sync', summary: '增量同步', responses: { '202': { description: '已启动' } } } } } }) };
    if (url === '/api/agent-guide.md') return { ok: true, text: async () => '# Agent Guide' };
    if (String(url).startsWith('/api/orders/models')) return { ok: true, json: async () => ({ items: ['Model-A', 'Model-B'] }) };
    if (String(url).startsWith('/api/faults?')) return { ok: true, json: async () => ({ items: [{ id: 'r1', hostBarcode: 'PC-1', salesOrder: 'SO-1', productionOrder: 'PO-1', plannedStartDate: '20260101', materialDescription: '物料', faultDescription: '故障', ngStation: '站点' }], total: 1 }) };
    if (String(url).startsWith('/api/faults/stats')) return { ok: true, json: async () => faultStats };
    if (String(url).startsWith('/api/orders?')) return { ok: true, json: async () => ({ items: [{ id: 'SG:PO-1', source: 'SG', aufnr: 'PO-1', salesOrder: 'SO-1', customerId: 'C1', materialDescription: '物料', orderQuantity: 3, storageQuantity: 2, recordCount: 1 }], total: 1 }) };
    if (String(url).startsWith('/api/orders/stats')) return { ok: true, json: async () => orderStats };
    if (String(url).startsWith('/api/views/Z_V_ZMES_T_001?')) return { ok: true, json: async () => ({ items: [{ id: 'station-1', HISTROYID: 'H-1', PCODE: 'PC-1', OCODE: 'OC-1', AUFNR: 'PO-1', SPEC: 'OP-10', OPERATION: '装配', GSTRS: '20260102', ACTUAL_START_TIME: '2026-01-02 03:04:05', ACTUAL_END_TIME: '2026-01-02 03:05:05' }], total: 1, preview: true, hasMore: false }) };
    if (String(url).startsWith('/api/views/Z_V_ZMES_T_001/stats')) return { ok: true, json: async () => ({ total: 1, missingSalesOrder: 3, missingProductionOrder: 2, dataStartDate: '20260102', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' }) };
    if (String(url).startsWith('/api/views/Z_V_ZMES_T_001/detail')) return { ok: true, json: async () => ({ fields: [{ key: 'PCODE', label: '主机序列号', value: 'PC-1' }, { key: 'PRODH', label: '产品层次', value: '00100' }] }) };
    if (String(url).startsWith('/api/views/ZSGV_ZSD124?')) return { ok: true, json: async () => ({ items: [{ id: 'bom-1', MATNR: 'MAT-1' }], total: 2 }) };
    if (String(url).startsWith('/api/views/ZSGV_ZSD124/stats')) return { ok: true, json: async () => ({ total: 2, salesOrders: 1, productionOrders: 2, missingSalesOrder: 3, missingProductionOrder: 4, missingProductionOrderDistinct: 2, dataStartDate: '20260102', dataEndDate: '20260102', latestSyncedAt: '2026-01-02T03:04:05Z' }) };
    if (String(url).startsWith('/api/views/ZSGV_ZSD124/detail')) return { ok: true, json: async () => ({ fields: [{ key: 'MATNR', label: '物料号', value: 'MAT-1' }] }) };
    if (String(url).startsWith('/api/views/ZSGV_ZPP_SERNOLIST?')) return { ok: true, json: async () => ({ items: [{ id: 'serial-1', ZCODE_HEAD: 'HEAD-1', ZCODE_ITEM: 'ITEM-1', AUFNR_HEAD: 'PO-H', AUFNR_ITEM: 'PO-I', PRODH: '00100' }], total: 1 }) };
    if (String(url).startsWith('/api/views/ZSGV_ZPP_SERNOLIST/stats')) return { ok: true, json: async () => ({ total: 1, dataStartDate: '', dataEndDate: '', latestSyncedAt: '' }) };
    if (String(url).startsWith('/api/views/ZSGV_ZPP_SERNOLIST/detail')) return { ok: true, json: async () => ({ fields: [{ key: 'ZCODE_HEAD', label: '大刀/机头序列号（ZCODE_HEAD）', value: 'HEAD-1' }] }) };
    if (String(url).startsWith('/api/views/SCS_DOA?')) return { ok: true, json: async () => ({ items: [{ id: 'doa-1', doa_code: 'DOA-1', doa_judge: '是', customer_uid: '北京示例科技有限公司' }], total: 1 }) };
    if (String(url).startsWith('/api/views/SCS_DOA/stats')) return { ok: true, json: async () => ({ total: 1, dataStartDate: '', dataEndDate: '', latestSyncedAt: '' }) };
    if (url.includes('/detail')) return { ok: true, json: async () => ({ fault: { serialNumber: 'SN-1', hostBarcode: 'PC-1' }, fields: [{ key: 'PCODE', label: '主机条码', value: 'PC-1' }] }) };
    if (options.method === 'POST') return { ok: true, json: async () => ({ state: 'running', message: '正在执行增量同步' }) };
    return { ok: true, json: async () => ({}) };
  });
}

describe('operations workbench', () => {
  it('opens the service configuration panel with redacted database metadata', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '查看服务配置' }));
    expect(await screen.findByRole('heading', { name: '服务配置' })).toBeInTheDocument();
    expect(await screen.findByText('mongo.example:27017')).toBeInTheDocument();
    expect(screen.getByText('已配置（已隐藏）')).toBeInTheDocument();
    expect(screen.queryByText('密码')).not.toBeInTheDocument();
  });

  it('opens API docs, searches, expands and copies an operation', async () => {
    vi.stubGlobal('fetch', mockFetch());
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'API 文档' }));
    expect(await screen.findByRole('heading', { name: 'API 文档中心' })).toBeInTheDocument();
    expect(screen.getByText('需人工确认')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索接口' }), { target: { value: '维修' } });
    expect(screen.getByText('/api/faults')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /GET.*api\/faults/ }));
    expect(await screen.findByText('页码')).toBeInTheDocument();
    fireEvent.click(screen.getByTitle('复制 curl'));
    expect(navigator.clipboard.writeText).toHaveBeenCalled();
  });

  it('groups SCS operations separately in the API docs center', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'API 文档' }));
    expect(await screen.findByRole('heading', { name: 'API 文档中心' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /SCS 数据/ }));
    expect(screen.getByText('/api/views/SCS_DOA')).toBeInTheDocument();
    expect(screen.getByText('查询 SCS DOA 申报')).toBeInTheDocument();
  });

  it('loads repair records and opens detail drawer', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    expect(await screen.findByRole('heading', { name: '维修故障记录' })).toBeInTheDocument();
    expect(screen.getByText('SAP HANA 视图 ZSGV_ZZT_WLJL', { exact: false })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '计划生产时间' })).toBeInTheDocument();
    expect(screen.getByText('主机条码数量（去重）')).toBeInTheDocument();
    expect((await screen.findAllByText('PC-1')).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByTitle('查看完整数据库字段')[0]);
    expect(await screen.findByText('SN-1')).toBeInTheDocument();
    fireEvent.click(screen.getByTitle('关闭'));
    await waitFor(() => expect(screen.queryByText('SN-1')).not.toBeInTheDocument());
  });

  it('filters repair records by planned or repair time', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    await screen.findByRole('heading', { name: '维修故障记录' });
    expect(screen.getByRole('button', { name: /计划生产时间周期/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /计划生产时间周期/ }));
    fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2026-03-01' } });
    fireEvent.change(screen.getByLabelText('结束日期'), { target: { value: '2026-03-31' } });
    fireEvent.click(screen.getByRole('button', { name: /筛选/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('timeField=planned') && String(url).includes('dateFrom=2026-03-01') && String(url).includes('dateTo=2026-03-31'))).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: '维修时间' }));
    expect(screen.getByRole('button', { name: /维修时间周期/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /筛选/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('timeField=repair'))).toBe(true));
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
    fireEvent.change(screen.getByRole('textbox', { name: '客户 ID / 最终用户' }), { target: { value: '客户A' } });
    fireEvent.click(screen.getByRole('button', { name: /筛选/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('source=SG') && String(url).includes('customer=%E5%AE%A2%E6%88%B7A'))).toBe(true));
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
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /工位记录/ }));
    expect(await screen.findByRole('heading', { name: '工位记录' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '主机序列号（PCODE）' })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/api/views/Z_V_ZMES_T_001?') && String(url).includes('page=1') && String(url).includes('pageSize=20') && String(url).includes('preview=true'))).toBe(true));
    expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/views/Z_V_ZMES_T_001/stats'))).toBe(false);
    expect(screen.getByText('当前页 1 条')).toBeInTheDocument();
    fireEvent.click(screen.getAllByTitle('查看完整数据库字段')[0]);
    expect(await screen.findByText('主机序列号')).toBeInTheDocument();
    expect(screen.getByText('产品层次')).toBeInTheDocument();
  });

  it('loads view statistics when a non-preview dashboard is selected', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /工位记录/ }));
    expect(await screen.findByRole('heading', { name: '工位记录' })).toBeInTheDocument();
    expect(screen.queryByText('记录总数')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /订单过账/ }));
    expect(await screen.findByRole('heading', { name: '订单 BOM 过账' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('记录总数').parentElement).toHaveTextContent('2'));
  });

  it('shows bilingual serial binding headers and fields', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /序列号绑定/ }));
    expect(await screen.findByRole('heading', { name: '序列号绑定 / Serial Number Binding', exact: true })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '大刀/机头序列号（ZCODE_HEAD）' })).toBeInTheDocument();
    fireEvent.click(screen.getAllByTitle(/查看完整数据库字段/)[0]);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('大刀/机头序列号（ZCODE_HEAD）')).toBeInTheDocument();
  });

  it('filters SCS DOA records with the 5000 company checkbox', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'SCS DOA 申报' }));
    expect(await screen.findByRole('heading', { name: 'SCS DOA 申报明细' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /高级条件展开/ }));
    const checkbox = await screen.findByRole('checkbox', { name: '是否为5000公司' });
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole('button', { name: /筛选/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/api/views/SCS_DOA?') && String(url).includes('company5000=true'))).toBe(true));
  });

  it('filters SCS DOA records by status and judgment', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'SCS DOA 申报' }));
    expect(await screen.findByRole('heading', { name: 'SCS DOA 申报明细' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /高级条件展开/ }));

    expect(screen.getByRole('combobox', { name: '处理状态' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '判定结果' })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('DOA类型（整机 / 备件）')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('曙光S/N 或备件S/N')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('产品/备件名称')).not.toBeInTheDocument();

    fireEvent.change(screen.getByRole('combobox', { name: '处理状态' }), { target: { value: '待受理' } });
    fireEvent.change(screen.getByRole('combobox', { name: '判定结果' }), { target: { value: '是' } });
    fireEvent.click(screen.getByRole('button', { name: /筛选/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => {
      const query = new URL(String(url), 'http://localhost').searchParams;
      return query.get('status') === '待受理' && query.get('judgment') === '是';
    })).toBe(true));
  });

  it('keeps SCS DOA judgment compact and customer names readable', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'SCS DOA 申报' }));
    expect(await screen.findByRole('heading', { name: 'SCS DOA 申报明细' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '判定结果' })).toHaveClass('column-judgment');
    expect(screen.getByRole('columnheader', { name: '客户全称' })).toHaveClass('column-customer');
    expect(await screen.findByText('是')).toHaveClass('result-pill', 'is-yes');
    expect(screen.getByText('北京示例科技有限公司')).toBeInTheDocument();
  });

  it('filters SCS change records with the 5000 company checkbox', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'SCS 换上换下' }));
    expect(await screen.findByRole('heading', { name: 'SCS 换上换下明细' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /高级条件展开/ }));
    const checkbox = await screen.findByRole('checkbox', { name: '是否为5000公司' });
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole('button', { name: /筛选/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/api/views/SCS_CHANGE?') && String(url).includes('company5000=true'))).toBe(true));
  });

  it('shows BOM production and sales order summaries', async () => {
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /订单过账/ }));
    expect(await screen.findByRole('heading', { name: '订单 BOM 过账' })).toBeInTheDocument();
    expect(screen.getByText('生产订单数量（去重）').parentElement).toHaveTextContent('2');
    expect(screen.getByText('销售订单数量（去重）').parentElement).toHaveTextContent('1');
    expect(screen.getByText('销售订单为空').parentElement).toHaveTextContent('3');
    expect(screen.getByText('生产订单为空').parentElement).toHaveTextContent('4');
    expect(screen.getByText('生产订单为空（去重）').parentElement).toHaveTextContent('2');
  });

  it('exports BOM rows with an empty sales order', async () => {
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:bom');
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /订单过账/ }));
    await screen.findByRole('heading', { name: '订单 BOM 过账' });
    fireEvent.click(screen.getByRole('button', { name: '导出销售订单为空' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/api/views/ZSGV_ZSD124?') && String(url).includes('missingSalesOrder=true'))).toBe(true));
    expect(createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:bom');
    createObjectURL.mockRestore();
    revokeObjectURL.mockRestore();
    click.mockRestore();
  });

  it('shows a loading state while a view is fetching', async () => {
    const baseFetch = mockFetch();
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    vi.stubGlobal('fetch', vi.fn((url, options) => String(url).startsWith('/api/views/ZSGV_ZSD124?') ? pending : baseFetch(url, options)));
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /订单过账/ }));
    expect(await screen.findByRole('status')).toHaveTextContent('正在加载订单 BOM 过账数据');
    expect(document.querySelector('.bom-table-shell .loading-spinner')).toBeInTheDocument();
    expect(document.querySelectorAll('.bom-table-shell .loading-bars i')).toHaveLength(4);
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
