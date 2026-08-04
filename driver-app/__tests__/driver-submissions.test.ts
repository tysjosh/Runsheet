/**
 * The request bodies and the duty-status mapping the Phase 1 screens submit.
 *
 * **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.8, 7.1, 13.4, 13.5, 13.10, 5.5, 5.6**
 */

import {
  cleaningEventRequestBody,
  CLEANING_METHODS,
  waitMinutesBetween,
  waitReportRequestBody,
} from '@/lib/ops-api';
import { exceptionRequestBody } from '@/lib/exception-api';
import {
  componentLabel,
  INSPECTION_COMPONENTS,
  inspectionPhotoRefs,
  inspectionRequestBody,
  localCalendarDay,
  type InspectionReport,
} from '@/lib/inspection-api';
import {
  adoptServerDutyStatus,
  configureDutyStatusStore,
  controlForStatus,
  DUTY_CONTROLS,
  DUTY_CONTROL_STATUS,
  storedDutyStatus,
  storeDutyStatus,
  type DutyStatusStore,
} from '@/lib/duty-api';
import { approximateBase64Bytes, isPresignExpired } from '@/lib/pod-api';

function memoryStore(): DutyStatusStore {
  const map = new Map<string, string>();
  return {
    getString: (key) => map.get(key),
    set: (key, value) => {
      map.set(key, value);
    },
    delete: (key) => {
      map.delete(key);
    },
  };
}

describe('terminal wait reports (R8.1)', () => {
  const body = waitReportRequestBody({
    terminalId: 'term_001',
    waitStart: '2026-05-01T08:00:00.000Z',
    waitEnd: '2026-05-01T08:45:00.000Z',
    driverId: 'driver_17',
    notes: '  two racks down  ',
  });

  it('omits source so the server default driver_report applies', () => {
    expect(body).not.toHaveProperty('source');
  });

  it('reports the driver-observed times, not the transmission time', () => {
    expect(body.observed_at).toBe('2026-05-01T08:45:00.000Z');
    expect(body.wait_minutes).toBe(45);
  });

  it('attributes the observation to the session driver', () => {
    expect(body.reporter_id).toBe('driver_17');
  });

  it('trims the note and drops it when empty', () => {
    expect(body.notes).toBe('two racks down');
    expect(
      waitReportRequestBody({
        terminalId: 'term_001',
        waitStart: '2026-05-01T08:00:00.000Z',
        waitEnd: '2026-05-01T08:10:00.000Z',
        driverId: 'driver_17',
        notes: '   ',
      }),
    ).not.toHaveProperty('notes');
  });

  it('never reports a negative wait', () => {
    expect(
      waitMinutesBetween('2026-05-01T09:00:00Z', '2026-05-01T08:00:00Z'),
    ).toBe(0);
    expect(waitMinutesBetween('not a time', '2026-05-01T08:00:00Z')).toBe(0);
  });
});

describe('compartment cleaning events (R8.2)', () => {
  const body = cleaningEventRequestBody({
    compartmentId: 'C-2',
    method: 'purge',
    driverId: 'driver_17',
    evidenceRefs: ['tenants/t1/photo/2026/05/01/a.jpg'],
    notes: 'purged and witnessed',
  });

  it('carries the session driver id on the canonical field and the alias', () => {
    expect(body.driver_id).toBe('driver_17');
    expect(body.actor_id).toBe('driver_17');
  });

  it('carries a method from the accepted set', () => {
    expect(CLEANING_METHODS).toContain(body.method);
  });

  it('carries the evidence file_ref values', () => {
    expect(body.evidence_refs).toEqual(['tenants/t1/photo/2026/05/01/a.jpg']);
  });

  it('omits evidence_refs when there is none', () => {
    expect(
      cleaningEventRequestBody({
        compartmentId: 'C-2',
        method: 'flush',
        driverId: 'driver_17',
      }),
    ).not.toHaveProperty('evidence_refs');
  });
});

describe('exception reports (R7.1)', () => {
  it('carries the type, the severity, the note, the geotag, and the media refs', () => {
    expect(
      exceptionRequestBody({
        exceptionType: 'vehicle_breakdown',
        severity: 'high',
        note: '  air line blew on the ramp  ',
        geotag: { lat: 29.76, lng: -95.37 },
        mediaRefs: ['tenants/t1/photo/2026/05/01/b.jpg'],
      }),
    ).toEqual({
      exception_type: 'vehicle_breakdown',
      severity: 'high',
      note: 'air line blew on the ramp',
      location: { lat: 29.76, lng: -95.37 },
      media_refs: ['tenants/t1/photo/2026/05/01/b.jpg'],
    });
  });

  it('omits the location when precise location is denied (R10.15)', () => {
    const body = exceptionRequestBody({
      exceptionType: 'weather',
      severity: 'low',
      note: 'ice on the approach',
      geotag: null,
    });
    expect(body).not.toHaveProperty('location');
  });
});

