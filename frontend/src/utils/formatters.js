export function formatCell(value) { if (value === null || value === undefined || value === '') return '-'; return typeof value === 'object' ? JSON.stringify(value) : String(value); }
export function formatDate(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-'; }
export function formatBusinessDate(value) { const raw = String(value || '').trim(); return /^\d{8}$/.test(raw) ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6)}` : raw.slice(0, 10) || '-'; }
export function formatDateRange(start, end) { return start && end ? `${formatBusinessDate(start)} 至 ${formatBusinessDate(end)}` : '-'; }
export function formatNumber(value) { return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value || 0); }
export function csvCell(value) { const text = value === null || value === undefined ? '' : String(value); const safeText = /^[=+@]/.test(text) || /^-\D/.test(text) ? `'${text}` : text; return /[",\r\n]/.test(safeText) ? `"${safeText.replaceAll('"', '""')}"` : safeText; }
export function createCsv(rows, columns) { return [columns.map(column => csvCell(column.label)).join(','), ...rows.map(item => columns.map(column => csvCell(column.value ? column.value(item) : item[column.key])).join(','))].join('\r\n'); }
