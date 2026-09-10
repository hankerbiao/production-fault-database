import { get, post } from './client';
export const syncStatus = () => get('/api/sync/status');
export const startSync = () => post('/api/sync/incremental');
export const dataStatus = () => get('/api/data-status');
export const syncTasks = () => get('/api/sync/tasks');
export const syncRuns = (limit = 20) => get('/api/sync/runs', { limit });
export const syncRun = id => get(`/api/sync/runs/${encodeURIComponent(id)}`);
export const createSyncRun = body => post('/api/sync/runs', body);
export const retrySyncRun = id => post(`/api/sync/runs/${encodeURIComponent(id)}/retry`);