describe('inspection reports (R8.3, R8.4, R8.8)', () => {
  const report = (
    overrides: Partial<InspectionReport> = {},
  ): InspectionReport => ({
    inspectionType: 'pre_trip',
    assetId: '  truck_7  ',
    odometerMiles: 128450.5,
    inspectionTimestamp: '2026-05-01T06:15:00.000Z',
    inspectionLocalDate: '2026-05-01',
    defects: [],
    ...overrides,
  });

  it('carries the asset, the odometer in miles, the stamp, and the day', () => {
    expect(inspectionRequestBody(report())).toEqual({
      asset_id: 'truck_7',
      odometer_miles: 128450.5,
      inspection_timestamp: '2026-05-01T06:15:00.000Z',
      inspection_local_date: '2026-05-01',
      inspection_type: 'pre_trip',
      defects: [],
    });
  });

  it('never sends a driver_id — the server takes it from the session', () => {
    expect(inspectionRequestBody(report())).not.toHaveProperty('driver_id');
  });

  it('sends post_trip on the same field set as pre_trip (R8.8)', () => {
    const pre = inspectionRequestBody(report({ inspectionType: 'pre_trip' }));
    const post = inspectionRequestBody(report({ inspectionType: 'post_trip' }));

    expect(post.inspection_type).toBe('post_trip');
    expect(Object.keys(post).sort()).toEqual(Object.keys(pre).sort());
    expect({ ...post, inspection_type: 'pre_trip' }).toEqual(pre);
  });

  it('carries each defect as component, severity, note, and photo refs', () => {
    const body = inspectionRequestBody(
      report({
        defects: [
          {
            component: 'service_brakes',
            severity: 'out_of_service',
            note: '  left front line weeping  ',
            photoRefs: ['tenants/t1/photo/2026/05/01/c.jpg'],
          },
          { component: 'horn', severity: 'minor', note: 'intermittent' },
        ],
      }),
    );

    expect(body.defects).toEqual([
      {
        component: 'service_brakes',
        severity: 'out_of_service',
        note: 'left front line weeping',
        photo_refs: ['tenants/t1/photo/2026/05/01/c.jpg'],
      },
      {
        component: 'horn',
        severity: 'minor',
        note: 'intermittent',
        photo_refs: [],
      },
    ]);
  });

  it('collects every photo ref so the bytes are retained (R11.16)', () => {
    expect(
      inspectionPhotoRefs(
        report({
          defects: [
            { component: 'tires', severity: 'minor', note: '', photoRefs: ['a'] },
            { component: 'pump', severity: 'minor', note: '' },
            { component: 'other', severity: 'minor', note: '', photoRefs: ['b'] },
          ],
        }),
      ),
    ).toEqual(['a', 'b']);
  });

  it('derives the calendar day from the local date, not the UTC date', () => {
    // 2026-05-01T19:30 local is 2026-05-02 in UTC east of the meridian, and the
    // driver's day is the local one.
    const local = new Date(2026, 4, 1, 19, 30, 0);
    expect(localCalendarDay(local)).toBe('2026-05-01');
  });

  it('labels every declared component without inventing a second vocabulary', () => {
    expect(INSPECTION_COMPONENTS).toContain('service_brakes');
    expect(INSPECTION_COMPONENTS).toContain('other');
    expect(componentLabel('cargo_tank_valves')).toBe('Cargo tank valves');
  });
});

describe('duty status (R13.4, R13.5, R13.10)', () => {
  beforeEach(() => {
    configureDutyStatusStore({ store: memoryStore() });
  });

  afterEach(() => {
    configureDutyStatusStore({ store: null });
  });

  it('maps the on-duty control to active and the off-duty control to off_duty', () => {
    expect(DUTY_CONTROL_STATUS.on_duty).toBe('active');
    expect(DUTY_CONTROL_STATUS.off_duty).toBe('off_duty');
  });

  it('presents on_break as a control of its own', () => {
    expect(DUTY_CONTROL_STATUS.on_break).toBe('on_break');
    expect(DUTY_CONTROLS.map((entry) => entry.control)).toEqual([
      'on_duty',
      'on_break',
      'off_duty',
    ]);
  });

  it('offers no control for the administrator-set inactive value (R13.2)', () => {
    expect(DUTY_CONTROLS.map((entry) => String(entry.status))).toEqual([
      'active',
      'on_break',
      'off_duty',
    ]);
    expect(controlForStatus('inactive')).toBeNull();
  });

  it('adopts the server value when the stored one differs', () => {
    storeDutyStatus('off_duty');

    const adoption = adoptServerDutyStatus('active');

    expect(adoption).toEqual({
      status: 'active',
      adopted: true,
      previous: 'off_duty',
    });
    expect(storedDutyStatus()).toBe('active');
  });

  it('adopts nothing when the two already agree', () => {
    storeDutyStatus('active');
    expect(adoptServerDutyStatus('active').adopted).toBe(false);
  });

  it('keeps the stored value when the server answers with nothing usable', () => {
    storeDutyStatus('on_break');

    expect(adoptServerDutyStatus(null).status).toBe('on_break');
    expect(adoptServerDutyStatus('driving').status).toBe('on_break');
    expect(storedDutyStatus()).toBe('on_break');
  });
});

describe('POD upload preconditions (R5.5, R5.6)', () => {
  it('measures the decoded size of a base64 payload, not the string length', () => {
    // "AAAA" encodes three bytes.
    expect(approximateBase64Bytes('AAAA')).toBe(3);
    expect(approximateBase64Bytes('AAAA\nAAAA')).toBe(6);
    expect(approximateBase64Bytes('AAA=')).toBe(2);
  });

  it('treats a presigned URL as expired only once expires_at has passed', () => {
    const now = Date.parse('2026-05-01T08:00:00.000Z');
    expect(isPresignExpired('2026-05-01T07:59:59.000Z', now)).toBe(true);
    expect(isPresignExpired('2026-05-01T08:00:01.000Z', now)).toBe(false);
    expect(isPresignExpired(null, now)).toBe(false);
    expect(isPresignExpired('not a date', now)).toBe(false);
  });
});
