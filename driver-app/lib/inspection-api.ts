/**
 * Vehicle inspection reports — pre-trip and post-trip, one body shape.
 *
 * `POST /api/driver/inspections` records the acting `driver_id` from the verified
 * session, the inspected asset, the odometer reading **in miles**, the client's
 * inspection timestamp, and the defect list (R8.3, R8.4). There is no
 * `driver_id` field in the body: the server takes the subject from the session
 * claim, so this module never sends one.
 *
 * `inspection_type` is the only thing that distinguishes a post-trip report from
 * a pre-trip one — the field set is identical (R8.8). The app sends both; whether
 * a `post_trip` submission is accepted is the tenant's decision, expressed
 * through the `driver.pretrip_inspection_required` overlay flag and answered by
 * the server. A tenant that has not enabled the workflow answers 400
 * `post_trip_intake_not_enabled`, which is why the screen presents pre-trip as
 * the default.
 *
 * `inspection_local_date` is **precomputed here**, from the device's own
 * calendar, and sent as `YYYY-MM-DD`. The server stores it as a keyword so the
 * "first transition in a calendar day" gate is one term filter rather than a
 * timezone calculation; deriving it on the device is what makes the day the
 * driver's day rather than UTC's.
 *
 * The report goes through the offline mutation queue (R11.8). A walk-around
 * happens in a yard, and a yard is exactly where the radio is worst; the queue
 * sends the row with its `X-Idempotency-Key` when service returns, and the key
 * is minted once at action time and reused on every retry (R8.10, R11.6, R11.9).
 * Defect photo `file_ref` values travel on the row, so the bytes stay on the
 * device until the submission has actually left (R11.16).
 *
 * Requirements: 8.3, 8.4, 8.8, 8.10, 11.6, 11.8, 11.9, 11.16
 */

import {
  enqueueMutation,
  generateIdempotencyKey,
  type EnqueueResult,
} from './offline-queue';

/** `driver/services/inspection_service.py` `INSPECTION_TYPES`, verbatim. */
export type InspectionTypeValue = 'pre_trip' | 'post_trip';

/** `DEFECT_SEVERITIES`, verbatim. `out_of_service` stops the asset (R8.5). */
export type DefectSeverityValue = 'minor' | 'out_of_service';

/** `INSPECTION_COMPONENTS`, verbatim and in the server's order (R8.4). */
export const INSPECTION_COMPONENTS = [
  'service_brakes',
  'parking_brake',
  'steering_mechanism',
  'lighting_devices',
  'tires',
  'wheels_and_rims',
  'horn',
  'windshield_wipers',
  'rear_vision_mirrors',
  'coupling_devices',
  'suspension',
  'frame_and_body',
  'exhaust_system',
  'fuel_system',
  'emergency_equipment',
  'fire_extinguisher',
  'cargo_tank_shell',
  'cargo_tank_valves',
  'hoses_and_fittings',
  'pump',
  'meter_and_register',
  'vapor_recovery',
  'bottom_loading_equipment',
  'grounding_equipment',
  'placards_and_markings',
  'other',
] as const;

export type InspectionComponent = (typeof INSPECTION_COMPONENTS)[number];

export interface InspectionTypeOption {
  value: InspectionTypeValue;
  label: string;
  description: string;
}

/**
 * The two report types the screen offers. Pre-trip is first because it is
 * accepted in every tenant; post-trip depends on the tenant having enabled the
 * inspection workflow, which the control says rather than leaving the driver to
 * discover it from a rejection.
 */
export const INSPECTION_TYPE_OPTIONS: InspectionTypeOption[] = [
  {
    value: 'pre_trip',
    label: 'Pre-trip',
    description: 'Walk-around before the first trip of the day.',
  },
  {
    value: 'post_trip',
    label: 'Post-trip',
    description: 'End-of-day report. Available where your carrier enables it.',
  },
];

