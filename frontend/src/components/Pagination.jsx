import { ChevronLeft, ChevronRight } from 'lucide-react';
import { PAGE_SIZE_OPTIONS } from '../hooks/usePagedResource';
export function Pagination({ page, total, pageSize, setPage, setPageSize, bilingual = false, preview = false, hasMore = false, itemUnit = '条' }) {
  const shown = preview ? (page - 1) * pageSize + total : Math.min(page * pageSize, total);
  const first = total ? (page - 1) * pageSize + 1 : 0;
  const nextDisabled = preview ? !hasMore : page * pageSize >= total;
  const totalPages = preview ? (hasMore ? null : Math.max(1, page)) : Math.max(1, Math.ceil(total / pageSize));
  const pageLabel = totalPages ? `${bilingual ? '第 / Page ' : '第 '}${page} / ${totalPages} ${bilingual ? '页 / pages' : '页'}` : `${bilingual ? '第 / Page ' : '第 '}${page} / ${bilingual ? '多页 / more' : '多页'}`;
  return <div className="pagination"><span>{total ? `${bilingual ? '显示 / Showing ' : '显示 '}${first}-${shown} ${bilingual ? '条 / records' : itemUnit}` : (bilingual ? '暂无记录 / No records' : '暂无记录')}</span><div><label className="pagination-size"><span>{bilingual ? '每页 / Page size' : '每页'}</span><select aria-label={bilingual ? '每页数量 / Page size' : '每页数量'} value={pageSize} onChange={event => setPageSize?.(event.target.value)}>{PAGE_SIZE_OPTIONS.map(size => <option key={size} value={size}>{size}</option>)}</select></label><button disabled={page === 1} onClick={() => setPage(page - 1)} title={bilingual ? '上一页 / Previous page' : '上一页'}><ChevronLeft size={17} /></button><b aria-label={bilingual ? '页码 / Page' : '页码'}>{pageLabel}</b><button disabled={nextDisabled} onClick={() => setPage(page + 1)} title={bilingual ? '下一页 / Next page' : '下一页'}><ChevronRight size={17} /></button></div></div>;
}
