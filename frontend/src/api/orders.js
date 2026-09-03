import { fetchAll, fetchPage, get } from './client';
export const listOrders = (filters, page, pageSize) => fetchPage('/api/orders', filters, page, pageSize);
export const allOrders = (filters, pageSize) => fetchAll('/api/orders', filters, pageSize);
export const orderStats = filters => get('/api/orders/stats', filters);
export const orderModels = keyword => get('/api/orders/models', keyword ? { keyword } : undefined);
export const orderDetail = id => get('/api/orders/detail', { id });
