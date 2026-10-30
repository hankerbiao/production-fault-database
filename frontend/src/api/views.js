import { fetchPage, get } from './client';
export const listView = (viewId, filters, page, pageSize, preview = false) => fetchPage(`/api/views/${viewId}`, { ...filters, preview: preview || undefined }, page, pageSize);
export const viewStats = (viewId, filters) => get(`/api/views/${viewId}/stats`, filters);
export const viewDetail = (viewId, id) => get(`/api/views/${viewId}/detail`, { id });
export const bomCompanyOptions = () => get('/api/views/ZSGV_ZSD124/options');
