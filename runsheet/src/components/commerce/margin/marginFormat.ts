/**
 * Margin formatting (margin-feed design "Admin UI", Simplification 12).
 *
 * Money arrives as integer cents or micros. Everything here is integer
 * arithmetic plus string padding, never float division, so a value is shown
 * exactly as stored.
 *
 * A missing cost is `null` and renders as the literal "No cost". It can
 * never become "$0.00" (`Math.trunc(null)` is 0, which is why nothing here
 * coerces). `undefined` is a missing field, which is a bug rather than a
 * missing cost, so it throws.
 */

export const NO_COST = "No cost";
export const MISSING_COST = "Missing cost";

/** Why a record has no cost (`no_cost_reason`), in words. */
export const NO_COST_REASON_LABELS: Record<string, string> = {
  product_unknown: "Unknown product",
  no_lots_no_rack: "No purchases or rack price in the window",
  rack_stale: "Rack price is too old",
  terminal_unattributed_no_cost: "No terminal and no tenant-wide cost",
  computation_error: "Cost could not be computed",
};

export function noCostReasonLabel(reason: string | null): string {
  if (!reason) return "Reason not recorded";
  return NO_COST_REASON_LABELS[reason] ?? reason;
}

/** Flag names as badge text (never colour alone). */
export const FLAG_LABELS: Record<string, string> = {
  missing_cost: "Missing cost",
  negative_margin: "Negative margin",
  below_floor: "Below floor",
  terminal_unattributed: "No terminal",
};

function requireInteger(value: unknown, name: string): number {
  if (value === undefined) {
    throw new TypeError(
      `${name} is undefined (a missing field, not a missing cost)`,
    );
  }
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw new TypeError(`${name} must be a safe integer, got ${String(value)}`);
  }
  return value;
}

/** "1234567" -> "1,234,567" (digits only). */
function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/**
 * Split a non-negative integer into whole and fractional digit strings at
 * `places` decimal places, by string slicing only (no division at all).
 */
function splitScaled(abs: number, places: number): [string, string] {
  const digits = String(abs).padStart(places + 1, "0");
  return [digits.slice(0, -places), digits.slice(-places)];
}

/** Integer cents as USD: 123456 -> "$1,234.56", -20000 -> "-$200.00", null -> "No cost". */
export function formatCents(value: number | null): string {
  if (value === null) return NO_COST;
  const cents = requireInteger(value, "cents");
  const sign = cents < 0 ? "-" : "";
  const [whole, fraction] = splitScaled(Math.abs(cents), 2);
  return `${sign}$${groupThousands(whole)}.${fraction}`;
}

/**
 * Integer micros (USD per gallon) as USD, keeping every significant digit:
 * 2500000 -> "$2.50", 2456789 -> "$2.456789", null -> "No cost".
 */
export function formatMicros(value: number | null): string {
  if (value === null) return NO_COST;
  const micros = requireInteger(value, "micros");
  const sign = micros < 0 ? "-" : "";
  const [whole, raw] = splitScaled(Math.abs(micros), 6);
  const fraction = raw.replace(/0+$/, "").padEnd(2, "0");
  return `${sign}$${groupThousands(whole)}.${fraction}`;
}

/** Server `margin_pct` ("12.50") as "12.50%"; null -> "No cost". */
export function formatPct(value: string | null): string {
  if (value === null) return NO_COST;
  if (value === undefined) {
    throw new TypeError(
      "margin_pct is undefined (a missing field, not a missing cost)",
    );
  }
  if (typeof value !== "string" || !/^-?\d+\.\d{2}$/.test(value)) {
    throw new TypeError(
      `margin_pct must be a 2-dp decimal string, got ${String(value)}`,
    );
  }
  return `${value}%`;
}

/** Integer micro-gallons as gallons: 1000000000 -> "1,000", 1500000 -> "1.5". */
export function formatGallons(ugal: number): string {
  const value = requireInteger(ugal, "gallons_ugal");
  const sign = value < 0 ? "-" : "";
  const [whole, raw] = splitScaled(Math.abs(value), 6);
  const fraction = raw.replace(/0+$/, "");
  return `${sign}${groupThousands(whole)}${fraction ? `.${fraction}` : ""}`;
}

/** Integer basis points as a percentage: 1250 -> "12.50%"; null -> "n/a". */
/**
 * The calendar date (YYYY-MM-DD) of an ISO instant in an IANA time zone.
 * Records use the margin settings timezone, the same date axis as the
 * filters, summary and recompute: "2026-10-05T03:00:00Z" in America/Chicago
 * is "2026-10-04".
 */
export function formatLocalDate(iso: string, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(iso));
  const part = (type: string) =>
    parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/**
 * Summary cost and margin cover costed records only. A block with none of
 * them has no cost to show, so it renders "No cost" rather than "$0.00".
 * (`missing_cost` is set exactly when a record's method is `none`.)
 */
export function hasCostedRecords(block: {
  records: number;
  flag_counts: Record<string, { records: number }>;
}): boolean {
  return block.records - (block.flag_counts.missing_cost?.records ?? 0) > 0;
}

/** Summary cost or margin cents: "No cost" when the block has no costed record. */
export function formatCostedCents(
  block: Parameters<typeof hasCostedRecords>[0],
  cents: number,
): string {
  return formatCents(hasCostedRecords(block) ? cents : null);
}

export function formatBasisPoints(value: number | null): string {
  if (value === null) return "n/a";
  const bp = requireInteger(value, "basis points");
  const sign = bp < 0 ? "-" : "";
  const [whole, fraction] = splitScaled(Math.abs(bp), 2);
  return `${sign}${whole}.${fraction}%`;
}

/** Integer milli-gallons as a 3-dp decimal string for an input: 1500 -> "1.500". */
export function milliToDecimalString(value: number): string {
  const milli = requireInteger(value, "gallons_milli");
  const sign = milli < 0 ? "-" : "";
  const [whole, fraction] = splitScaled(Math.abs(milli), 3);
  return `${sign}${whole}.${fraction}`;
}

/** Integer micros as a 6-dp decimal string for an input: 100000 -> "0.100000". */
export function microsToDecimalString(value: number): string {
  const micros = requireInteger(value, "micros");
  const sign = micros < 0 ? "-" : "";
  const [whole, fraction] = splitScaled(Math.abs(micros), 6);
  return `${sign}${whole}.${fraction}`;
}
