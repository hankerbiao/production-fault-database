import { ApiError, fetchPage, queryString, request } from './client';

export const listKKDeliveries = (filters, page, pageSize) =>
  fetchPage('/api/kk-sales-order-deliveries', filters, page, pageSize);

export const downloadKKDeliveries = async filters => {
  const response = await fetch(`/api/kk-sales-order-deliveries/export?${queryString(filters)}`, {
    headers: { Accept: 'text/csv' },
  });
  if (!response.ok) {
    let message = `导出失败（${response.status}）`;
    try {
      const text = await response.text();
      const payload = text ? JSON.parse(text) : null;
      if (payload?.error) message = payload.error;
      else if (text) message = text;
    } catch { /* Keep the HTTP status message. */ }
    throw new ApiError(message, response.status);
  }
  const blob = await response.blob();
  const disposition = response.headers?.get?.('Content-Disposition') || '';
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || 'kk-sales-order-deliveries.csv';
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
};

export const getKKDeliveryDetail = (id, signal) =>
  request(`/api/kk-sales-order-deliveries/detail?${queryString({ id })}`, { signal });
