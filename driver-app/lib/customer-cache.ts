/**
 * On-device retention of customer contact data (R15.7).
 *
 * The driver surface caches three PII values so a stop can be worked in a dead
 * zone: the customer display name, the customer phone number, and the
 * destination address. Requirement 15.7 bounds how long the phone may hold them:
 *
 *  - **Only for currently assigned orders.** {@link retainOnlyAssignedOrders} is
 *    called with the driver's full assigned set; anything else is deleted on the
 *    spot. An order that is revoked or reassigned takes its customer data with it.
 *  - **Deleted within 24 hours of a terminal status.** The first time an order is
 *    observed `delivered`, `failed`, or `cancelled`, the clock starts;
 *    {@link sweepCustomerCache} — run on app foreground — deletes the entry once
 *    24 hours have elapsed. A read past the deadline returns nothing and deletes
 *    the row, so an expired value cannot be rendered even if no sweep has run.
 *  - **Gone at sign-out.** The eraser is registered against the `customer-cache`
 *    domain of `lib/session.ts`, which Requirement 15.5 makes sign-out call.
 *
 * Storage is a dedicated `react-native-mmkv` instance, separate from the query
 * cache so a cache-wide clear cannot resurrect or orphan these rows. No token
 * ever lands here — credentials live in `expo-secure-store` (R15.3). Nothing in
 * this module logs a cached value.
 *
 * Requirements: 15.7, 15.5
 */

import { MMKV } from 'react-native-mmkv';

import { registerSessionPurgeHandler } from './session';
import { isTerminalOrderStatus, type FuelOrder } from '@/types/order';

/** Requirement 15.7's deadline, measured from the first terminal observation. */
export const TERMINAL_RETENTION_MS = 24 * 60 * 60 * 1000;

const STORAGE_ID = 'runsheet-customer-cache';
const KEY_PREFIX = 'order:';

/** The cached values, all three of them. */
export interface CustomerContact {
  customerName: string;
  customerPhone: string | null;
  destinationAddress: string | null;
}

interface CachedEntry extends CustomerContact {
  status: string;
  /** Epoch ms of the first terminal observation, or `null` while not terminal. */
  terminalSince: number | null;
}

/**
 * The slice of key-value storage this module needs. Injectable so the retention
 * rules can be tested without a native module.
 */
export interface CustomerCacheStore {
  getString(key: string): string | undefined;
  set(key: string, value: string): void;
  delete(key: string): void;
  getAllKeys(): string[];
}

function createMemoryStore(): CustomerCacheStore {
  const map = new Map<string, string>();
  return {
    getString: (key) => map.get(key),
    set: (key, value) => {
      map.set(key, value);
    },
    delete: (key) => {
      map.delete(key);
    },
    getAllKeys: () => Array.from(map.keys()),
  };
}

let store: CustomerCacheStore | null = null;
let clock: () => number = () => Date.now();

function resolveStore(): CustomerCacheStore {
  if (!store) {
    try {
      store = new MMKV({ id: STORAGE_ID });
    } catch {
      // No native MMKV in this environment (Jest, web preview). An in-memory
      // store keeps the retention rules identical; only durability is lost,
      // which errs toward holding *less* customer data, never more.
      store = createMemoryStore();
    }
  }
  return store;
}

/** Override the store and the clock. Tests only. */
export function configureCustomerCache(next: {
  store?: CustomerCacheStore | null;
  now?: (() => number) | null;
}): void {
  if (next.store !== undefined) {
    store = next.store;
  }
  if (next.now !== undefined) {
    clock = next.now ?? (() => Date.now());
  }
}

function keyOf(orderId: string): string {
  return `${KEY_PREFIX}${orderId}`;
}

function cachedKeys(): string[] {
  return resolveStore()
    .getAllKeys()
    .filter((key) => key.startsWith(KEY_PREFIX));
}

