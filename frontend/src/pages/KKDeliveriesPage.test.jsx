import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { downloadKKDeliveries, getKKDeliveryDetail, listKKDeliveries } from '../api/kkDeliveries';
import { KKDeliveriesPage } from './KKDeliveriesPage';

vi.mock('../api/kkDeliveries', () => ({ listKKDeliveries: vi.fn(), getKKDeliveryDetail: vi.fn(), downloadKKDeliveries: vi.fn() }));

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

it('formats WADAT, keeps identifiers as strings, applies filters and paginates on the server', async () => {
  listKKDeliveries.mockResolvedValue({
    items: [
      { VBELN: '0000123456', MATNR: '0000000061234', WADAT: '20261124', DATUM: '20261120', KUNNR: '0000009876', WERKS: '1000', SERNR: '0001234', SN: '612340123456789' },
      { id: 'kk-row-2', VBELN: '0000123456', MATNR: '0000000061234', WADAT: '20261124', DATUM: '20261124', KUNNR: '0000009876', WERKS: '1000', SERNR: '0001235', SN: '612340123456790' },
    ],
    page: 1, pageSize: 20, total: 41, collectionEmpty: false,
  });
  render(<KKDeliveriesPage />);
  expect(listKKDeliveries).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  expect(await screen.findAllByText('2026-11-24')).toHaveLength(3);
  expect(screen.getByRole('columnheader', { name: '数据时间（DATUM）' })).toBeInTheDocument();
  expect(screen.getByText('2026-11-20')).toBeInTheDocument();
  expect(screen.getAllByText('0000123456')).toHaveLength(2);
  expect(screen.getAllByText('0000000061234')).toHaveLength(2);
  expect(screen.getAllByText('0000009876')).toHaveLength(2);
  expect(screen.getByText('0001234')).toBeInTheDocument();
  expect(screen.getByText('0001235')).toBeInTheDocument();
  expect(screen.getByText('612340123456789')).toBeInTheDocument();
  expect(screen.getByText('612340123456790')).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: '设备序列号（SERNR）' })).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'SN' })).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText('销售订单号'), { target: { value: '0000123456' } });
  fireEvent.change(screen.getByLabelText('物料号'), { target: { value: '61234' } });
  fireEvent.change(screen.getByLabelText('客户号'), { target: { value: '0000009876' } });
  fireEvent.change(screen.getByLabelText('工厂'), { target: { value: '1000' } });
  fireEvent.change(screen.getByLabelText('SN'), { target: { value: '612340123456789' } });
  fireEvent.click(screen.getByRole('button', { name: '交付日期不限' }));
  fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2026-11-01' } });
  fireEvent.change(screen.getByLabelText('结束日期'), { target: { value: '2026-11-30' } });
  fireEvent.click(screen.getByRole('button', { name: '完成' }));
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  await waitFor(() => expect(listKKDeliveries).toHaveBeenLastCalledWith({
    dateFrom: '2026-11-01', dateTo: '2026-11-30', salesOrder: '0000123456',
    materialNumber: '61234', customerNumber: '0000009876', plant: '1000', sn: '612340123456789',
  }, 1, 20));

  fireEvent.click(screen.getByTitle('下一页'));
  await waitFor(() => expect(listKKDeliveries).toHaveBeenLastCalledWith(expect.any(Object), 2, 20));
  await waitFor(() => expect(screen.getByTitle('下一页')).not.toBeDisabled());
  fireEvent.click(screen.getByRole('button', { name: '重置' }));
  await waitFor(() => expect(screen.getByText('设置条件后查询')).toBeInTheDocument());
  expect(listKKDeliveries).toHaveBeenCalledTimes(3);
  expect(screen.getByLabelText('销售订单号')).toHaveValue('');
});

it('renders missing and null legacy serial fields as em dashes', async () => {
  listKKDeliveries.mockResolvedValue({
    items: [
      { VBELN: '1', MATNR: '2', WADAT: '20261124', KUNNR: '3', WERKS: '4' },
      { VBELN: '5', MATNR: '6', WADAT: '20261124', KUNNR: '7', WERKS: '8', SERNR: null, SN: null },
    ],
    page: 1, pageSize: 20, total: 2, collectionEmpty: false,
  });
  render(<KKDeliveriesPage />);
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  await screen.findAllByText('2026-11-24');
  expect(screen.getAllByText('—')).toHaveLength(4);
});

it('distinguishes an empty collection from a filter with no matches', async () => {
  listKKDeliveries.mockResolvedValueOnce({ items: [], total: 0, page: 1, pageSize: 20, collectionEmpty: true });
  render(<KKDeliveriesPage />);
  expect(listKKDeliveries).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  expect(await screen.findByText('集合暂时无数据')).toBeInTheDocument();

  listKKDeliveries.mockResolvedValueOnce({ items: [], total: 0, page: 1, pageSize: 20, collectionEmpty: false });
  fireEvent.change(screen.getByLabelText('销售订单号'), { target: { value: '0000999999' } });
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  expect(await screen.findByText('筛选无结果')).toBeInTheDocument();
});

it('shows query failures separately from empty results', async () => {
  listKKDeliveries.mockRejectedValue(new Error('暂时无法连接数据库'));
  render(<KKDeliveriesPage />);
  expect(listKKDeliveries).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  const alert = await screen.findByRole('alert');
  expect(alert).toHaveTextContent('查询失败');
  expect(alert).toHaveTextContent('暂时无法连接数据库');
});

it('does not query until the user submits the search', () => {
  render(<KKDeliveriesPage />);
  expect(screen.getByText('设置条件后查询')).toBeInTheDocument();
  expect(listKKDeliveries).not.toHaveBeenCalled();
});

it('opens a row and displays every raw field returned for that document', async () => {
  listKKDeliveries.mockResolvedValue({
    items: [{ id: 'raw-row-id', VBELN: '0000123456', MATNR: '0000000061234', WADAT: '20261124', KUNNR: '0000009876', WERKS: '1000', SERNR: '0001234', SN: '612340123456789' }],
    page: 1, pageSize: 20, total: 1, collectionEmpty: false,
  });
  getKKDeliveryDetail.mockResolvedValue({ VBELN: '0000123456', EXTRA_SOURCE_FIELD: '原始源字段', _sync_run_id: 'sync-1' });
  render(<KKDeliveriesPage />);
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  fireEvent.click(await screen.findByText('0000123456'));

  expect(await screen.findByRole('dialog', { name: 'KK 交付原始数据' })).toBeInTheDocument();
  expect(getKKDeliveryDetail).toHaveBeenCalledWith('raw-row-id', expect.any(AbortSignal));
  fireEvent.click(screen.getByRole('tab', { name: /字段列表/ }));
  expect((await screen.findAllByText('EXTRA_SOURCE_FIELD')).length).toBeGreaterThan(0);
  expect(screen.getByText('原始源字段')).toBeInTheDocument();
  expect(screen.getAllByText('_sync_run_id').length).toBeGreaterThan(0);
});

it('shows a loading status while the server query is pending', () => {
  listKKDeliveries.mockReturnValue(new Promise(() => {}));
  render(<KKDeliveriesPage />);
  expect(listKKDeliveries).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  expect(screen.getByRole('status')).toHaveTextContent('正在读取 KK 交付记录');
});
