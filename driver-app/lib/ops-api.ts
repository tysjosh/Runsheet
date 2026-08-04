/**
 * Driver-submitted operational data: terminal wait reports and compartment
 * cleaning events.
 *
 * Both endpoints already existed and neither is under `/api/driver`, so neither
 * path is re-based here:
 *
 *   `POST /api/fuel/terminals/{terminal_id}/wait-reports`
 *   `POST /api/fuel/mvp/compartments/{compartment_id}/cleaning-events`
 *
 * **Wait reports omit `source`** (R8.1). The server defaults it to
 * `driver_report`, and a client that sent the value itself would be asserting
 * provenance the server already knows — so the field is absent from the body by
 * construction, not merely left blank. The times in the report are the driver's
 * own: `observed_at` carries the driver-observed end of the wait, and
 * `wait_minutes` is the driver-observed start subtracted from it. Nothing is
 * stamped from the device clock at send time, which is what makes a report queued
 * in a dead zone still describe the wait that actually happened.
 *
 * **Cleaning events carry the session `driver_id`** (R8.2), the method drawn from
 * `{flush, purge, sanitize}`, and the evidence `file_ref` values the presign
 * service returned. `actor_id` is populated with the same `driver_id`: the field
 * is a deprecated free-text alias the endpoint still requires, and giving it the
 * canonical value keeps the two from disagreeing.
 *
 * Both are offline-queue mutations with no `order_id`, so they drain
 * unserialized (R11.8).
 *
 * Requirements: 8.1, 8.2, 11.6, 11.8, 11.9
 */

import {
  enqueueMutation,
  generateIdempotencyKey,
  type EnqueueResult,
} from './offline-queue';

// ---------------------------------------------------------------------------
// Terminal wait reports (R8.1)
// ---------------------------------------------------------------------------

export interface TerminalWaitObservation {
  terminalId: string;
  /** Driver-observed instant the wait began, ISO 8601. */
  waitStart: string;
  /** Driver-observed instant the wait ended, ISO 8601. */
  waitEnd: string;
  /** The session `driver_id`. Required whenever `source` is `driver_report`. */
  driverId: string;
  notes?: string | null;
  /** Optional truck attribution. */
  truckId?: string | null;
}

/** The wait in whole-minute resolution, never negative. */
export function waitMinutesBetween(waitStart: string, waitEnd: string): number {
  const start = Date.parse(waitStart);
  const end = Date.parse(waitEnd);
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    return 0;
  }
  const minutes = (end - start) / 60_000;
  return minutes > 0 ? Number(minutes.toFixed(1)) : 0;
}

/**
 * The wait-report body.
 *
 * There is deliberately no `source` key: R8.1 requires the server default
 * `driver_report` to apply. Exported so the unit test can assert its absence.
 */
export function waitReportRequestBody(
  observation: TerminalWaitObservation,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    wait_minutes: waitMinutesBetween(observation.waitStart, observation.waitEnd),
    reporter_id: observation.driverId,
    // The driver-observed end of the wait, not the moment of transmission.
    observed_at: observation.waitEnd,
  };
  if (observation.truckId) {
    body.truck_id = observation.truckId;
  }
  const notes = observation.notes?.trim();
  if (notes) {
    body.notes = notes;
  }
  return body;
}

/** Queue one terminal wait report. */
export async function queueTerminalWaitReport(args: {
  observation: TerminalWaitObservation;
  idempotencyKey?: string;
}): Promise<EnqueueResult> {
  const body = waitReportRequestBody(args.observation);
  return enqueueMutation({
    kind: 'wait_report',
    method: 'POST',
    path: `/api/fuel/terminals/${encodeURIComponent(
      args.observation.terminalId,
    )}/wait-reports`,
    body,
    orderId: null,
    eventTimestamp: args.observation.waitEnd,
    idempotencyKey: args.idempotencyKey ?? generateIdempotencyKey(),
  });
}

// ---------------------------------------------------------------------------
// Compartment cleaning events (R8.2)
// ---------------------------------------------------------------------------

/** The three regimes `CleaningEventCreateRequest.method` accepts. */
export const CLEANING_METHODS = ['flush', 'purge', 'sanitize'] as const;

export type CleaningMethod = (typeof CLEANING_METHODS)[number];

export interface CleaningMethodOption {
  value: CleaningMethod;
  label: string;
  description: string;
}

/** The three controls the route screen offers, in escalating thoroughness. */
export const CLEANING_METHOD_OPTIONS: CleaningMethodOption[] = [
  {
    value: 'flush',
    label: 'Flush',
    description: 'Product flush of the compartment and lines.',
  },
  {
    value: 'purge',
    label: 'Purge',
    description: 'Full purge of residual product and vapour.',
  },
  {
    value: 'sanitize',
    label: 'Sanitize',
    description: 'Wash and sanitize to a certified standard.',
  },
];

export interface CompartmentCleaning {
  compartmentId: string;
  method: CleaningMethod;
  /** The session `driver_id` — the canonical actor reference (R8.2). */
  driverId: string;
  /** `file_ref` values from the presign service (photos, certificates). */
  evidenceRefs?: string[];
  notes?: string | null;
}

/** The cleaning-event body. Exported for the unit test. */
export function cleaningEventRequestBody(
  cleaning: CompartmentCleaning,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    method: cleaning.method,
    // Deprecated free-text alias and canonical reference, given one value so
    // they cannot disagree.
    actor_id: cleaning.driverId,
    driver_id: cleaning.driverId,
  };
  if (cleaning.evidenceRefs && cleaning.evidenceRefs.length > 0) {
    body.evidence_refs = [...cleaning.evidenceRefs];
  }
  const notes = cleaning.notes?.trim();
  if (notes) {
    body.notes = notes;
  }
  return body;
}

/** Queue one compartment cleaning event. */
export async function queueCompartmentCleaning(args: {
  cleaning: CompartmentCleaning;
  idempotencyKey?: string;
}): Promise<EnqueueResult> {
  const eventTimestamp = new Date().toISOString();
  return enqueueMutation({
    kind: 'cleaning_event',
    method: 'POST',
    path: `/api/fuel/mvp/compartments/${encodeURIComponent(
      args.cleaning.compartmentId,
    )}/cleaning-events`,
    body: cleaningEventRequestBody(args.cleaning),
    orderId: null,
    eventTimestamp,
    idempotencyKey: args.idempotencyKey ?? generateIdempotencyKey(),
    artifactRefs: args.cleaning.evidenceRefs ?? [],
  });
}
