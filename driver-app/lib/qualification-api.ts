/**
 * Driver qualification (DQF) visibility — the read behind the profile screen's
 * compliance section.
 *
 * `GET /api/driver/qualifications` needs no scoping argument: the server resolves
 * the record from the session's `driver_id` and rejects a request naming anybody
 * else, so this module sends no identifier at all (R12.1, R12.6).
 *
 * **Two `DriverStatus` vocabularies, kept apart.** The compliance status
 * (`compliance/models/driver.py:34` — `active | suspended | expired`) arrives
 * under `qualification_status`, deliberately not `status`, because the
 * operational duty status (`fuel/order_models.py:63` — `active | inactive |
 * on_break | off_duty`) uses the same field name for different values. Nothing
 * in this module touches duty status, and `lib/duty-api.ts` never reads a
 * qualification value: the separation is structural, not a naming convention
 * (R12.7).
 *
 * **The thresholds live here, once.** An item expiring within
 * {@link ADVISORY_WINDOW_DAYS} days is advisory and an item expiring within
 * {@link URGENT_WINDOW_DAYS} days is urgent (R12.3, R12.4), both carrying the
 * number of days remaining. The screen renders what {@link qualificationItems}
 * returns and classifies nothing itself.
 *
 * **Eligibility is never recomputed.** `is_dispatch_eligible` and the reasons
 * behind it come from the same `Dispatch_Eligibility` the backend transition gate
 * consults, so the banner cannot tell a driver they are blocked for a reason the
 * gate does not hold — or, worse, tell them they are fine while the gate blocks
 * them (R12.5).
 *
 * This is a read, so there is no offline-queue involvement: R11.8 lists the
 * queued mutation kinds and a qualification read is not one.
 *
 * Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.7
 */

import { apiRequest } from './api-client';

/** The compliance `DriverStatus` (`compliance/models/driver.py:34`), verbatim. */
export type QualificationStatus = 'active' | 'suspended' | 'expired';

/** Wire shape of `GET /api/driver/qualifications` (R12.2). */
export interface DriverQualifications {
  driver_id: string;
  cdl_class?: string | null;
  /** Compliance status — never conflated with duty status (R12.7). */
  qualification_status?: string | null;
  is_dispatch_eligible: boolean;
  /** What the persistent banner lists while ineligible (R12.5). */
  ineligibility_reasons?: string[] | null;
  /** ISO `YYYY-MM-DD`, or `null` where the driver holds no such item. */
  cdl_expiry_date?: string | null;
  medical_card_expiry_date?: string | null;
  hazmat_endorsement_expiry_date?: string | null;
  tanker_endorsement_expiry_date?: string | null;
  last_drug_test_date?: string | null;
}

interface QualificationResponse {
  data: DriverQualifications;
  request_id?: string;
}

/** Read the authenticated driver's own qualification summary (R12.1). */
export async function loadDriverQualifications(): Promise<DriverQualifications> {
  const response = await apiRequest<QualificationResponse>({
    method: 'GET',
    path: '/api/driver/qualifications',
  });
  return response.data;
}

// ---------------------------------------------------------------------------
// Expiry classification
// ---------------------------------------------------------------------------

/** An item expiring this many days out or sooner is advisory (R12.3). */
export const ADVISORY_WINDOW_DAYS = 60;

/** An item expiring this many days out or sooner is urgent (R12.4). */
export const URGENT_WINDOW_DAYS = 7;

/**
 * How an expiry date is rendered.
 *
 * `expired` is its own tier rather than a very urgent one: an item whose date has
 * passed has no "days remaining" to show, and it is the state that makes a driver
 * ineligible, so the interface must not present it as a countdown.
 */
export type ExpiryUrgency = 'expired' | 'urgent' | 'advisory' | 'ok' | 'unknown';

/** Milliseconds in one day. Dates here are whole calendar days, never instants. */
const MS_PER_DAY = 86_400_000;

/**
 * Parse an ISO `YYYY-MM-DD` calendar date to UTC midnight.
 *
 * Deliberately not `new Date(value)` on a bare date-only string in a local-time
 * context: an expiry is a calendar day, and anchoring both sides of the
 * subtraction at UTC midnight is what keeps "expires in 7 days" from becoming 6
 * or 8 for a driver in a negative UTC offset.
 */
function parseCalendarDate(value: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value.trim());
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const ms = Date.UTC(year, month - 1, day);
  const parsed = new Date(ms);
  // Rejects 2026-02-30 and friends, which `Date.UTC` would roll forward.
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    return null;
  }
  return ms;
}

/** The reference instant's own calendar day, anchored at UTC midnight. */
function todayAnchor(now: Date): number {
  return Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
}

/**
 * Whole days from the reference date to an expiry date.
 *
 * `0` means the item expires today, negatives mean it already has, and `null`
 * means there is no usable date to count from.
 */
export function daysUntil(
  isoDate: string | null | undefined,
  now: Date = new Date(),
): number | null {
  if (!isoDate) {
    return null;
  }
  const expiry = parseCalendarDate(isoDate);
  if (expiry === null) {
    return null;
  }
  return Math.round((expiry - todayAnchor(now)) / MS_PER_DAY);
}

/**
 * Classify a day count into a rendering tier (R12.3, R12.4).
 *
 * The windows are inclusive on both bounds: an item 7 days out is urgent, and one
 * 60 days out is advisory. The urgent window is checked first, so the two
 * overlapping requirements resolve to the more serious of the two rather than to
 * whichever happened to be tested first.
 */
export function expiryUrgency(daysRemaining: number | null): ExpiryUrgency {
  if (daysRemaining === null) {
    return 'unknown';
  }
  if (daysRemaining < 0) {
    return 'expired';
  }
  if (daysRemaining <= URGENT_WINDOW_DAYS) {
    return 'urgent';
  }
  if (daysRemaining <= ADVISORY_WINDOW_DAYS) {
    return 'advisory';
  }
  return 'ok';
}

