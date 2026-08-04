/**
 * The active route — the compartment ledger the driver reads and the stop
 * check-in the driver submits.
 *
 * Two things live here, and they are related:
 *
 *  1. **The ledger** (R6.10). For each compartment: the identifier, the loaded
 *     grade, the loaded gallons, and the gallons remaining after each completed
 *     stop. The work detail carries planned per-grade gallons per stop and a
 *     completion status per stop, so the remaining figure is derived rather than
 *     read: every completed stop's per-grade drop is drawn down from the
 *     compartments loaded with that grade, in compartment order, capped at what
 *     each one holds. Nothing is converted — every figure on the wire is already
 *     US gallons (R16.18).
 *  2. **The gate** (R6.11, R6.12). A compartment whose loaded grade differs from
 *     the grade it previously held carries a cross-contamination warning naming
 *     all three facts. While such a warning is unacknowledged and the compartment
 *     records no cleaning event, a check-in drawing from that compartment is
 *     blocked and the acknowledgement prompt is shown instead.
 *
 * The check-in itself goes through the offline queue (R11.8) and sends volumes on
 * `actual_quantities_gallons` with `quantity_unit: "us_gallon"`, built by
 * `lib/units.ts` — this module performs no unit conversion (R6.14, R16.18).
 *
 * Requirements: 6.10, 6.11, 6.12, 6.14, 11.6, 11.8, 11.9, 16.18
 */

import { MMKV } from 'react-native-mmkv';

import {
  enqueueMutation,
  generateIdempotencyKey,
  type EnqueueResult,
} from './offline-queue';
import { gallonsCheckinPayload } from './units';
import type {
  CompartmentManifestEntry,
  FuelOrder,
  RouteStop,
} from '@/types/order';

// ---------------------------------------------------------------------------
// The ledger
// ---------------------------------------------------------------------------

/** One completed stop's draw against one compartment. */
export interface CompartmentDraw {
  sequence: number;
  stationId: string;
  /** US gallons drawn from this compartment at that stop. */
  gallons: number;
  /** US gallons still in the compartment once that stop was complete. */
  remainingAfter: number;
}

/** One row of the compartment manifest, as the route screen renders it (R6.10). */
export interface CompartmentLedgerRow {
  compartmentId: string;
  /** The grade loaded into this compartment for this run. */
  loadedGrade: string;
  /** US gallons loaded. */
  loadedGallons: number;
  /** US gallons left after every completed stop. */
  remainingGallons: number;
  /** One entry per completed stop that drew from this compartment. */
  draws: CompartmentDraw[];
  /** The grade this compartment held before, or `null` when unknown. */
  priorGrade: string | null;
  /** `true` when the prior grade differs from the loaded grade (R6.11). */
  crossContaminationWarning: boolean;
  /** When the compartment was last cleaned, or `null` for never. */
  lastCleanedAt: string | null;
  /** A cleaning event exists for this compartment. */
  cleaningRecorded: boolean;
}

function asGallons(value: number | null | undefined): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? value
    : 0;
}

function completedStops(stops: RouteStop[] | undefined): RouteStop[] {
  return [...(stops ?? [])]
    .filter((stop) => stop.status === 'completed')
    .sort((left, right) => left.sequence - right.sequence);
}

function ledgerRow(entry: CompartmentManifestEntry): CompartmentLedgerRow {
  const loadedGallons = asGallons(entry.planned_gallons);
  return {
    compartmentId: String(entry.compartment_id ?? ''),
    loadedGrade: String(entry.product_grade ?? ''),
    loadedGallons,
    remainingGallons: loadedGallons,
    draws: [],
    priorGrade: entry.prior_product_grade ?? null,
    crossContaminationWarning: Boolean(entry.cross_contamination_warning),
    lastCleanedAt: entry.last_cleaned_at ?? null,
    cleaningRecorded: Boolean(entry.last_cleaned_at),
  };
}

