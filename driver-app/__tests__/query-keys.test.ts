/**
 * `lib/query-keys.ts` — the one query-key registry.
 *
 * **Validates: Requirements 16.14**
 */

import * as registry from '@/lib/query-keys';
import { WORK_SCOPE, queryKeys } from '@/lib/query-keys';

describe('the registry is singular and declares exactly the six keys', () => {
  it('exports one registry object', () => {
    const exportedRegistries = Object.entries(registry).filter(
      ([name, value]) => /queryKeys/i.test(name) && typeof value === 'object',
    );
    expect(exportedRegistries.map(([name]) => name)).toEqual(['queryKeys']);
  });

  it('declares six key factories and no more', () => {
    expect(Object.keys(queryKeys).sort()).toEqual([
      'dqf',
      'hos',
      'me',
      'messages',
      'order',
      'work',
    ]);
  });

  it('produces the declared key shapes', () => {
    expect(queryKeys.work()).toEqual(['work', {}]);
    expect(queryKeys.order('ord_4821')).toEqual(['work', 'ord_4821']);
    expect(queryKeys.me()).toEqual(['me']);
    expect(queryKeys.messages('ord_4821')).toEqual(['messages', 'ord_4821']);
    expect(queryKeys.hos()).toEqual(['hos']);
    expect(queryKeys.dqf()).toEqual(['dqf']);
  });
});

describe('work keys share one invalidation scope', () => {
  it('prefixes both the list and the detail key with the work scope', () => {
    expect(WORK_SCOPE).toBe('work');
    expect(queryKeys.work({ page: 2 })[0]).toBe(WORK_SCOPE);
    expect(queryKeys.order('ord_1')[0]).toBe(WORK_SCOPE);
  });
});

describe('filter normalization', () => {
  it('treats absent and undefined filters as the same key', () => {
    expect(queryKeys.work({ page: undefined, statuses: [] })).toEqual(queryKeys.work());
  });

  it('is insensitive to the order statuses were listed in', () => {
    expect(queryKeys.work({ statuses: ['in_transit', 'dispatched'] })).toEqual(
      queryKeys.work({ statuses: ['dispatched', 'in_transit'] }),
    );
  });

  it('keeps distinct filters on distinct keys', () => {
    expect(queryKeys.work({ page: 1 })).not.toEqual(queryKeys.work({ page: 2 }));
    expect(queryKeys.work({ windowStart: '2026-01-01T00:00:00Z' })).toEqual([
      'work',
      { windowStart: '2026-01-01T00:00:00Z' },
    ]);
  });

  it('copies the supplied status array rather than retaining it', () => {
    const statuses: ('dispatched' | 'in_transit')[] = ['in_transit', 'dispatched'];
    const key = queryKeys.work({ statuses });
    statuses.push('dispatched');
    expect(key).toEqual(['work', { statuses: ['dispatched', 'in_transit'] }]);
  });
});
