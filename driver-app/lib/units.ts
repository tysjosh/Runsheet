/**
 * The ONE units boundary of the Runsheet driver app.
 *
 * Volume, distance, and temperature are formatted here and nowhere else
 * (R16.10). Volume is always US gallons labelled `gal` (R16.19), distance is
 * miles labelled `mi`, temperature is degrees Fahrenheit labelled `°F`, and
 * money defaults to US dollars (R16.9) — the donor's Naira helper at
 * `azumi-rider/lib/utils.ts:8-37` is not carried.
 *
 * **There is no litre formatter and no litre conversion in this module, and
 * therefore none in the app.** A litre value has no rendering path
 * (R16.19). Litres are canonical only inside `mvp_plan_executions`, and the
 * gallons↔litres conversion lives server-side in
 * `Agents/support/volume_units.py` — the app converts nothing (R16.18).
 *
 * Requirements: 16.9, 16.10, 16.18, 16.19
 */

import { QUANTITY_UNIT, type QuantityUnit } from '@/types/order';

/** Unit abbreviation on every displayed volume (R16.19). */
export const VOLUME_UNIT_LABEL = 'gal';

/** Unit abbreviation on every displayed distance (R16.10). */
export const DISTANCE_UNIT_LABEL = 'mi';

/** Unit abbreviation on every displayed temperature (R16.10). */
export const TEMPERATURE_UNIT_LABEL = '°F';

/** Unit abbreviation on every speed the app displays or transmits (R16.10). */
export const SPEED_UNIT_LABEL = 'mph';

/**
 * Miles per hour in one metre per second.
 *
 * The one speed conversion in the app. `expo-location` reports
 * `coords.speed` in metres per second with no way to ask for anything else, and
 * the breadcrumb contract is miles per hour (R10.1, R16.19), so exactly one
 * conversion is unavoidable — and it lives here, at the units boundary, rather
 * than at the call site. Nothing in this app produces km/h.
 */
export const MPH_PER_METER_PER_SECOND = 2.236936;

/** Default currency for every monetary value (R16.9). */
export const DEFAULT_CURRENCY = 'USD';

/** Rendered in place of a missing or unusable number, so no `NaN` reaches the UI. */
export const NO_VALUE = '—';

const LOCALE = 'en-US';

const numberFormatters = new Map<string, Intl.NumberFormat>();

function numberFormatter(minimumFractionDigits: number, maximumFractionDigits: number) {
  const cacheKey = `${minimumFractionDigits}:${maximumFractionDigits}`;
  let formatter = numberFormatters.get(cacheKey);
  if (!formatter) {
    formatter = new Intl.NumberFormat(LOCALE, {
      minimumFractionDigits,
      maximumFractionDigits,
    });
    numberFormatters.set(cacheKey, formatter);
  }
  return formatter;
}

function isRenderable(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

export interface NumberFormatOptions {
  minimumFractionDigits?: number;
  maximumFractionDigits?: number;
}

/**
 * Format a US-gallon volume with its `gal` label.
 *
 * The value is already gallons on every driver-surface contract; nothing is
 * converted here (R16.18).
 */
export function formatGallons(
  gallons: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isRenderable(gallons)) {
    return NO_VALUE;
  }
  const { minimumFractionDigits = 0, maximumFractionDigits = 1 } = options;
  const digits = numberFormatter(minimumFractionDigits, maximumFractionDigits).format(gallons);
  return `${digits} ${VOLUME_UNIT_LABEL}`;
}

/** Format a distance in miles with its `mi` label. */
export function formatMiles(
  miles: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isRenderable(miles)) {
    return NO_VALUE;
  }
  const { minimumFractionDigits = 0, maximumFractionDigits = 1 } = options;
  const digits = numberFormatter(minimumFractionDigits, maximumFractionDigits).format(miles);
  return `${digits} ${DISTANCE_UNIT_LABEL}`;
}

/**
 * Device speed in metres per second → miles per hour (R16.19).
 *
 * A missing reading stays missing: iOS and Android both report `-1` for a speed
 * they do not have, and a negative speed is absent rather than negative, so it
 * answers `null` instead of a fabricated `-2.2`. Rounded to two decimals, which
 * is finer than any GPS speed is accurate to and keeps float noise out of the
 * request body.
 */
export function milesPerHourFromMetersPerSecond(
  metersPerSecond: number | null | undefined,
): number | null {
  if (!isRenderable(metersPerSecond) || metersPerSecond < 0) {
    return null;
  }
  return Math.round(metersPerSecond * MPH_PER_METER_PER_SECOND * 100) / 100;
}

/** Format a speed in miles per hour with its `mph` label. */
export function formatMilesPerHour(
  milesPerHour: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isRenderable(milesPerHour)) {
    return NO_VALUE;
  }
  const { minimumFractionDigits = 0, maximumFractionDigits = 0 } = options;
  const digits = numberFormatter(minimumFractionDigits, maximumFractionDigits).format(
    milesPerHour,
  );
  return `${digits} ${SPEED_UNIT_LABEL}`;
}

/** Format a temperature in degrees Fahrenheit with its `°F` label. */
export function formatFahrenheit(
  fahrenheit: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isRenderable(fahrenheit)) {
    return NO_VALUE;
  }
  const { minimumFractionDigits = 0, maximumFractionDigits = 0 } = options;
  const digits = numberFormatter(minimumFractionDigits, maximumFractionDigits).format(fahrenheit);
  return `${digits}${TEMPERATURE_UNIT_LABEL}`;
}

/** Format a monetary amount. Currency defaults to US dollars (R16.9). */
export function formatCurrency(
  amount: number | null | undefined,
  currency: string = DEFAULT_CURRENCY,
): string {
  if (!isRenderable(amount)) {
    return NO_VALUE;
  }
  return new Intl.NumberFormat(LOCALE, {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

/** Grade → gallons, rendered as one line per grade, for a stop's planned drop. */
export function formatGallonsByGrade(
  gallonsByGrade: Record<string, number> | null | undefined,
): string[] {
  if (!gallonsByGrade) {
    return [];
  }
  return Object.keys(gallonsByGrade)
    .sort()
    .map((grade) => `${grade} ${formatGallons(gallonsByGrade[grade])}`);
}

/**
 * The check-in volume payload (R16.18).
 *
 * Driver-entered gallons are copied through **unchanged** and tagged
 * `quantity_unit: 'us_gallon'`, so the unit is asserted by the request rather
 * than assumed. The deprecated litres field `actual_quantities` is never
 * populated by this app.
 */
export function gallonsCheckinPayload(gallonsByGrade: Record<string, number>): {
  actual_quantities_gallons: Record<string, number>;
  quantity_unit: QuantityUnit;
} {
  return {
    actual_quantities_gallons: { ...gallonsByGrade },
    quantity_unit: QUANTITY_UNIT,
  };
}
