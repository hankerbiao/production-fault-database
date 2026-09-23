import { describe, expect, it } from 'vitest';
import { createCsv } from './formatters';
import { repairExportColumns } from './repairExport';

describe('repair export columns', () => {
  it('includes every known field and any extra raw field', () => {
    const columns = repairExportColumns([{ raw: { PCODE: 'PC-1', RECORD01REPAIRM: '37001343', CUSTOM_FIELD: { note: '额外' }, _id: 'abc' } }]);
    const labels = columns.map(column => column.label);
    expect(labels[0]).toBe('集团');
    expect(labels).toContain('非关键件物料序号');
    expect(labels).toContain('文档主键');
    expect(labels.at(-1)).toBe('CUSTOM_FIELD');
    const csv = createCsv([{ raw: { PCODE: 'PC-1', RECORD01REPAIRM: '37001343', CUSTOM_FIELD: { note: '额外' }, _id: 'abc' } }], columns);
    expect(csv).toContain('PC-1');
    expect(csv).toContain('37001343');
    expect(csv).toContain('"{""note"":""额外""}"');
    expect(csv).toContain('abc');
  });
});