/**
 * Build the ledger for one order's manifest and stop sequence (R6.10).
 *
 * A stop drop of a grade is allocated across the compartments loaded with that
 * grade in manifest order, taking as much as each one still holds. That is the
 * only allocation the driver surface can make honestly: the execution record
 * says which stops are complete and what each one planned, not which compartment
 * each gallon came out of.
 */
export function buildCompartmentLedger(
  order: Pick<FuelOrder, 'compartment_manifest' | 'stops'> | null | undefined,
): CompartmentLedgerRow[] {
  const rows = (order?.compartment_manifest ?? []).map(ledgerRow);
  if (rows.length === 0) {
    return rows;
  }

  for (const stop of completedStops(order?.stops)) {
    for (const [grade, plannedGallons] of Object.entries(
      stop.planned_gallons_by_grade ?? {},
    )) {
      let outstanding = asGallons(plannedGallons);
      if (outstanding <= 0) {
        continue;
      }
      for (const row of rows) {
        if (outstanding <= 0) {
          break;
        }
        if (row.loadedGrade !== grade || row.remainingGallons <= 0) {
          continue;
        }
        const drawn = Math.min(row.remainingGallons, outstanding);
        row.remainingGallons = Number(
          (row.remainingGallons - drawn).toFixed(3),
        );
        outstanding = Number((outstanding - drawn).toFixed(3));
        row.draws.push({
          sequence: stop.sequence,
          stationId: String(stop.station_id ?? ''),
          gallons: drawn,
          remainingAfter: row.remainingGallons,
        });
      }
    }
  }

  return rows;
}

/**
 * The warning sentence, naming the compartment, the prior grade, and the current
 * grade (R6.11). `null` when the compartment carries no warning.
 */
export function crossContaminationMessage(
  row: CompartmentLedgerRow,
): string | null {
  if (!row.crossContaminationWarning) {
    return null;
  }
  const prior = row.priorGrade ?? 'an unrecorded grade';
  return (
    `Compartment ${row.compartmentId} last held ${prior} and is now loaded ` +
    `with ${row.loadedGrade}. Confirm it was cleaned before you draw from it.`
  );
}

// ---------------------------------------------------------------------------
// The acknowledgement store
// ---------------------------------------------------------------------------

const STORAGE_ID = 'runsheet-compartment-acknowledgements';
const KEY_PREFIX = 'ack:';

/** The slice of key-value storage this module needs. Injectable for tests. */
export interface AcknowledgementStore {
  getString(key: string): string | undefined;
  set(key: string, value: string): void;
  delete(key: string): void;
  getAllKeys(): string[];
}

function createMemoryStore(): AcknowledgementStore {
  const map = new Map<string, string>();
  return {
    getString: (key) => map.get(key),
    set: (key, value) => {
      map.set(key, value);
    },
    delete: (key) => {
      map.delete(key);
    },
    getAllKeys: () => Array.from(map.keys()),
  };
}

let store: AcknowledgementStore | null = null;

function resolveStore(): AcknowledgementStore {
  if (!store) {
    try {
      store = new MMKV({ id: STORAGE_ID });
    } catch {
      // No native MMKV here (Jest, web preview). Losing durability means the
      // driver is asked to acknowledge again, which is the safe direction.
      store = createMemoryStore();
    }
  }
  return store;
}

/** Override the store. Tests only. */
export function configureAcknowledgementStore(next: {
  store?: AcknowledgementStore | null;
}): void {
  if (next.store !== undefined) {
    store = next.store;
  }
}

/** Record that the driver has seen and accepted a compartment's warning. */
export function acknowledgeCompartment(compartmentId: string): void {
  resolveStore().set(`${KEY_PREFIX}${compartmentId}`, new Date().toISOString());
}

/** Whether a compartment's warning has been acknowledged on this device. */
export function isCompartmentAcknowledged(compartmentId: string): boolean {
  return Boolean(resolveStore().getString(`${KEY_PREFIX}${compartmentId}`));
}

/** Every acknowledged compartment identifier. */
export function acknowledgedCompartments(): Set<string> {
  return new Set(
    resolveStore()
      .getAllKeys()
      .filter((key) => key.startsWith(KEY_PREFIX))
      .map((key) => key.slice(KEY_PREFIX.length)),
  );
}

