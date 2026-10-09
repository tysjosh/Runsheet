"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../services/api";
import { isRateLimited, rateLimitMessage } from "../../services/portalApi";

/** Customer-facing text for a failed read. */
export function portalErrorMessage(
  error: unknown,
  { notFound, fallback }: { notFound?: string; fallback: string },
): string {
  if (isRateLimited(error)) return rateLimitMessage(error);
  if (notFound && error instanceof ApiError && error.status === 404) {
    return notFound;
  }
  return fallback;
}

/**
 * Load one portal resource with loading and error state. `load` should be
 * memoized by the caller (or depend only on `deps`).
 */
export function usePortalData<T>(
  load: () => Promise<T>,
  deps: readonly unknown[],
): {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  // Callers pass the inputs of `load` as deps.
  const run = useCallback(() => {
    seq.current += 1;
    const mine = seq.current;
    setLoading(true);
    setError(null);
    load()
      .then((value) => {
        if (mine === seq.current) setData(value);
      })
      .catch((err: unknown) => {
        if (mine === seq.current) setError(err);
      })
      .finally(() => {
        if (mine === seq.current) setLoading(false);
      });
  }, deps);

  useEffect(() => {
    run();
  }, [run]);

  return { data, error, loading, reload: run };
}
