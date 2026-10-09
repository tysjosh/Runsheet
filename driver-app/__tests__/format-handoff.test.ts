/**
 * `lib/format.ts` (same rules as the web app's, R13.9) and `lib/handoff.ts`
 * (Navigate / Call, R13.4).
 */

import { Platform } from 'react-native';

import {
  configureFormat,
  date,
  dateTime,
  EMPTY,
  productName,
  relative,
  time,
  timeWindow,
} from '../lib/format';
import { mapsUrl, telUrl } from '../lib/handoff';
import { plannedGallons, productLines, sortWork } from '../lib/work-view';
import { LATER_ORDER, NEXT_ORDER } from './helpers/screen-harness';

beforeEach(() => configureFormat({ timeZone: 'America/Chicago', locale: 'en-US' }));

describe('format', () => {
  const at = '2026-07-30T13:30:45Z'; // 08:30:45 in Chicago (CDT)

  it('shows times in 24 h without seconds, in the configured zone', () => {
    expect(time(at)).toBe('08:30');
    expect(dateTime(at)).toBe('Thu 30 Jul, 08:30');
    expect(date(at)).toBe('Thu 30 Jul');
  });

  it('formats a same-day window compactly and a cross-day window in full', () => {
    expect(timeWindow('2026-07-30T13:30:00Z', '2026-07-30T15:30:00Z')).toBe('08:30–10:30');
    expect(timeWindow('2026-07-31T03:00:00Z', '2026-07-31T07:00:00Z')).toBe('Thu 30 Jul, 22:00 – Fri 31 Jul, 02:00');
    expect(timeWindow('2026-07-30T13:30:00Z', null)).toBe('from 08:30');
  });

  it('renders missing or bad dates as an em dash', () => {
    expect(time(null)).toBe(EMPTY);
    expect(dateTime('not a date')).toBe(EMPTY);
  });

  it('relative times', () => {
    const now = Date.parse(at);
    expect(relative(now - 10_000, { now })).toBe('just now');
    expect(relative(now - 12 * 60_000, { now })).toBe('12 min ago');
    expect(relative(now + 20 * 60_000, { now })).toBe('in 20 min');
  });

  it('names products from the catalog and humanises unknown codes', () => {
    expect(productName('DIESEL_2')).toBe('Diesel #2 (on-road)');
    expect(productName('PROPANE')).toBe('Propane');
    expect(productName('JET_A')).toBe('Jet a');
    expect(productName(null)).toBe(EMPTY);
  });
});

describe('handoff', () => {
  const dest = { address: '1840 County Road 12, Greenfield, IN', lat: 39.785, lon: -85.769 };
  const q = encodeURIComponent(dest.address);

  it.each([
    ['ios', `maps:?daddr=${q}`],
    ['android', `geo:0,0?q=${q}`],
    ['web', `https://www.google.com/maps/dir/?api=1&destination=${q}`],
  ])('mapsUrl on %s', (os, url) => {
    const original = Platform.OS;
    Object.defineProperty(Platform, 'OS', { configurable: true, get: () => os });
    try {
      expect(mapsUrl(dest)).toBe(url);
    } finally {
      Object.defineProperty(Platform, 'OS', { configurable: true, get: () => original });
    }
  });

  it('falls back to coordinates when there is no address, and to null with neither', () => {
    expect(mapsUrl({ address: ' ', lat: 1.5, lon: -2 }, 'android')).toBe(`geo:0,0?q=${encodeURIComponent('1.5,-2')}`);
    expect(mapsUrl({ address: null }, 'ios')).toBeNull();
  });

  it('builds tel: URLs from formatted numbers', () => {
    expect(telUrl('+1 (317) 555-0142')).toBe('tel:+13175550142');
    expect(telUrl('317.555.0142')).toBe('tel:3175550142');
    expect(telUrl('')).toBeNull();
    expect(telUrl(null)).toBeNull();
  });
});

describe('work view helpers', () => {
  it('puts the delivery in transit first, then window order', () => {
    expect(sortWork([LATER_ORDER, NEXT_ORDER]).map((o) => o.order_id)).toEqual(['ord-1', 'ord-2']);
  });

  it('sums manifest gallons by grade, else uses the ordered grade', () => {
    expect(productLines(NEXT_ORDER)).toEqual([{ grade: 'DIESEL_2', gallons: 4200.4 }]);
    expect(productLines(LATER_ORDER)).toEqual([{ grade: 'GASOLINE_REG', gallons: 3100 }]);
  });

  it('planned gallons are whole', () => {
    expect(plannedGallons(NEXT_ORDER)).toBe(4200);
    expect(plannedGallons({ ...LATER_ORDER, ordered_gallons: 0 })).toBeNull();
  });
});
