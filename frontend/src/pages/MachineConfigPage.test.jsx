import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MachineConfigPage } from './MachineConfigPage';

const config = [
  {
    needNum: null, num: 2, totalNum: 146, name: 'Intel 8457C 2.6G 97.5M 48C 350W CPU', materialgroup: '620002',
    dn: 'B6200004', relationId: 'local-B6200004', id: 'local-30240235-B6200004', type: 'CPU',
    params: [
      { id: 'local-30240235-B6200004-model', orderId: 30240235, relationId: 'local-B6200004', materialSn: 'B6200004', comparionValue: '8457C', name: '型号', partsDescription: 'Intel CPU', materialGroup: '620002' },
      { id: 'local-30240235-B6200004-location', orderId: 30240235, relationId: 'local-B6200004', materialSn: 'B6200004', comparionValue: '1N00', name: '库位', partsDescription: 'Intel CPU', materialGroup: '620002' },
    ],
  },
  {
    needNum: null, num: 8, totalNum: 584, name: '安费诺 25CM MCIO 8X 转 MCIO 8X 线缆', materialgroup: '370001',
    dn: '3700001', relationId: 'local-3700001', id: 'local-30240235-3700001', type: 'XL',
    params: [
      { id: 'local-30240235-3700001-location', orderId: 30240235, relationId: 'local-3700001', materialSn: '3700001', comparionValue: '1N00', name: '库位', partsDescription: '线缆', materialGroup: '370001' },
    ],
  },
];

it('explains every returned field and renders the aggregated machine configuration', async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, json: async () => config }));
  vi.stubGlobal('fetch', fetchMock);
  render(<MachineConfigPage />);

  expect(screen.getByText('需求数量')).toBeInTheDocument();
  expect(screen.getByText('单机数量')).toBeInTheDocument();
  expect(screen.getByText('订单合计')).toBeInTheDocument();
  expect(screen.getByText('参数数组')).toBeInTheDocument();
  expect(screen.getByText('comparionValue')).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText('整机序列号 · SN'), { target: { value: ' 6102310604796170 ' } });
  fireEvent.click(screen.getByRole('button', { name: '查询配置' }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/machines/config/5000?sn=6102310604796170'), expect.any(Object)));
  const summary = await screen.findAllByText('30240235');
  expect(summary.length).toBeGreaterThan(1);
  expect(screen.getByText('Intel 8457C 2.6G 97.5M 48C 350W CPU')).toBeInTheDocument();
  expect(screen.getAllByText('8457C').length).toBeGreaterThan(0);
  expect(screen.getByText(/× 2/)).toBeInTheDocument();
  expect(screen.getAllByText('处理器').length).toBeGreaterThan(0);
  expect(screen.getAllByText('线缆').length).toBeGreaterThan(0);
  expect(screen.getByText('查看原始返回 JSON')).toBeInTheDocument();
});

it('shows the backend error when the SN has no production order', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, json: async () => ({ error: '未找到 SN 999 对应的生产订单' }) })));
  render(<MachineConfigPage />);
  fireEvent.change(screen.getByLabelText('整机序列号 · SN'), { target: { value: '999' } });
  fireEvent.click(screen.getByRole('button', { name: '查询配置' }));
  const alert = await screen.findByRole('alert');
  expect(within(alert).getByText('未找到 SN 999 对应的生产订单')).toBeInTheDocument();
});


it('switches the data source and queries HANA', async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, json: async () => config }));
  vi.stubGlobal('fetch', fetchMock);
  render(<MachineConfigPage />);
  expect(screen.getByRole('radio', { name: /5000/ })).toBeChecked();
  fireEvent.click(screen.getByRole('radio', { name: /HANA/ }));
  fireEvent.change(screen.getByLabelText('整机序列号 · SN'), { target: { value: '9800200402645747' } });
  fireEvent.click(screen.getByRole('button', { name: '查询配置' }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/machines/config/hana?sn=9800200402645747'), expect.any(Object)));
  expect(await screen.findByText('Intel 8457C 2.6G 97.5M 48C 350W CPU')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('radio', { name: /5000/ }));
  expect(screen.queryByText('Intel 8457C 2.6G 97.5M 48C 350W CPU')).not.toBeInTheDocument();
});
