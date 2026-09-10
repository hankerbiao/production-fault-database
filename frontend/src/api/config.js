import { get } from './client';

export const serviceConfig = () => get('/api/config');
