/**
 * `lib/route-api.ts` — the compartment ledger, the cross-contamination warning,
 * the check-in gate, and the check-in request body.
 *
 * **Validates: Requirements 6.10, 6.11, 6.12, 6.14, 16.18**
 */

import {
  buildCompartmentLedger,
  checkinRequestBody,
  configureAcknowledgementStore,
  crossContaminationMessage,
  evaluateCheckinGate,
  type AcknowledgementStore,
  type CompartmentLedgerRow,
} from '@/lib/route-api';
import type { CompartmentManifestEntry, FuelOrder, RouteStop } from '@/types/order';

function memoryStore(): AcknowledgementStore {
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

function compartment(
  overrides: Partial<CompartmentManifestEntry> & { compartment_id: string },
): CompartmentManifestEntry {
  return {
    product_grade: 'Diesel #2',
    planned_gallons: 3000,
    prior_product_grade: 'Diesel #2',
    cross_contamination_warning: false,
    last_cleaned_at: null,
    ...overrides,
  };
}

function stop(overrides: Partial<RouteStop> & { sequence: number }): RouteStop {
  return {
    station_id: `STATION-${overrides.sequence}`,
    lat: 29.76,
    lon: -95.37,
    planned_arrival: null,
    planned_gallons_by_grade: {},
    status: 'pending',
    ...overrides,
  };
}

function order(
  manifest: CompartmentManifestEntry[],
  stops: RouteStop[],
): Pick<FuelOrder, 'compartment_manifest' | 'stops'> {
  return { compartment_manifest: manifest, stops };
}

beforeEach(() => {
  configureAcknowledgementStore({ store: memoryStore() });
});

afterEach(() => {
  configureAcknowledgementStore({ store: null });
});

describe('buildCompartmentLedger', () => {
  it('reports the identifier, the loaded grade, and the loaded gallons (R6.10)', () => {
    const [row] = buildCompartmentLedger(
      order([compartment({ compartment_id: 'C-1', planned_gallons: 2500 })], []),
    );

    expect(row.compartmentId).toBe('C-1');
    expect(row.loadedGrade).toBe('Diesel #2');
    expect(row.loadedGallons).toBe(2500);
    expect(row.remainingGallons).toBe(2500);
    expect(row.draws).toEqual([]);
  });

  it('draws down the remaining gallons after each completed stop (R6.10)', () => {
    const [row] = buildCompartmentLedger(
      order(
        [compartment({ compartment_id: 'C-1', planned_gallons: 3000 })],
        [
          stop({
            sequence: 0,
            status: 'completed',
            planned_gallons_by_grade: { 'Diesel #2': 1200 },
          }),
          stop({
            sequence: 1,
            status: 'completed',
            planned_gallons_by_grade: { 'Diesel #2': 800 },
          }),
          stop({
            sequence: 2,
            planned_gallons_by_grade: { 'Diesel #2': 1000 },
          }),
        ],
      ),
    );

    expect(row.draws.map((draw) => draw.remainingAfter)).toEqual([1800, 1000]);
    // The pending stop is not drawn down: it has not happened yet.
    expect(row.remainingGallons).toBe(1000);
  });

  it('spills a completed drop into the next compartment of the same grade', () => {
    const rows = buildCompartmentLedger(
      order(
        [
          compartment({ compartment_id: 'C-1', planned_gallons: 1000 }),
          compartment({ compartment_id: 'C-2', planned_gallons: 1000 }),
        ],
        [
          stop({
            sequence: 0,
            status: 'completed',
            planned_gallons_by_grade: { 'Diesel #2': 1500 },
          }),
        ],
      ),
    );

    expect(rows[0].remainingGallons).toBe(0);
    expect(rows[1].remainingGallons).toBe(500);
  });

  it('leaves a compartment of another grade untouched', () => {
    const rows = buildCompartmentLedger(
      order(
        [
          compartment({ compartment_id: 'C-1', planned_gallons: 1000 }),
          compartment({
            compartment_id: 'C-2',
            product_grade: 'Regular 87',
            planned_gallons: 900,
          }),
        ],
        [
          stop({
            sequence: 0,
            status: 'completed',
            planned_gallons_by_grade: { 'Diesel #2': 400 },
          }),
        ],
      ),
    );

    expect(rows[0].remainingGallons).toBe(600);
    expect(rows[1].remainingGallons).toBe(900);
  });

  it('returns nothing when no manifest was resolved', () => {
    expect(buildCompartmentLedger(order([], []))).toEqual([]);
    expect(buildCompartmentLedger(null)).toEqual([]);
  });
});

describe('crossContaminationMessage', () => {
  it('names the compartment, the prior grade, and the current grade (R6.11)', () => {
    const [row] = buildCompartmentLedger(
      order(
        [
          compartment({
            compartment_id: 'C-2',
            product_grade: 'Diesel #2',
            prior_product_grade: 'Regular 87',
            cross_contamination_warning: true,
          }),
        ],
        [],
      ),
    );

    const warning = crossContaminationMessage(row) ?? '';
    expect(warning).toContain('C-2');
    expect(warning).toContain('Regular 87');
    expect(warning).toContain('Diesel #2');
  });

  it('says nothing about a compartment carrying no warning', () => {
    const [row] = buildCompartmentLedger(
      order([compartment({ compartment_id: 'C-1' })], []),
    );
    expect(crossContaminationMessage(row)).toBeNull();
  });
});

describe('evaluateCheckinGate', () => {
  function warned(overrides: Partial<CompartmentLedgerRow> = {}): CompartmentLedgerRow {
    const [row] = buildCompartmentLedger(
      order(
        [
          compartment({
            compartment_id: 'C-2',
            prior_product_grade: 'Regular 87',
            cross_contamination_warning: true,
          }),
        ],
        [],
      ),
    );
    return { ...row, ...overrides };
  }

  it('blocks a check-in drawing from an unacknowledged, uncleaned compartment (R6.12)', () => {
    const gate = evaluateCheckinGate({
      rows: [warned()],
      grades: ['Diesel #2'],
    });

    expect(gate.blocked).toBe(true);
    expect(gate.blockedBy.map((row) => row.compartmentId)).toEqual(['C-2']);
  });

  it('allows the check-in once the warning is acknowledged', () => {
    const gate = evaluateCheckinGate({
      rows: [warned()],
      grades: ['Diesel #2'],
      acknowledged: new Set(['C-2']),
    });

    expect(gate.blocked).toBe(false);
  });

  it('allows the check-in when a cleaning event is recorded', () => {
    const gate = evaluateCheckinGate({
      rows: [warned({ cleaningRecorded: true })],
      grades: ['Diesel #2'],
    });

    expect(gate.blocked).toBe(false);
  });

  it('does not block a check-in that draws another grade', () => {
    const gate = evaluateCheckinGate({
      rows: [warned()],
      grades: ['Regular 87'],
    });

    expect(gate.blocked).toBe(false);
  });

  it('does not block once the compartment is empty', () => {
    const gate = evaluateCheckinGate({
      rows: [warned({ remainingGallons: 0 })],
      grades: ['Diesel #2'],
    });

    expect(gate.blocked).toBe(false);
  });
});

describe('checkinRequestBody', () => {
  const body = checkinRequestBody({
    planId: 'plan_1',
    routeId: 'route_1',
    stationId: 'STATION-1',
    sequence: 2,
    orderId: 'ord_1',
    gallonsByGrade: { 'Diesel #2': 1800.5 },
    geotag: { lat: 29.76, lng: -95.37 },
    eventTimestamp: '2026-05-01T08:00:00.000Z',
  });

  it('sends the gallons field with the us_gallon unit (R6.14)', () => {
    expect(body.actual_quantities_gallons).toEqual({ 'Diesel #2': 1800.5 });
    expect(body.quantity_unit).toBe('us_gallon');
  });

  it('never populates the deprecated litres field (R6.15, R16.18)', () => {
    expect(body).not.toHaveProperty('actual_quantities');
  });

  it('carries the geotag, the client event timestamp, and the order link', () => {
    expect(body.geotag).toEqual({ lat: 29.76, lng: -95.37 });
    expect(body.event_timestamp).toBe('2026-05-01T08:00:00.000Z');
    expect(body.order_id).toBe('ord_1');
    expect(body.route_id).toBe('route_1');
    expect(body.station_id).toBe('STATION-1');
    expect(body.sequence).toBe(2);
  });
});
