/**
 * `lib/hos-api.ts` — the 60-minute threshold, the out-of-hours mapping, and the
 * refusal to render an unavailable figure as zero.
 *
 * The boundaries are what matter: 60 minutes is the last approaching-limit
 * minute, 61 is within limits, 0 is at-limit, and an `unavailable` figure is
 * neither — a Geotab tenant supplies no figures at all, and showing that as
 * `0 minutes` would tell a driver they are out of hours.
 *
 * **Validates: Requirements 16.20, 17.13, 17.15, 17.16, 17.27**
 */

import {
  APPROACHING_LIMIT_WINDOW_MINUTES,
  DRIVE_LIMIT_NAME,
  FIGURE_UNAVAILABLE,
  figureMinutes,
  formatHOSFigure,
  hosFigureRows,
  hosLimitMessage,
  hosLimitState,
  hosReadingExplanation,
  isOutOfHoursDutyStatus,
  minutesLabel,
  readingAgeLabel,
  remainingDriveMinutes,
  type HOSAdvisory,
  type HOSFigure,
} from '@/lib/hos-api';

const UNAVAILABLE: HOSFigure = { availability: 'unavailable', advisory: true };

function hours(value: number): HOSFigure {
  return { availability: 'available', value, unit: 'hours', advisory: true };
}

/** A fresh reading whose connector supplies every figure. */
function advisory(overrides: Partial<HOSAdvisory> = {}): HOSAdvisory {
  return {
    tenant_id: 'tenant-1',
    driver_id: 'drv-1',
    freshness_state: 'fresh',
    compliance_state: 'within_limits',
    reason_code: null,
    truck_id: 'trk-1',
    duty_status: 'Driving',
    recorded_at: '2026-07-29T12:00:00+00:00',
    reading_age_seconds: 12,
    freshness_window_seconds: 300,
    provider_name: 'geotab',
    remaining_drive_time: hours(4),
    remaining_on_duty_window: hours(6),
    cycle_hours: hours(38),
    ...overrides,
  };
}

/** The Geotab tenant: a duty-status string and nothing else (R17.13). */
function geotabAdvisory(overrides: Partial<HOSAdvisory> = {}): HOSAdvisory {
  return advisory({
    compliance_state: 'unknown',
    remaining_drive_time: UNAVAILABLE,
    remaining_on_duty_window: UNAVAILABLE,
    cycle_hours: UNAVAILABLE,
    ...overrides,
  });
}

describe('hours to minutes, converted once', () => {
  it('reads the wire figure in hours as whole minutes', () => {
    expect(figureMinutes(hours(1))).toBe(60);
    expect(figureMinutes(hours(0.25))).toBe(15);
    expect(figureMinutes(hours(0))).toBe(0);
  });

  it('answers null for a figure it cannot convert, never zero', () => {
    expect(figureMinutes(UNAVAILABLE)).toBeNull();
    expect(figureMinutes(null)).toBeNull();
    expect(figureMinutes({ availability: 'available', value: null, unit: 'hours' })).toBeNull();
    expect(
      figureMinutes({ availability: 'available', value: 2, unit: 'minutes' }),
    ).toBeNull();
  });
});

describe('threshold classification (R17.15, R17.16)', () => {
  it('treats 60 minutes as the outer edge of the approaching-limit window', () => {
    expect(APPROACHING_LIMIT_WINDOW_MINUTES).toBe(60);
    expect(hosLimitState(advisory({ remaining_drive_time: hours(1) }))).toBe(
      'approaching_limit',
    );
    expect(hosLimitState(advisory({ remaining_drive_time: hours(0.75) }))).toBe(
      'approaching_limit',
    );
    expect(
      hosLimitState(advisory({ remaining_drive_time: hours(61 / 60) })),
    ).toBe('within_limits');
  });

  it('treats zero remaining minutes as at-limit, not as approaching it', () => {
    expect(hosLimitState(advisory({ remaining_drive_time: hours(0) }))).toBe(
      'at_limit',
    );
    expect(hosLimitState(advisory({ remaining_drive_time: hours(-1) }))).toBe(
      'at_limit',
    );
  });

  it('names the limit and the remaining minutes on the advisory (R17.15)', () => {
    const message = hosLimitMessage(
      advisory({ remaining_drive_time: hours(0.5) }),
    );
    expect(message).toContain('30 minutes');
    expect(message).toContain(DRIVE_LIMIT_NAME);
  });

  it('names the limit and no countdown at the limit (R17.16)', () => {
    const message = hosLimitMessage(advisory({ remaining_drive_time: hours(0) }));
    expect(message).toContain(DRIVE_LIMIT_NAME);
    expect(message).not.toContain('0 minutes');
  });

  it('raises no line while within limits or while no figure exists', () => {
    expect(hosLimitMessage(advisory())).toBeNull();
    expect(hosLimitMessage(geotabAdvisory())).toBeNull();
    expect(minutesLabel(1)).toBe('1 minute');
    expect(minutesLabel(45)).toBe('45 minutes');
  });
});