/** The four DQF items that carry an expiry date, in the order they are shown. */
const EXPIRY_FIELDS = [
  { key: 'cdl', label: 'Commercial driver licence', field: 'cdl_expiry_date' },
  { key: 'medical_card', label: 'Medical card', field: 'medical_card_expiry_date' },
  {
    key: 'hazmat_endorsement',
    label: 'HAZMAT endorsement',
    field: 'hazmat_endorsement_expiry_date',
  },
  {
    key: 'tanker_endorsement',
    label: 'Tanker endorsement',
    field: 'tanker_endorsement_expiry_date',
  },
] as const satisfies readonly {
  key: string;
  label: string;
  field: keyof DriverQualifications;
}[];

export type QualificationItemKey = (typeof EXPIRY_FIELDS)[number]['key'];

/** One row of the qualification section, already classified for rendering. */
export interface QualificationItem {
  key: QualificationItemKey;
  label: string;
  /** ISO `YYYY-MM-DD`, or `null` when the driver holds no such item. */
  expiryDate: string | null;
  /** Whole days remaining; negative once expired, `null` when there is no date. */
  daysRemaining: number | null;
  urgency: ExpiryUrgency;
  /** The days-remaining line for the advisory and urgent indicators, or `null`. */
  indicatorLabel: string | null;
}

/** `N days` / `1 day` / `today`, for the indicator text (R12.3, R12.4). */
export function daysRemainingLabel(daysRemaining: number): string {
  if (daysRemaining === 0) {
    return 'today';
  }
  return daysRemaining === 1 ? '1 day' : `${daysRemaining} days`;
}

/** `Expires today` / `Expires in 5 days`. */
function expiryPhrase(daysRemaining: number): string {
  return daysRemaining === 0
    ? 'Expires today'
    : `Expires in ${daysRemainingLabel(daysRemaining)}`;
}

/**
 * The days-remaining indicator line, or `null` when no indicator is warranted.
 *
 * Only the advisory and urgent tiers get one: an item more than 60 days out needs
 * no countdown, and an expired one has none to give.
 */
function indicatorLabelFor(
  urgency: ExpiryUrgency,
  daysRemaining: number | null,
): string | null {
  if (daysRemaining === null) {
    return null;
  }
  if (urgency === 'urgent') {
    return `${expiryPhrase(daysRemaining)} — renew now`;
  }
  if (urgency === 'advisory') {
    return expiryPhrase(daysRemaining);
  }
  return null;
}

/**
 * Project the DQF record into the rows the profile screen renders.
 *
 * The most recent drug-test date is **not** among them: it is a date something
 * happened, not a date something lapses, so classifying it against an expiry
 * window would invent a rule Requirement 12 does not state. The screen shows it
 * as a plain fact instead.
 */
export function qualificationItems(
  record: DriverQualifications,
  now: Date = new Date(),
): QualificationItem[] {
  return EXPIRY_FIELDS.map((descriptor) => {
    const raw = record[descriptor.field];
    const expiryDate = typeof raw === 'string' && raw.length > 0 ? raw : null;
    const daysRemaining = daysUntil(expiryDate, now);
    const urgency = expiryUrgency(daysRemaining);
    return {
      key: descriptor.key,
      label: descriptor.label,
      expiryDate,
      daysRemaining,
      urgency,
      indicatorLabel: indicatorLabelFor(urgency, daysRemaining),
    };
  });
}

/**
 * The lines the persistent ineligibility banner lists (R12.5).
 *
 * The server's reasons are used as given — they come from the same eligibility
 * computation the transition gate enforces. The derived fallback covers only the
 * case where the server reports ineligibility with no reasons attached: an empty
 * persistent banner would tell a driver they are blocked and refuse to say by
 * what, so the expired and missing items are named instead. Nothing is derived
 * while the server supplied reasons of its own.
 */
export function ineligibilityReasons(
  record: DriverQualifications,
  now: Date = new Date(),
): string[] {
  const reported = (record.ineligibility_reasons ?? [])
    .filter((reason): reason is string => typeof reason === 'string')
    .map((reason) => reason.trim())
    .filter((reason) => reason.length > 0);
  if (reported.length > 0) {
    return reported;
  }
  const derived = qualificationItems(record, now)
    .filter((item) => item.urgency === 'expired')
    .map((item) =>
      item.expiryDate
        ? `${item.label} expired on ${formatQualificationDate(item.expiryDate)}`
        : item.label,
    );
  if (derived.length > 0) {
    return derived;
  }
  return ['Dispatch is blocked on your qualification file. Contact the office.'];
}

/** Compliance status label. Never routed through `dutyStatusLabel` (R12.7). */
export function qualificationStatusLabel(
  status: string | null | undefined,
): string {
  switch (status) {
    case 'active':
      return 'Qualified';
    case 'suspended':
      return 'Suspended';
    case 'expired':
      return 'Expired';
    default:
      return 'Unknown';
  }
}

/** Rendered in place of a date the record does not carry. */
export const NO_DATE = 'Not on file';

/**
 * Render an ISO `YYYY-MM-DD` as a US calendar date.
 *
 * Formatted from the parsed parts rather than from a `Date` in the device's zone,
 * for the same reason {@link parseCalendarDate} exists: a date-only value shown
 * through a local-time conversion can land on the previous day.
 */
export function formatQualificationDate(
  isoDate: string | null | undefined,
): string {
  if (!isoDate) {
    return NO_DATE;
  }
  const ms = parseCalendarDate(isoDate);
  if (ms === null) {
    return NO_DATE;
  }
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(ms));
}
