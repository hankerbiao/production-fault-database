import { get, post } from './client';
export const syncStatus = () => get('/api/sync/status');
export const startSync = () => post('/api/sync/incremental');
export const dataStatus = () => get('/api/data-status');