describe('an out-of-hours duty status is at-limit on its own (R17.16)', () => {
  it('maps the configured vendor values regardless of spelling', () => {
    expect(isOutOfHoursDutyStatus('OutOfHours')).toBe(true);
    expect(isOutOfHoursDutyStatus('out_of_hours')).toBe(true);
    expect(isOutOfHoursDutyStatus('HOS-Violation')).toBe(true);
    expect(
      hosLimitState(geotabAdvisory({ duty_status: 'OutOfHours' })),
    ).toBe('at_limit');
    expect(
      hosLimitMessage(geotabAdvisory({ duty_status: 'OutOfHours' })),
    ).toContain(DRIVE_LIMIT_NAME);
  });

  it('leaves the Geotab rest states alone — a break is not exhausted hours', () => {
    for (const status of [
      'Driving',
      'OnDuty',
      'SB',
      'OffDuty',
      'PersonalConveyance',
      'YardMove',
    ]) {
      expect(isOutOfHoursDutyStatus(status)).toBe(false);
    }
    expect(isOutOfHoursDutyStatus(null)).toBe(false);
    expect(isOutOfHoursDutyStatus('')).toBe(false);
  });

  it('never routes a Runsheet duty status through this mapping (R17.27)', () => {
    // The availability vocabulary of R13 is not an Hours-of-Service vocabulary.
    for (const status of ['active', 'inactive', 'on_break', 'off_duty']) {
      expect(isOutOfHoursDutyStatus(status)).toBe(false);
    }
  });
});

describe('an unavailable figure is rendered honestly (R17.13)', () => {
  it('classifies a duty-status-only connector as unavailable, not within limits', () => {
    expect(hosLimitState(geotabAdvisory())).toBe('unavailable');
    expect(remainingDriveMinutes(geotabAdvisory())).toBeNull();
    expect(hosLimitState(null)).toBe('unavailable');
  });

  it('says the figure is not supplied instead of showing zero', () => {
    expect(formatHOSFigure(UNAVAILABLE)).toBe(FIGURE_UNAVAILABLE);
    const displays = hosFigureRows(geotabAdvisory()).map((row) => row.display);
    expect(displays).toEqual([
      FIGURE_UNAVAILABLE,
      FIGURE_UNAVAILABLE,
      FIGURE_UNAVAILABLE,
    ]);
    expect(formatHOSFigure(hours(0))).toBe('0 minutes');
  });

  it('honours a server at-limit state even with no convertible figure', () => {
    expect(
      hosLimitState(geotabAdvisory({ compliance_state: 'at_limit' })),
    ).toBe('at_limit');
  });

  it('formats an available figure with its unit', () => {
    expect(formatHOSFigure(hours(4))).toBe('4 hours');
    expect(formatHOSFigure(hours(4.5))).toBe('4.5 hours');
    expect(formatHOSFigure(hours(0.75))).toBe('45 minutes');
  });

  it('lists the three figures in a fixed order', () => {
    expect(hosFigureRows(advisory()).map((row) => row.key)).toEqual([
      'remaining_drive_time',
      'remaining_on_duty_window',
      'cycle_hours',
    ]);
  });
});

describe('why the advisory says what it says', () => {
  it('explains each reason code in the driver’s words', () => {
    expect(
      hosReadingExplanation(
        geotabAdvisory({
          freshness_state: 'unknown',
          reason_code: 'HOS_TRUCK_UNASSIGNED',
        }),
      ),
    ).toContain('No truck is assigned');
    expect(
      hosReadingExplanation(
        geotabAdvisory({ freshness_state: 'stale', reason_code: 'HOS_READING_STALE' }),
      ),
    ).toContain('freshness window');
    expect(hosReadingExplanation(advisory())).toBeNull();
  });

  it('reports the reading age without inventing one', () => {
    expect(readingAgeLabel(advisory({ reading_age_seconds: 12 }))).toBe(
      'Recorded 12 seconds ago',
    );
    expect(readingAgeLabel(advisory({ reading_age_seconds: 180 }))).toBe(
      'Recorded 3 minutes ago',
    );
    expect(readingAgeLabel(advisory({ reading_age_seconds: null }))).toBeNull();
  });
});
