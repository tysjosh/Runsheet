/**
 * Hours-of-Service advisory — the read behind the profile screen's HOS section.
 *
 * `GET /api/driver/hos` takes no scoping argument: the server resolves the
 * reading from the session's `driver_id` and rejects a request naming anybody
 * else with 403, so this module sends no identifier at all (R17.32).
 *
 * **Every figure is advisory and the carrier's ELD is the record.** The envelope
 * carries `authoritative_record_statement`, and that sentence is what the screen
 * renders (R16.20, R17.1). It is not restated here: one statement, authored
 * server-side, means the app cannot drift from the wording the backend considers
 * the disclosure.
 *
 * **`unavailable` is not zero.** `_normalize_duty_status`
 * (`integrations/geotab.py:470-489`) extracts only the vendor duty-status string,
 * and `truck_telemetry.hos_status` is a single `keyword` field, so for a Geotab
 * tenant all three figures arrive `availability: 'unavailable'` (R17.13). A
 * figure with no value has no minutes, so {@link figureMinutes} answers `null`
 * and the screen says the figure is not supplied rather than showing `0 minutes`
 * — which would read as "you are out of hours" and be a fabrication.
 *
 * **The hours→minutes conversion happens once**, in {@link figureMinutes}. The
 * wire unit is hours (`HOSFigure.hours` attaches `unit: 'hours'`); R17.15 and
 * R17.16 are stated in minutes. Nothing else in the app multiplies by 60.
 *
 * **This module reads no Runsheet duty status.** `active` / `inactive` /
 * `on_break` / `off_duty` are availability values owned by `lib/duty-api.ts`
 * (R17.27); the `duty_status` field here is the *telematics vendor's* string off
 * the reading, and the two never meet — not in a label helper, not in a query
 * key, not in a card.
 *
 * A read is not a queued mutation: R11.8 lists the queued kinds and this is not
 * one of them.
 *
 * Requirements: 16.20, 17.1, 17.11, 17.12, 17.13, 17.15, 17.16, 17.27, 17.32
 */

import { apiRequest } from './api-client';

/** Freshness of the resolved reading (R17.8, R17.10). */
export type HOSFreshnessState = 'fresh' | 'stale' | 'unknown';

/** Compliance state the server resolved (R17.10). Never inferred here. */
export type HOSComplianceState = 'within_limits' | 'at_limit' | 'unknown';

/** Whether the tenant's connector supplies a figure at all (R17.13). */
export type HOSFigureAvailability = 'available' | 'unavailable';

/** Wire unit of every available figure. */
export const HOURS_UNIT = 'hours';

/**
 * One Hours-of-Service figure, or the explicit absence of one.
 *
 * `availability` is the discriminator: `unavailable` says the connector does not
 * supply this figure, which is a different claim from "the figure is zero".
 */
export interface HOSFigure {
  availability: HOSFigureAvailability;
  value?: number | null;
  unit?: string | null;
  advisory?: boolean;
}

/** The advisory `GET /api/driver/hos` returns (R17.11, R17.12). */
export interface HOSAdvisory {
  tenant_id: string;
  driver_id: string;
  freshness_state: HOSFreshnessState;
  compliance_state: HOSComplianceState;
  reason_code?: string | null;
  truck_id?: string | null;
  /** The telematics vendor's duty-status string — never a Runsheet duty status. */
  duty_status?: string | null;
  recorded_at?: string | null;
  reading_age_seconds?: number | null;
  freshness_window_seconds?: number | null;
  provider_name?: string | null;
  remaining_drive_time: HOSFigure;
  remaining_on_duty_window: HOSFigure;
  cycle_hours: HOSFigure;
}

interface HOSAdvisoryResponse {
  data: HOSAdvisory;
  advisory?: boolean;
  authoritative_record?: string;
  authoritative_record_statement?: string;
  request_id?: string;
}

/** The advisory together with the disclosure that must accompany it (R16.20). */
export interface HOSAdvisoryRead {
  advisory: HOSAdvisory;
  /**
   * The server's ELD statement, rendered verbatim. `null` only if a future
   * response omitted it — the screen then shows no figures rather than showing
   * figures with no disclosure.
   */
  authoritativeRecordStatement: string | null;
}

/** Read the authenticated driver's own Hours-of-Service advisory (R17.32). */
export async function loadHOSAdvisory(): Promise<HOSAdvisoryRead> {
  const response = await apiRequest<HOSAdvisoryResponse>({
    method: 'GET',
    path: '/api/driver/hos',
  });
  const statement = response.authoritative_record_statement;
  return {
    advisory: response.data,
    authoritativeRecordStatement:
      typeof statement === 'string' && statement.trim().length > 0
        ? statement.trim()
        : null,
  };
}

// ---------------------------------------------------------------------------
// Minutes — the one conversion
// ---------------------------------------------------------------------------

/** Minutes in one hour. The only place hours become minutes. */
export const MINUTES_PER_HOUR = 60;

