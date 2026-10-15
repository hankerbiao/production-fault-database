import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchPage, get } from '../api/client';
import { downloadCsv } from './export';

vi.mock('../api/client', () => ({ fetchPage: vi.fn(), get: vi.fn() }));

const endpoint = '/api/views/ZSGV_ZSD46N_SO_KK';
const allEndpoint = `${endpoint}/all`;
const filters = { salesOrder: 'SO-1' };
const options = { endpoint, allEndpoint, filters, filename: 'orders.csv', columns: [{ key: 'id', label: 'ID' }] };

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

  it('reads a complete all response once without paging it', async () => {
    get.mockResolvedValue({ items: [{ id: 'first' }, { id: 'second' }], total: 2 });
    await downloadCsv(options);
    expect(get).toHaveBeenCalledExactlyOnceWith(allEndpoint, filters);
    expect(fetchPage).not.toHaveBeenCalled();
    expect(blobs).toHaveLength(1);
  });

  it('falls back to pages when the all response hits the 100000 row limit', async () => {
    const total = 100001;
    const rows = Array.from({ length: total }, (_, id) => ({ id }));
    get.mockResolvedValue({ items: rows.slice(0, 100000), total });
    fetchPage.mockImplementation(async (_endpoint, _filters, page, pageSize) => ({
      items: rows.slice((page - 1) * pageSize, page * pageSize), total,
    }));
    await downloadCsv(options);
    expect(get).toHaveBeenCalledOnce();
    expect(fetchPage).toHaveBeenCalledTimes(1001);
    expect(fetchPage).toHaveBeenLastCalledWith(endpoint, filters, 1001, 100);
    const csv = new TextDecoder().decode(await blobs[0].arrayBuffer());
    const exportedRows = csv.trim().split(/\r?\n/).slice(1);
    expect(exportedRows).toHaveLength(total);
    expect(new Set(exportedRows).size).toBe(total);
    expect(exportedRows.at(-1)).toContain('100000');
  });

  it('keeps ordinary exports paginated and preserves their filters', async () => {
    fetchPage
      .mockResolvedValueOnce({ items: [{ id: 'first' }, { id: 'second' }], total: 3 })
      .mockResolvedValueOnce({ items: [{ id: 'third' }], total: 3 });
    await downloadCsv({ ...options, allEndpoint: undefined, pageSize: 2 });
    expect(get).not.toHaveBeenCalled();
    expect(fetchPage).toHaveBeenNthCalledWith(1, endpoint, filters, 1, 2);
    expect(fetchPage).toHaveBeenNthCalledWith(2, endpoint, filters, 2, 2);
    expect(blobs).toHaveLength(1);
  });
});
