/**
 * `lib/units.ts` — the units boundary.
 *
 * **Validates: Requirements 16.9, 16.10, 16.18, 16.19**
 */

import * as units from '@/lib/units';
import {
  DEFAULT_CURRENCY,
  NO_VALUE,
  formatCurrency,
  formatFahrenheit,
  formatGallons,
  formatGallonsByGrade,
  formatMiles,
  gallonsCheckinPayload,
} from '@/lib/units';

describe('volume formatting', () => {
  it('labels every volume with gal and groups thousands', () => {
    expect(formatGallons(3200)).toBe('3,200 gal');
    expect(formatGallons(1850.25)).toBe('1,850.3 gal');
    expect(formatGallons(0)).toBe('0 gal');
  });

  it('honours an explicit fraction-digit request', () => {
    expect(formatGallons(1850, { minimumFractionDigits: 2, maximumFractionDigits: 2 })).toBe(
      '1,850.00 gal',
    );
  });

  it('renders a placeholder instead of NaN for a missing volume', () => {
    expect(formatGallons(null)).toBe(NO_VALUE);
    expect(formatGallons(undefined)).toBe(NO_VALUE);
    expect(formatGallons(Number.NaN)).toBe(NO_VALUE);
    expect(formatGallons(Number.POSITIVE_INFINITY)).toBe(NO_VALUE);
  });

  it('renders one labelled line per grade, ordered by grade', () => {
    expect(formatGallonsByGrade({ UNLEADED_87: 900, DIESEL_2: 1850 })).toEqual([
      'DIESEL_2 1,850 gal',
      'UNLEADED_87 900 gal',
    ]);
    expect(formatGallonsByGrade(null)).toEqual([]);
  });
});

describe('litres cannot be rendered (R16.19)', () => {
  it('exports no litre formatter and no litre conversion', () => {
    const litreish = Object.keys(units).filter((name) => /lit(er|re)/i.test(name));
    expect(litreish).toEqual([]);
  });
});

describe('distance and temperature formatting', () => {
  it('labels distance with mi', () => {
    expect(formatMiles(12.44)).toBe('12.4 mi');
    expect(formatMiles(1234)).toBe('1,234 mi');
    expect(formatMiles(null)).toBe(NO_VALUE);
  });

  it('labels temperature with degrees Fahrenheit, rounded to whole degrees', () => {
    expect(formatFahrenheit(86.4)).toBe('86°F');
    expect(formatFahrenheit(-4)).toBe('-4°F');
    expect(formatFahrenheit(undefined)).toBe(NO_VALUE);
  });
});

describe('currency formatting', () => {
  it('defaults to US dollars', () => {
    expect(DEFAULT_CURRENCY).toBe('USD');
    expect(formatCurrency(1234.5)).toBe('$1,234.50');
  });

  it('renders a placeholder for a missing amount', () => {
    expect(formatCurrency(null)).toBe(NO_VALUE);
  });
});

describe('check-in payload (R16.18)', () => {
  it('sends driver-entered gallons unconverted, tagged us_gallon', () => {
    const entered = { DIESEL_2: 1850.5, UNLEADED_87: 900 };
    const payload = gallonsCheckinPayload(entered);

    expect(payload).toEqual({
      actual_quantities_gallons: { DIESEL_2: 1850.5, UNLEADED_87: 900 },
      quantity_unit: 'us_gallon',
    });
    // A copy, not the caller's object, and no deprecated litres field.
    expect(payload.actual_quantities_gallons).not.toBe(entered);
    expect(payload).not.toHaveProperty('actual_quantities');
  });
});
