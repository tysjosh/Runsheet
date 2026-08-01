/**
 * `lib/customer-cache.ts` — on-device customer-data retention.
 *
 * **Validates: Requirements 15.7, 15.5**
 */

import {
  TERMINAL_RETENTION_MS,
  cacheAssignedOrders,
  configureCustomerCache,
  purgeCustomerCache,
  readCustomerContact,
  retainOnlyAssignedOrders,
  sweepCustomerCache,
  type CacheableOrder,
  type CustomerCacheStore,
} from '@/lib/customer-cache';
import type { OrderStatus } from '@/types/order';

function memoryStore(): CustomerCacheStore {
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

function order(orderId: string, status: OrderStatus): CacheableOrder {
  return {
    order_id: orderId,
    status,
    customer_name: `Customer ${orderId}`,
    customer_phone: '+15125550143',
    destination: { address: '1200 Ship Channel Rd, Houston TX', lat: 29.76, lon: -95.37 },
  };
}

let now = Date.parse('2026-05-01T08:00:00Z');

beforeEach(() => {
  now = Date.parse('2026-05-01T08:00:00Z');
  configureCustomerCache({ store: memoryStore(), now: () => now });
});

afterEach(() => {
  configureCustomerCache({ store: null, now: null });
});

describe('caching assigned orders', () => {
  it('retains the name, the phone, and the destination address', () => {
    cacheAssignedOrders([order('ord_1', 'dispatched')]);

    expect(readCustomerContact('ord_1')).toEqual({
      customerName: 'Customer ord_1',
      customerPhone: '+15125550143',
      destinationAddress: '1200 Ship Channel Rd, Houston TX',
    });
  });

  it('records an absent phone as null rather than inventing one', () => {
    cacheAssignedOrders([{ ...order('ord_2', 'in_transit'), customer_phone: null }]);

    expect(readCustomerContact('ord_2')?.customerPhone).toBeNull();
  });

  it('returns nothing for an order that was never cached', () => {
    expect(readCustomerContact('ord_missing')).toBeNull();
  });
});

describe('retention is limited to currently assigned orders (R15.7)', () => {
  it('deletes the customer data of an order that is no longer assigned', () => {
    cacheAssignedOrders([order('ord_1', 'dispatched'), order('ord_2', 'dispatched')]);

    const deleted = retainOnlyAssignedOrders(['ord_1']);

    expect(deleted).toBe(1);
    expect(readCustomerContact('ord_1')).not.toBeNull();
    expect(readCustomerContact('ord_2')).toBeNull();
  });
});

describe('terminal orders are deleted within 24 hours (R15.7)', () => {
  it('keeps the data while the deadline has not passed', () => {
    cacheAssignedOrders([order('ord_1', 'delivered')]);

    now += TERMINAL_RETENTION_MS - 1000;

    expect(sweepCustomerCache()).toBe(0);
    expect(readCustomerContact('ord_1')).not.toBeNull();
  });

  it.each<OrderStatus>(['delivered', 'failed', 'cancelled'])(
    'deletes the data once 24 hours have passed since %s',
    (status) => {
      cacheAssignedOrders([order('ord_1', status)]);

      now += TERMINAL_RETENTION_MS;

      expect(sweepCustomerCache()).toBe(1);
      expect(readCustomerContact('ord_1')).toBeNull();
    },
  );

  it('withholds and deletes an expired entry on read even without a sweep', () => {
    cacheAssignedOrders([order('ord_1', 'delivered')]);

    now += TERMINAL_RETENTION_MS + 1;

    expect(readCustomerContact('ord_1')).toBeNull();
    expect(sweepCustomerCache()).toBe(0);
  });

  it('does not restart the deadline when the same terminal order is re-read', () => {
    cacheAssignedOrders([order('ord_1', 'delivered')]);

    now += TERMINAL_RETENTION_MS - 60_000;
    cacheAssignedOrders([order('ord_1', 'delivered')]);
    now += 60_000;

    expect(readCustomerContact('ord_1')).toBeNull();
  });

  it('starts no deadline for an order that can still resume', () => {
    cacheAssignedOrders([order('ord_1', 'on_hold')]);

    now += TERMINAL_RETENTION_MS * 3;

    expect(sweepCustomerCache()).toBe(0);
    expect(readCustomerContact('ord_1')).not.toBeNull();
  });
});

describe('sign-out erasure (R15.5)', () => {
  it('deletes every cached customer value', () => {
    cacheAssignedOrders([order('ord_1', 'dispatched'), order('ord_2', 'in_transit')]);

    purgeCustomerCache();

    expect(readCustomerContact('ord_1')).toBeNull();
    expect(readCustomerContact('ord_2')).toBeNull();
  });
});
