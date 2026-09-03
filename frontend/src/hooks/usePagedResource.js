import { useCallback, useEffect, useState } from 'react';

export function usePagedResource({ loadPage, loadStats, initialFilters = {}, initialStats = {}, pageSize = 20, refreshToken = 0, onConnectionChange, onRefreshing }) {
  const [filters, setFilters] = useState(initialFilters);
  const [items, setItems] = useState([]); const [stats, setStats] = useState(initialStats); const [total, setTotal] = useState(0); const [page, setPage] = useState(1); const [loading, setLoading] = useState(false); const [error, setError] = useState('');
  const load = useCallback(async (nextPage = page, nextFilters = filters) => {
    setLoading(true); onRefreshing?.(true);
    try { const [data, summary] = await Promise.all([loadPage(nextFilters, nextPage, pageSize), loadStats(nextFilters)]); setItems(data?.items || []); setTotal(Number(data?.total) || 0); setStats(summary || initialStats); setError(''); onConnectionChange?.(true); }
    catch { setItems([]); setTotal(0); setStats(initialStats); setError('无法连接 MongoDB 网关'); onConnectionChange?.(false); }
    finally { setLoading(false); onRefreshing?.(false); }
  }, [filters, page, pageSize, loadPage, loadStats, initialStats, onConnectionChange, onRefreshing]);
  useEffect(() => { load(); }, [page, refreshToken]);
  return { filters, setFilters, items, stats, total, page, setPage, loading, error, load, reset: next => { setFilters(next); setPage(1); load(1, next); } };
}