function readEntry(orderId: string): CachedEntry | null {
  const raw = resolveStore().getString(keyOf(orderId));
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Partial<CachedEntry>;
    if (typeof parsed.customerName !== 'string' || typeof parsed.status !== 'string') {
      return null;
    }
    return {
      customerName: parsed.customerName,
      customerPhone: typeof parsed.customerPhone === 'string' ? parsed.customerPhone : null,
      destinationAddress:
        typeof parsed.destinationAddress === 'string' ? parsed.destinationAddress : null,
      status: parsed.status,
      terminalSince: typeof parsed.terminalSince === 'number' ? parsed.terminalSince : null,
    };
  } catch {
    return null;
  }
}

function isExpired(entry: CachedEntry, now: number): boolean {
  return entry.terminalSince !== null && now - entry.terminalSince >= TERMINAL_RETENTION_MS;
}

/** The order fields this cache reads. A whole {@link FuelOrder} satisfies it. */
export type CacheableOrder = Pick<
  FuelOrder,
  'order_id' | 'status' | 'customer_name' | 'customer_phone' | 'destination'
>;

/**
 * Record the customer data of orders assigned to the driver.
 *
 * Called after any work-list or order-detail read. The terminal clock is started
 * on the first terminal observation and never restarted, so a repeated read of a
 * delivered order cannot extend its retention.
 */
export function cacheAssignedOrders(orders: CacheableOrder[]): void {
  const now = clock();
  for (const order of orders) {
    const existing = readEntry(order.order_id);
    const terminal = isTerminalOrderStatus(order.status);
    const entry: CachedEntry = {
      customerName: order.customer_name,
      customerPhone: order.customer_phone ?? null,
      destinationAddress: order.destination?.address ?? null,
      status: order.status,
      terminalSince: terminal ? (existing?.terminalSince ?? now) : null,
    };
    if (isExpired(entry, now)) {
      resolveStore().delete(keyOf(order.order_id));
      continue;
    }
    resolveStore().set(keyOf(order.order_id), JSON.stringify(entry));
  }
}

/**
 * Read the cached contact for an order, or `null` when nothing may be shown.
 *
 * An entry past its 24-hour deadline is deleted here rather than returned, so
 * the deadline holds even if no sweep has run since the app was launched.
 */
export function readCustomerContact(orderId: string): CustomerContact | null {
  const entry = readEntry(orderId);
  if (!entry) {
    return null;
  }
  if (isExpired(entry, clock())) {
    resolveStore().delete(keyOf(orderId));
    return null;
  }
  return {
    customerName: entry.customerName,
    customerPhone: entry.customerPhone,
    destinationAddress: entry.destinationAddress,
  };
}

/**
 * Delete every cached entry whose order is not in the driver's assigned set, and
 * every entry past its 24-hour terminal deadline (R15.7).
 *
 * Call with the **unfiltered** assigned order ids. Passing a filtered subset is
 * safe but wasteful: the dropped entries are simply re-fetched.
 *
 * @returns the number of entries deleted.
 */
export function retainOnlyAssignedOrders(assignedOrderIds: Iterable<string>): number {
  const assigned = new Set(assignedOrderIds);
  const now = clock();
  let deleted = 0;
  for (const key of cachedKeys()) {
    const orderId = key.slice(KEY_PREFIX.length);
    const entry = readEntry(orderId);
    if (!entry || !assigned.has(orderId) || isExpired(entry, now)) {
      resolveStore().delete(key);
      deleted += 1;
    }
  }
  return deleted;
}

/**
 * Delete every entry past its 24-hour terminal deadline. Run on app foreground.
 *
 * @returns the number of entries deleted.
 */
export function sweepCustomerCache(): number {
  const now = clock();
  let deleted = 0;
  for (const key of cachedKeys()) {
    const entry = readEntry(key.slice(KEY_PREFIX.length));
    if (!entry || isExpired(entry, now)) {
      resolveStore().delete(key);
      deleted += 1;
    }
  }
  return deleted;
}

/** Delete every cached customer value. Registered as the sign-out eraser (R15.5). */
export function purgeCustomerCache(): void {
  for (const key of cachedKeys()) {
    resolveStore().delete(key);
  }
}

registerSessionPurgeHandler('customer-cache', purgeCustomerCache);