/** Drop every acknowledgement — a new load is a new set of facts. */
export function forgetCompartmentAcknowledgements(): void {
  const current = resolveStore();
  current
    .getAllKeys()
    .filter((key) => key.startsWith(KEY_PREFIX))
    .forEach((key) => current.delete(key));
}

// ---------------------------------------------------------------------------
// The gate
// ---------------------------------------------------------------------------

export interface CheckinGate {
  /** `true` when the check-in must not be submitted (R6.12). */
  blocked: boolean;
  /** The compartments whose warnings have to be dealt with first. */
  blockedBy: CompartmentLedgerRow[];
}

/**
 * Decide whether a stop check-in may be submitted (R6.12).
 *
 * A compartment blocks the check-in when all three hold: it carries a
 * cross-contamination warning, it records no cleaning event, and the check-in
 * draws from it — that is, the stop moves the grade that compartment is loaded
 * with and the compartment still holds something.
 */
export function evaluateCheckinGate(args: {
  rows: CompartmentLedgerRow[];
  /** The grades this check-in reports, i.e. the grades it draws. */
  grades: Iterable<string>;
  acknowledged?: ReadonlySet<string>;
}): CheckinGate {
  const grades = new Set(args.grades);
  const acknowledged = args.acknowledged ?? new Set<string>();
  const blockedBy = args.rows.filter(
    (row) =>
      row.crossContaminationWarning &&
      !row.cleaningRecorded &&
      !acknowledged.has(row.compartmentId) &&
      grades.has(row.loadedGrade) &&
      row.remainingGallons > 0,
  );
  return { blocked: blockedBy.length > 0, blockedBy };
}

// ---------------------------------------------------------------------------
// The check-in
// ---------------------------------------------------------------------------

/** Latitude/longitude as the check-in contract declares it (`driver/models.py`). */
export interface CheckinGeotag {
  lat: number;
  lng: number;
}

export interface StopCheckin {
  planId: string;
  routeId: string;
  stationId: string;
  sequence: number;
  /** Links the stop to the order's POD (R6.8). */
  orderId?: string | null;
  /** Grade → US gallons actually dropped. Never litres (R16.18). */
  gallonsByGrade: Record<string, number>;
  geotag: CheckinGeotag;
  eventTimestamp?: string;
}

/**
 * The check-in request body.
 *
 * `actual_quantities_gallons` and `quantity_unit` come from
 * `gallonsCheckinPayload`, so the deprecated litres field `actual_quantities` is
 * never populated by this app and no conversion happens anywhere on the client
 * (R6.14, R6.15, R16.18). Exported for the unit test.
 */
export function checkinRequestBody(checkin: StopCheckin): Record<string, unknown> {
  return {
    route_id: checkin.routeId,
    station_id: checkin.stationId,
    sequence: checkin.sequence,
    ...gallonsCheckinPayload(checkin.gallonsByGrade),
    geotag: { lat: checkin.geotag.lat, lng: checkin.geotag.lng },
    event_timestamp: checkin.eventTimestamp ?? new Date().toISOString(),
    ...(checkin.orderId ? { order_id: checkin.orderId } : {}),
  };
}

/**
 * Queue one stop check-in.
 *
 * The row is durable before this resolves, and it is serialized on `order_id`
 * so a check-in cannot overtake the POD for the same order (R11.11).
 */
export async function queueStopCheckin(args: {
  checkin: StopCheckin;
  idempotencyKey?: string;
}): Promise<EnqueueResult> {
  const body = checkinRequestBody(args.checkin);
  return enqueueMutation({
    kind: 'checkin',
    method: 'POST',
    path: `/api/fuel/mvp/plan/${encodeURIComponent(args.checkin.planId)}/checkin`,
    body,
    orderId: args.checkin.orderId ?? null,
    eventTimestamp: String(body.event_timestamp),
    idempotencyKey: args.idempotencyKey ?? generateIdempotencyKey(),
  });
}
