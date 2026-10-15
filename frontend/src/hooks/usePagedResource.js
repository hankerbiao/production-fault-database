import { useCallback, useEffect, useRef, useState } from 'react';

export const PAGE_SIZE = 20;
export const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];

export function usePagedResource({ loadPage, loadStats, initialFilters = {}, initialStats = {}, pageSize = PAGE_SIZE, refreshToken = 0, autoLoad = true, onConnectionChange, onRefreshing }) {
  const [filters, setFilters] = useState(initialFilters);
  const [appliedFilters, setAppliedFilters] = useState(initialFilters);
  const [items, setItems] = useState([]); const [stats, setStats] = useState(initialStats); const [total, setTotal] = useState(0); const [hasMore, setHasMore] = useState(false); const [preview, setPreview] = useState(false); const [page, setPage] = useState(1); const [currentPageSize, setCurrentPageSize] = useState(pageSize); const [loading, setLoading] = useState(false); const [error, setError] = useState('');
  const requestId = useRef(0); const skipNextPageLoad = useRef(false); const firstPageEffect = useRef(true); const hasLoaded = useRef(autoLoad);
  const load = useCallback(async (nextPage = page, nextFilters = appliedFilters, includeStats = true) => {
    hasLoaded.current = true;
    const currentRequest = ++requestId.current;
    setLoading(true); onRefreshing?.(true);
    try { const [data, summary] = await Promise.all([loadPage(nextFilters, nextPage, currentPageSize), includeStats ? loadStats(nextFilters) : Promise.resolve(null)]); if (currentRequest !== requestId.current) return; setItems(data?.items || []); setTotal(Number(data?.total) || 0); setHasMore(Boolean(data?.hasMore)); setPreview(Boolean(data?.preview)); if (includeStats) setStats(summary || initialStats); setError(''); onConnectionChange?.(true); }
    catch { if (currentRequest !== requestId.current) return; setItems([]); setTotal(0); setHasMore(false); setPreview(false); if (includeStats) setStats(initialStats); setError('无法连接 MongoDB 网关'); onConnectionChange?.(false); }
    finally { if (currentRequest === requestId.current) { setLoading(false); onRefreshing?.(false); } }
  }, [appliedFilters, page, currentPageSize, loadPage, loadStats, initialStats, onConnectionChange, onRefreshing]);
  const loadCurrent = useCallback((nextPage, nextFilters, includeStats = true) => {
    setPage(nextPage); setFilters(nextFilters); setAppliedFilters(nextFilters); return load(nextPage, nextFilters, includeStats);
  }, [load]);
  useEffect(() => { if (hasLoaded.current) load(page, appliedFilters, true); }, [refreshToken]);
  useEffect(() => {
    if (firstPageEffect.current) { firstPageEffect.current = false; return; }
    if (!hasLoaded.current) return;
    if (skipNextPageLoad.current) { skipNextPageLoad.current = false; return; }
    load(page, appliedFilters, false);
  }, [page, currentPageSize]);
  const setPageSize = nextPageSize => {
    const next = Number(nextPageSize);
    if (!Number.isFinite(next) || next < 1 || next === currentPageSize) return;
    setPage(1);
    setCurrentPageSize(next);
  };
  return { filters, setFilters, appliedFilters, items, stats, total, hasMore, preview, page, setPage, pageSize: currentPageSize, setPageSize, loading, error, load, applyFilters: next => { skipNextPageLoad.current = true; return loadCurrent(1, next, true); }, reset: next => { skipNextPageLoad.current = true; return loadCurrent(1, next, true); } };
}
