import { fetchAll, fetchPage, get } from './client';
export const listView = (viewId, filters, page, pageSize) => fetchPage(`/api/views/${viewId}`, filters, page, pageSize);
export const allView = (viewId, filters, pageSize) => fetchAll(`/api/views/${viewId}`, filters, pageSize);
export const viewStats = (viewId, filters) => get(`/api/views/${viewId}/stats`, filters);
export const viewDetail = (viewId, id) => get(`/api/views/${viewId}/detail`, { id });
export const viewStreamUrl = (viewId, filters) => { const qs = new URLSearchParams(filters || {}).toString(); return `/api/views/${viewId}/stream${qs ? `?${qs}` : ''}`; };
