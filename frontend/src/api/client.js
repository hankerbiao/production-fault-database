const jsonHeaders = { Accept: 'application/json', 'Content-Type': 'application/json' };

export class ApiError extends Error {
  constructor(message, status = 0, payload = null) { super(message); this.name = 'ApiError'; this.status = status; this.payload = payload; }
}

async function parseResponse(response) {
  let payload = null;
  if (typeof response.text === 'function') {
    const text = await response.text();
    try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
  } else if (typeof response.json === 'function') {
    try { payload = await response.json(); } catch { payload = null; }
  }
  if (!response.ok) throw new ApiError(payload?.error || `请求失败（${response.status}）`, response.status, payload);
  return payload;
}

export function queryString(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== null && value !== '') query.set(key, String(value)); });
  return query.toString();
}

export async function request(path, options = {}) {
  const headers = options.body && typeof options.body !== 'string' ? jsonHeaders : { Accept: 'application/json', ...(options.headers || {}) };
  const body = options.body && typeof options.body !== 'string' ? JSON.stringify(options.body) : options.body;
  return parseResponse(await fetch(path, { ...options, headers, body }));
}

export function get(path, params) { const qs = queryString(params); return request(qs ? `${path}?${qs}` : path); }
export function post(path, body) { return request(path, { method: 'POST', body }); }
export async function fetchPage(path, filters, page, pageSize) { return get(path, { ...filters, page, pageSize }); }
export async function fetchAll(path, filters, pageSize = 10000) { return get(path, { ...filters, all: true, page: 1, pageSize }); }
