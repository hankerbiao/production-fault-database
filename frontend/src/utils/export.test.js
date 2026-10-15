import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchPage } from '../api/client';
import { downloadCsv } from './export';

vi.mock('../api/client', () => ({ fetchPage: vi.fn() }));

const endpoint = '/api/views/ZSGV_ZSD124';
const filters = { salesOrder: 'SO-1' };
const options = { endpoint, filters, filename: 'orders.csv', columns: [{ key: 'id', label: 'ID' }] };

describe('CSV export', () => {
  let blobs;

  beforeEach(() => {
    vi.clearAllMocks();
    blobs = [];
    vi.spyOn(URL, 'createObjectURL').mockImplementation(blob => {
      blobs.push(blob);
      return 'blob:export';
    });
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  });

  it('exports all pages beyond the bulk API row limit', async () => {
    const total = 10001;
    const rows = Array.from({ length: total }, (_, id) => ({ id }));
    fetchPage.mockImplementation(async (_endpoint, _filters, page, pageSize) => ({
      items: rows.slice((page - 1) * pageSize, page * pageSize), total,
    }));
    await downloadCsv(options);
    expect(fetchPage).toHaveBeenCalledTimes(101);
    expect(fetchPage).toHaveBeenLastCalledWith(endpoint, filters, 101, 100);
    const csv = new TextDecoder().decode(await blobs[0].arrayBuffer());
    const exportedRows = csv.trim().split(/\r?\n/).slice(1);
    expect(exportedRows).toHaveLength(total);
    expect(new Set(exportedRows).size).toBe(total);
    expect(exportedRows.at(-1)).toContain('10000');
  });

  it('keeps ordinary exports paginated and preserves their filters', async () => {
    fetchPage
      .mockResolvedValueOnce({ items: [{ id: 'first' }, { id: 'second' }], total: 3 })
      .mockResolvedValueOnce({ items: [{ id: 'third' }], total: 3 });
    await downloadCsv({ ...options, pageSize: 2 });
    expect(fetchPage).toHaveBeenNthCalledWith(1, endpoint, filters, 1, 2);
    expect(fetchPage).toHaveBeenNthCalledWith(2, endpoint, filters, 2, 2);
    expect(blobs).toHaveLength(1);
  });
});
