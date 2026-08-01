/**
 * Unit tests for the countdown behaviour retained from the copied
 * `components/DispatchOrderCard.tsx` offer card (Requirement 16.5).
 *
 * The three helpers under test are the pure part of the donor's countdown: the
 * expiry resolution that must come from the server rather than a hard-coded
 * duration, the `45s` / `1m 30s` label, and the colour grading of the timer
 * badge.
 */

import {
  formatTimeRemaining,
  secondsRemainingOn,
  timerToneClass,
  type OrderOffer,
} from '@/components/DispatchOrderCard';
import type { FuelOrder } from '@/types/order';

const order: FuelOrder = {
  order_id: 'ord-1',
  status: 'dispatched',
  delivery_window_start: '2026-07-29T14:00:00Z',
  delivery_window_end: '2026-07-29T18:00:00Z',
  destination: { lat: 41.88, lon: -87.63, address: '233 S Wacker Dr, Chicago, IL' },
  customer_name: 'Halsted Fuel Stop',
  product_grade: 'diesel',
  ordered_gallons: 4200,
  quantity_unit: 'us_gallon',
};

function offer(overrides: Partial<OrderOffer> = {}): OrderOffer {
  return { order, timeoutSeconds: 60, ...overrides };
}

describe('secondsRemainingOn', () => {
  const now = Date.parse('2026-07-29T14:00:00Z');

  it('prefers the server expiry instant over the timeout window', () => {
    expect(secondsRemainingOn(offer({ expiresAt: '2026-07-29T14:00:45Z' }), now)).toBe(45);
  });

  it('falls back to the server timeout when no expiry instant is supplied', () => {
    expect(secondsRemainingOn(offer({ timeoutSeconds: 90 }), now)).toBe(90);
  });

  it('never reports a negative remainder for a lapsed offer', () => {
    expect(secondsRemainingOn(offer({ expiresAt: '2026-07-29T13:59:00Z' }), now)).toBe(0);
  });

  it('falls back to the timeout when the expiry instant is unparseable', () => {
    expect(secondsRemainingOn(offer({ expiresAt: 'not-a-date', timeoutSeconds: 30 }), now)).toBe(30);
  });
});

describe('formatTimeRemaining', () => {
  it('renders seconds alone below a minute', () => {
    expect(formatTimeRemaining(45)).toBe('45s');
    expect(formatTimeRemaining(0)).toBe('0s');
  });

  it('renders minutes and seconds at or above a minute', () => {
    expect(formatTimeRemaining(60)).toBe('1m 0s');
    expect(formatTimeRemaining(90)).toBe('1m 30s');
  });
});

describe('timerToneClass', () => {
  it('grades green, amber, then red as the window closes', () => {
    expect(timerToneClass(60, 60)).toBe('bg-green-500');
    expect(timerToneClass(20, 60)).toBe('bg-yellow-500');
    expect(timerToneClass(5, 60)).toBe('bg-red-500');
  });

  it('reports red rather than dividing by a zero window', () => {
    expect(timerToneClass(0, 0)).toBe('bg-red-500');
  });
});
