"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PortalList } from "../../services/portalApi";

/**
 * Cursor pagination for a portal list: the first page loads on mount (and
 * whenever `deps` change); `loadMore` appends the next page.
 */
export function usePagedList<T>(
  fetchPage: (cursor: string | null) => Promise<PortalList<T>>,
  deps: readonly unknown[],
): {
  items: T[];
  loading: boolean;
  loadingMore: boolean;
  error: unknown;
  hasMore: boolean;
  loadMore: () => void;
  reload: () => void;
  refresh: () => Promise<void>;
} {
  const [items, setItems] = useState<T[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const seq = useRef(0);

  // Callers pass the inputs of `fetchPage` as deps.
  const reload = useCallback(() => {
    seq.current += 1;
    const mine = seq.current;
    setLoading(true);
    setError(null);
    fetchPage(null)
      .then((page) => {
        if (mine !== seq.current) return;
        setItems(page.data);
        setCursor(page.next_cursor);
      })
      .catch((err: unknown) => {
        if (mine === seq.current) setError(err);
      })
      .finally(() => {
        if (mine === seq.current) setLoading(false);
      });
  }, deps);

  useEffect(() => {
    reload();
  }, [reload]);
  // Re-fetch the first page in place: `loading` stays false, so the list
  // stays mounted and focus can stay inside it. Rejects on failure and leaves
  // `items` and `error` unchanged; the caller reports the error.
  const refresh = useCallback(async () => {
    seq.current += 1;
    const mine = seq.current;
    const page = await fetchPage(null);
    if (mine !== seq.current) return;
    setItems(page.data);
    setCursor(page.next_cursor);
    setError(null);
  }, [fetchPage]);

  const loadMore = useCallback(() => {
    if (!cursor || loadingMore) return;
    const mine = seq.current;
    setLoadingMore(true);
    setError(null);
    fetchPage(cursor)
      .then((page) => {
        if (mine !== seq.current) return;
        setItems((prev) => [...prev, ...page.data]);
        setCursor(page.next_cursor);
      })
      .catch((err: unknown) => {
        if (mine === seq.current) setError(err);
      })
      .finally(() => {
        if (mine === seq.current) setLoadingMore(false);
      });
  }, [cursor, loadingMore, fetchPage]);

  return {
    items,
    loading,
    loadingMore,
    error,
    hasMore: cursor !== null,
    loadMore,
    reload,
    refresh,
  };
}
