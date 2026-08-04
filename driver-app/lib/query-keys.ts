/**
 * The ONE query-key registry for the Runsheet driver app (R16.14).
 *
 * The donor application exported `queryKeys` from three separate modules, which
 * is how an invalidation silently misses a cache. This module is the single
 * export, and it declares exactly six keys:
 *
 *   `['work', filters]`      the assigned-work list
 *   `['work', orderId]`      one order's detail
 *   `['me']`                 driver identity + duty status
 *   `['messages', workRef]`  one dispatch thread
 *   `['hos']`                Hours-of-Service advisory (Phase 2 screen)
 *   `['dqf']`                driver qualification summary (Phase 2 screen)
 *
 * Both work keys share the `'work'` scope on purpose: `invalidateQueries({
 * queryKey: [WORK_SCOPE] })` reaches the list and every order detail in one
 * call, which is what every realtime event and every push does — an
 * invalidation, never a state write (R14.9, R9.11).
 *
 * Requirements: 16.14
 */

import type { OrderStatus } from '@/types/order';

/** Shared prefix of both work keys. Invalidate `[WORK_SCOPE]` to reach both. */
export const WORK_SCOPE = 'work';

/** Server-side filters on the assigned-work list (R3.3, R3.5). */
export interface WorkFilters {
  /** Defaults server-side to `dispatched` + `in_transit` when omitted. */
  statuses?: OrderStatus[];
  /** ISO 8601 lower bound on `delivery_window_start`. */
  windowStart?: string;
  /** ISO 8601 upper bound on `delivery_window_start`. */
  windowEnd?: string;
  page?: number;
  size?: number;
}

/**
 * Drop absent fields and sort `statuses`, so two filter objects that mean the
 * same query hash to the same key and share one cache entry.
 */
function normalizeWorkFilters(filters: WorkFilters): WorkFilters {
  const normalized: WorkFilters = {};
  if (filters.statuses && filters.statuses.length > 0) {
    normalized.statuses = [...filters.statuses].sort();
  }
  if (filters.windowStart !== undefined) {
    normalized.windowStart = filters.windowStart;
  }
  if (filters.windowEnd !== undefined) {
    normalized.windowEnd = filters.windowEnd;
  }
  if (filters.page !== undefined) {
    normalized.page = filters.page;
  }
  if (filters.size !== undefined) {
    normalized.size = filters.size;
  }
  return normalized;
}

export const queryKeys = {
  /** `['work', filters]` — the assigned-work list. */
  work: (filters: WorkFilters = {}) => [WORK_SCOPE, normalizeWorkFilters(filters)] as const,
  /** `['work', orderId]` — one order's detail. */
  order: (orderId: string) => [WORK_SCOPE, orderId] as const,
  /** `['me']` — driver identity, assigned truck, duty status. */
  me: () => ['me'] as const,
  /** `['messages', workRef]` — one dispatch thread, keyed by order or run ref. */
  messages: (workRef: string) => ['messages', workRef] as const,
  /** `['hos']` — the advisory Hours-of-Service figures. */
  hos: () => ['hos'] as const,
  /** `['dqf']` — the driver qualification file summary. */
  dqf: () => ['dqf'] as const,
} as const;

export type WorkQueryKey = ReturnType<typeof queryKeys.work>;
export type OrderQueryKey = ReturnType<typeof queryKeys.order>;

export type DriverQueryKey =
  | WorkQueryKey
  | OrderQueryKey
  | ReturnType<typeof queryKeys.me>
  | ReturnType<typeof queryKeys.messages>
  | ReturnType<typeof queryKeys.hos>
  | ReturnType<typeof queryKeys.dqf>;
