import { ChevronLeft, ChevronRight } from 'lucide-react';
export function Pagination({ page, total, pageSize, setPage, bilingual = false, preview = false, hasMore = false }) {
  const shown = preview ? (page - 1) * pageSize + total : Math.min(page * pageSize, total);
  const first = total ? (page - 1) * pageSize + 1 : 0;
  const nextDisabled = preview ? !hasMore : page * pageSize >= total;
  return <div className="pagination"><span>{total ? `${bilingual ? '显示 / Showing ' : '显示 '}${first}-${shown} ${bilingual ? '条 / records' : '条'}` : (bilingual ? '暂无记录 / No records' : '暂无记录')}</span><div><button disabled={page === 1} onClick={() => setPage(page - 1)} title={bilingual ? '上一页 / Previous page' : '上一页'}><ChevronLeft size={17} /></button><b>{page}</b><button disabled={nextDisabled} onClick={() => setPage(page + 1)} title={bilingual ? '下一页 / Next page' : '下一页'}><ChevronRight size={17} /></button></div></div>;
}
