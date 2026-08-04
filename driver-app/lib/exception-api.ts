/**
 * Field exceptions — the seven types a driver can report from the roadside.
 *
 * `POST /api/driver/orders/{order_id}/exceptions` persists the acting
 * `driver_id` from the session, the type, the severity, the note, the geotag, and
 * any media `file_ref` values (R7.1). Escalation to dispatch on `high` or
 * `critical` is the server's job (R7.3); this module only reports.
 *
 * The report goes through the offline mutation queue: a breakdown is exactly the
 * moment the radio is least likely to work, and losing the report because of it
 * would be the worst possible failure (R11.8). The idempotency key is minted once
 * at action time and reused on every retry (R11.6).
 *
 * Requirements: 7.1, 7.4, 7.13, 11.6, 11.8, 11.9
 */

import { enqueueMutation, generateIdempotencyKey, type EnqueueResult } from './offline-queue';

/** `driver/models.py:27-40` `ExceptionType`, verbatim. */
export type ExceptionTypeValue =
  | 'road_closure'
  | 'vehicle_breakdown'
  | 'customer_unavailable'
  | 'access_denied'
  | 'weather'
  | 'cargo_damage'
  | 'other';

/** `Agents/overlay/data_contracts.py` `Severity`, verbatim. */
export type SeverityValue = 'low' | 'medium' | 'high' | 'critical';

export interface ExceptionTypeOption {
  value: ExceptionTypeValue;
  label: string;
}

/** The seven types, in the order the report screen lists them. */
export const EXCEPTION_TYPES: ExceptionTypeOption[] = [
  { value: 'vehicle_breakdown', label: 'Vehicle breakdown' },
  { value: 'road_closure', label: 'Road closure' },
  { value: 'customer_unavailable', label: 'Customer unavailable' },
  { value: 'access_denied', label: 'Site access denied' },
  { value: 'weather', label: 'Weather' },
  { value: 'cargo_damage', label: 'Cargo damage' },
  { value: 'other', label: 'Other' },
];

export interface SeverityOption {
  value: SeverityValue;
  label: string;
  /** Stated on the control, because it changes who hears about the report. */
  effect: string;
}

/** The four severities. `high` and `critical` escalate to dispatch (R7.3). */
export const EXCEPTION_SEVERITIES: SeverityOption[] = [
  { value: 'low', label: 'Low', effect: 'Recorded for the office.' },
  { value: 'medium', label: 'Medium', effect: 'Recorded for the office.' },
  { value: 'high', label: 'High', effect: 'Escalated to dispatch immediately.' },
  {
    value: 'critical',
    label: 'Critical',
    effect: 'Escalated to dispatch immediately.',
  },
];

/** Latitude/longitude as the driver surface sends it (`driver/models.py` GeoPoint). */
export interface ExceptionGeotag {
  lat: number;
  lng: number;
}

export interface ExceptionReport {
  exceptionType: ExceptionTypeValue;
  severity: SeverityValue;
  note: string;
  /** Omitted entirely when precise location is denied (R10.15). */
  geotag?: ExceptionGeotag | null;
  /** `file_ref` values already uploaded through the presign service. */
  mediaRefs?: string[];
}

/**
 * The request body, built in one place so the field names live next to the
 * model they mirror. Exported for the unit test.
 */
export function exceptionRequestBody(report: ExceptionReport): Record<string, unknown> {
  const body: Record<string, unknown> = {
    exception_type: report.exceptionType,
    severity: report.severity,
    note: report.note.trim(),
  };
  if (report.geotag) {
    body.location = { lat: report.geotag.lat, lng: report.geotag.lng };
  }
  if (report.mediaRefs && report.mediaRefs.length > 0) {
    body.media_refs = [...report.mediaRefs];
  }
  return body;
}

/**
 * Queue one exception report against an assigned order.
 *
 * The row is durable before this resolves, so the driver may leave the screen
 * immediately; the drain loop sends it when a connection returns.
 */
export async function queueOrderException(args: {
  orderId: string;
  report: ExceptionReport;
  idempotencyKey?: string;
}): Promise<EnqueueResult> {
  const eventTimestamp = new Date().toISOString();
  return enqueueMutation({
    kind: 'exception',
    method: 'POST',
    path: `/api/driver/orders/${encodeURIComponent(args.orderId)}/exceptions`,
    body: exceptionRequestBody(args.report),
    orderId: args.orderId,
    eventTimestamp,
    idempotencyKey: args.idempotencyKey ?? generateIdempotencyKey(),
    artifactRefs: args.report.mediaRefs ?? [],
  });
}
