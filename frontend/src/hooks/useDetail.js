import { useCallback, useState } from 'react';
export function useDetail(loadDetail, errorMessage) {
  const [detail, setDetail] = useState(null); const [loading, setLoading] = useState(false);
  const open = useCallback(async id => { setLoading(true); setDetail(null); try { setDetail(await loadDetail(id)); } catch { setDetail({ error: errorMessage }); } finally { setLoading(false); } }, [loadDetail, errorMessage]);
  const close = useCallback(() => { setDetail(null); setLoading(false); }, []);
  return { detail, loading, open, close };
}