/** At or under this many remaining minutes, the advisory is raised (R17.15). */
export const APPROACHING_LIMIT_WINDOW_MINUTES = 60;

/**
 * The FMCSA driving limit the remaining-drive-time figure counts down from,
 * matching `DRIVE_LIMIT_HOURS` in `compliance/services/hos_checker.py`. Named in
 * the advisory because R17.15 requires the limit itself to be named, not just
 * the minutes left against it.
 */
export const DRIVE_LIMIT_HOURS = 11;

/** How the driving limit is named to the driver (R17.15). */
export const DRIVE_LIMIT_NAME = `${DRIVE_LIMIT_HOURS}-hour driving limit`;

/**
 * A figure's value in whole minutes, or `null` when there is none.
 *
 * `null` for an `unavailable` figure, for a non-finite value, and for a unit this
 * app cannot convert — an unrecognised unit is an unknown quantity, and guessing
 * it is hours would put a fabricated countdown in front of a driver.
 */
export function figureMinutes(figure: HOSFigure | null | undefined): number | null {
  if (!figure || figure.availability !== 'available') {
    return null;
  }
  const { value, unit } = figure;
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return null;
  }
  if (unit !== HOURS_UNIT) {
    return null;
  }
  return Math.round(value * MINUTES_PER_HOUR);
}

/** Remaining drive time in whole minutes, or `null` when unavailable. */
export function remainingDriveMinutes(
  advisory: HOSAdvisory | null | undefined,
): number | null {
  return advisory ? figureMinutes(advisory.remaining_drive_time) : null;
}

// ---------------------------------------------------------------------------
// The out-of-hours duty-status mapping
// ---------------------------------------------------------------------------

/**
 * Vendor duty-status values that mean the driver is out of hours (R17.16).
 *
 * R17.16 sources this from tenant configuration; no tenant configuration carries
 * such a mapping today, so this is the default the app applies until one does,
 * and {@link isOutOfHoursDutyStatus} takes an override so a configured mapping
 * can be threaded through without touching the screen.
 *
 * Compared after normalising case and separators, because vendors spell the same
 * state `OutOfHours`, `out_of_hours`, and `OUT-OF-HOURS`.
 *
 * Geotab's own set — `D`, `Driving`, `OnDuty`, `SB`, `OffDuty`,
 * `PersonalConveyance`, `YardMove` (`integrations/geotab.py:470-489`) — contains
 * none of these, and deliberately: `OffDuty` and `SB` are rest states, not
 * exhausted-hours states. Mapping them to at-limit would tell a driver taking a
 * legal break that they are out of hours.
 */
export const OUT_OF_HOURS_DUTY_STATUSES: readonly string[] = [
  'outofhours',
  'hosviolation',
  'drivelimitreached',
];

function normalizeDutyStatusToken(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, '');
}

/** Whether a vendor duty-status string maps to out-of-hours (R17.16). */
export function isOutOfHoursDutyStatus(
  dutyStatus: string | null | undefined,
  mapping: readonly string[] = OUT_OF_HOURS_DUTY_STATUSES,
): boolean {
  if (typeof dutyStatus !== 'string' || dutyStatus.trim().length === 0) {
    return false;
  }
  const token = normalizeDutyStatusToken(dutyStatus);
  return mapping.some((entry) => normalizeDutyStatusToken(entry) === token);
}

// ---------------------------------------------------------------------------
// Limit classification
// ---------------------------------------------------------------------------

/**
 * What the HOS section renders.
 *
 * `unavailable` is its own state rather than a benign `within_limits`: a tenant
 * whose connector supplies no figures has told the app nothing about limits, and
 * R17.10 already forbids the server calling that within limits.
 */
export type HOSLimitState =
  | 'at_limit'
  | 'approaching_limit'
  | 'within_limits'
  | 'unavailable';

/**
 * Classify an advisory into the state the screen renders (R17.15, R17.16).
 *
 * Order matters. The out-of-hours duty-status mapping is checked first because
 * R17.16 makes it sufficient on its own, whatever the figures say. Then, with no
 * usable figure, the server's own `at_limit` still stands and everything else is
 * `unavailable`. With a figure: `0` minutes or less is at-limit, up to and
 * including 60 minutes is the approaching-limit advisory, and beyond that is
 * within limits.
 *
 * Total by construction: every input lands on exactly one of the four states.
 */
export function hosLimitState(
  advisory: HOSAdvisory | null | undefined,
  mapping: readonly string[] = OUT_OF_HOURS_DUTY_STATUSES,
): HOSLimitState {
  if (!advisory) {
    return 'unavailable';
  }
  if (isOutOfHoursDutyStatus(advisory.duty_status, mapping)) {
    return 'at_limit';
  }
  const minutes = remainingDriveMinutes(advisory);
  if (minutes === null) {
    return advisory.compliance_state === 'at_limit' ? 'at_limit' : 'unavailable';
  }
  if (minutes <= 0) {
    return 'at_limit';
  }
  if (minutes <= APPROACHING_LIMIT_WINDOW_MINUTES) {
    return 'approaching_limit';
  }
  return 'within_limits';
}