export interface DefectSeverityOption {
  value: DefectSeverityValue;
  label: string;
  /** Stated on the control, because `out_of_service` stops the truck (R8.5). */
  effect: string;
}

/** The two severities. */
export const DEFECT_SEVERITY_OPTIONS: DefectSeverityOption[] = [
  {
    value: 'minor',
    label: 'Minor',
    effect: 'Recorded for maintenance. The truck stays in service.',
  },
  {
    value: 'out_of_service',
    label: 'Out of service',
    effect: 'Takes this truck out of service and alerts dispatch.',
  },
];

/** `service_brakes` → `Service brakes`, so the vocabulary has one source. */
export function componentLabel(component: string): string {
  const words = component.replace(/_/g, ' ').trim();
  if (!words) {
    return component;
  }
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** One defect the driver found, before its photos have `file_ref` values. */
export interface InspectionDefectDraft {
  component: InspectionComponent;
  severity: DefectSeverityValue;
  note: string;
  /** `file_ref` values already returned by the presign service (R8.4, R15.8). */
  photoRefs?: string[];
}

export interface InspectionReport {
  /** `pre_trip` or `post_trip` — the same field set either way (R8.8). */
  inspectionType: InspectionTypeValue;
  /** The inspected vehicle, as the fleet identifies it. */
  assetId: string;
  /** The odometer reading in **miles**, converted nowhere (R8.3, R16.10). */
  odometerMiles: number;
  /** The driver's own stamp for the walk-around, ISO 8601. */
  inspectionTimestamp: string;
  /** The calendar day in the driver's timezone, `YYYY-MM-DD`. */
  inspectionLocalDate: string;
  defects: InspectionDefectDraft[];
}

/**
 * The device's calendar day as `YYYY-MM-DD`.
 *
 * Built from the local date parts rather than from `toISOString`, which would
 * answer with the UTC day and put a 19:00 Houston walk-around on tomorrow.
 */
export function localCalendarDay(when: Date = new Date()): string {
  const month = String(when.getMonth() + 1).padStart(2, '0');
  const day = String(when.getDate()).padStart(2, '0');
  return `${when.getFullYear()}-${month}-${day}`;
}

/** Every photo `file_ref` on the report, in defect order. */
export function inspectionPhotoRefs(report: InspectionReport): string[] {
  return report.defects.flatMap((defect) => defect.photoRefs ?? []);
}

/**
 * The request body, built in one place so the field names live next to the
 * document they mirror. Exported for the unit test.
 */
export function inspectionRequestBody(
  report: InspectionReport,
): Record<string, unknown> {
  return {
    asset_id: report.assetId.trim(),
    odometer_miles: report.odometerMiles,
    inspection_timestamp: report.inspectionTimestamp,
    inspection_local_date: report.inspectionLocalDate,
    inspection_type: report.inspectionType,
    defects: report.defects.map((defect) => ({
      component: defect.component,
      severity: defect.severity,
      note: defect.note.trim(),
      photo_refs: [...(defect.photoRefs ?? [])],
    })),
  };
}

/**
 * Queue one inspection report, pre-trip or post-trip.
 *
 * The row is durable before this resolves and carries the idempotency key the
 * server deduplicates on (R8.10), so the driver may walk away from the truck
 * with the report still unsent. `orderId` is `null`: an inspection belongs to a
 * vehicle and a day, not to one delivery, so it drains unserialized.
 */
export async function queueInspectionReport(args: {
  report: InspectionReport;
  idempotencyKey?: string;
}): Promise<EnqueueResult> {
  return enqueueMutation({
    kind: 'inspection',
    method: 'POST',
    path: '/api/driver/inspections',
    body: inspectionRequestBody(args.report),
    orderId: null,
    eventTimestamp: args.report.inspectionTimestamp,
    idempotencyKey: args.idempotencyKey ?? generateIdempotencyKey(),
    artifactRefs: inspectionPhotoRefs(args.report),
  });
}
