import { get } from './client';

export function machineConfig(sn, source = '5000') {
  return get(`/api/machines/config/${source}`, { sn });
}
