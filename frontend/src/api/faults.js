import { fetchPage, get } from './client';
export const listFaults = (filters, page, pageSize) => fetchPage('/api/faults', filters, page, pageSize);
export const faultStats = filters => get('/api/faults/stats', filters);
export const faultDetail = id => get('/api/faults/detail', { id });
