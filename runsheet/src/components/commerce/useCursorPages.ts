"use client";

/**
 * Page numbers over a cursor-paginated list (Billing invoices and payments,
 * task 3.4), so the list can use `DataTable`'s pager. Page n is read with the
 * cursor page n − 1 returned; the next page exists while `has_more` is true.
 * Changing `resetKey` (the filters) goes back to page 1 in the same render,
 * so no read goes out with new filters and an old cursor.
 */
import { useCallback, useRef, useState } from "react";

export function useCursorPages(resetKey: string) {
  const [key, setKey] = useState(resetKey);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  // cursors[i] is the cursor that reads page i + 1 (page 1 has none).
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const pageRef = useRef(page);
  pageRef.current = page;

  let current = page;
  if (key !== resetKey) {
    // Derived-state reset during render (React re-renders before effects).
    setKey(resetKey);
    setPage(1);
    setHasMore(false);
    setCursors([null]);
    current = 1;
  }

  /** Cursor for the current page. */
  const cursor = current === 1 ? null : (cursors[current - 1] ?? null);

  /** Record what page `forPage` returned. */
  const received = useCallback(
    (forPage: number, next: { cursor: string | null; has_more: boolean }) => {
      if (forPage !== pageRef.current) return;
      const more = Boolean(next.has_more && next.cursor);
      setHasMore(more);
      if (more)
        setCursors((prev) => {
          const copy = prev.slice(0, forPage);
          copy[forPage] = next.cursor;
          return copy;
        });
    },
    [],
  );

  const totalPages = hasMore ? current + 1 : current;
  const goTo = useCallback(
    (p: number) => {
      if (p >= 1 && p <= cursors.length) setPage(p);
    },
    [cursors.length],
  );

  return { page: current, cursor, totalPages, received, goTo };
}
