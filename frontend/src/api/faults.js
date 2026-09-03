import { fetchAll, fetchPage, get, post } from './client';
export const listFaults = (filters, page, pageSize) => fetchPage('/api/faults', filters, page, pageSize);
export const faultStats = filters => get('/api/faults/stats', filters);
export const faultDetail = id => get('/api/faults/detail', { id });
export const lookupFaults = body => post('/api/faults/lookup', body);
export const faultsBySNS = body => post('/api/faults/by-sns', body);
export const allFaults = (filters, pageSize) => fetchAll('/api/faults', filters, pageSize);