/** `1 minute` / `45 minutes`. */
export function minutesLabel(minutes: number): string {
  return minutes === 1 ? '1 minute' : `${minutes} minutes`;
}

/**
 * The line the approaching-limit advisory and the at-limit state carry.
 *
 * Both name the limit; the approaching one also names the remaining minutes
 * (R17.15). `null` for the two states that raise no line at all.
 */
export function hosLimitMessage(
  advisory: HOSAdvisory | null | undefined,
  mapping: readonly string[] = OUT_OF_HOURS_DUTY_STATUSES,
): string | null {
  const state = hosLimitState(advisory, mapping);
  if (state === 'approaching_limit') {
    const minutes = remainingDriveMinutes(advisory);
    return `${minutesLabel(minutes ?? 0)} of drive time left against the ${DRIVE_LIMIT_NAME}.`;
  }
  if (state === 'at_limit') {
    if (advisory && isOutOfHoursDutyStatus(advisory.duty_status, mapping)) {
      return `Your carrier's ELD reports you out of hours against the ${DRIVE_LIMIT_NAME}.`;
    }
    return `No drive time left against the ${DRIVE_LIMIT_NAME}.`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Rendering the figures and the reading honestly
// ---------------------------------------------------------------------------

/** Shown in place of a figure the tenant's connector does not supply (R17.13). */
export const FIGURE_UNAVAILABLE = 'Not supplied by your telematics provider';

/** Format a figure, or say plainly that there is none. Never renders `0`. */
export function formatHOSFigure(figure: HOSFigure | null | undefined): string {
  const minutes = figureMinutes(figure);
  if (minutes === null) {
    return FIGURE_UNAVAILABLE;
  }
  if (minutes < MINUTES_PER_HOUR) {
    return minutesLabel(minutes);
  }
  const hours = minutes / MINUTES_PER_HOUR;
  const rendered = Number.isInteger(hours) ? `${hours}` : hours.toFixed(1);
  return `${rendered} ${HOURS_UNIT}`;
}

/** The three figures, in the order the section lists them. */
export interface HOSFigureRow {
  key: 'remaining_drive_time' | 'remaining_on_duty_window' | 'cycle_hours';
  label: string;
  figure: HOSFigure;
  /** Already formatted, so the screen renders and does not convert. */
  display: string;
}

/** Project the advisory's three figures into rows (R17.12, R17.13). */
export function hosFigureRows(advisory: HOSAdvisory): HOSFigureRow[] {
  const rows: Omit<HOSFigureRow, 'display'>[] = [
    {
      key: 'remaining_drive_time',
      label: `Drive time left (${DRIVE_LIMIT_NAME})`,
      figure: advisory.remaining_drive_time,
    },
    {
      key: 'remaining_on_duty_window',
      label: 'On-duty window left (14-hour window)',
      figure: advisory.remaining_on_duty_window,
    },
    {
      key: 'cycle_hours',
      label: 'Cycle hours used',
      figure: advisory.cycle_hours,
    },
  ];
  return rows.map((row) => ({ ...row, display: formatHOSFigure(row.figure) }));
}

/**
 * Why the advisory says what it says, in the driver's words.
 *
 * Each line maps one reason code the service documents; an unrecognised code
 * yields `null` rather than the raw token, and a `fresh` reading needs no
 * explanation at all.
 */
export function hosReadingExplanation(
  advisory: HOSAdvisory | null | undefined,
): string | null {
  if (!advisory) {
    return null;
  }
  switch (advisory.reason_code) {
    case 'HOS_TRUCK_UNASSIGNED':
      return 'No truck is assigned to you, so there is no telematics reading to show.';
    case 'HOS_NO_READING':
      return 'Your truck has sent no telematics reading, so there are no figures to show.';
    case 'HOS_READING_STALE':
      return 'The last telematics reading is older than your carrier’s freshness window, so the figures are withheld.';
    default:
      return advisory.freshness_state === 'fresh'
        ? null
        : 'No usable telematics reading resolved, so no figures are shown.';
  }
}

/** Seconds in one minute — the reading age arrives in seconds, not hours. */
const SECONDS_PER_MINUTE = 60;

/** `Recorded 45 seconds ago` / `Recorded 3 minutes ago`, or `null`. */
export function readingAgeLabel(
  advisory: HOSAdvisory | null | undefined,
): string | null {
  const age = advisory?.reading_age_seconds;
  if (typeof age !== 'number' || !Number.isFinite(age) || age < 0) {
    return null;
  }
  if (age < SECONDS_PER_MINUTE) {
    return age === 1 ? 'Recorded 1 second ago' : `Recorded ${age} seconds ago`;
  }
  const minutes = Math.round(age / SECONDS_PER_MINUTE);
  return `Recorded ${minutesLabel(minutes)} ago`;
}
