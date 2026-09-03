import { fetchPage } from '../api/client';
import { createCsv } from './formatters';

export async function downloadCsv({ endpoint, filters, filename, columns, pageSize = 100 }) {
  let page = 1;
  let total = Infinity;
  const rows = [];
  while (rows.length < total) {
    const data = await fetchPage(endpoint, filters, page, pageSize);
    const items = Array.isArray(data?.items) ? data.items : [];
    total = Number.isFinite(Number(data?.total)) ? Number(data.total) : rows.length + items.length;
    rows.push(...items);
    if (items.length === 0 || items.length < pageSize) break;
    page += 1;
  }
  const csv = createCsv(rows, columns);
  const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
