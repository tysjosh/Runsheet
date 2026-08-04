/**
 * `lib/qualification-api.ts` — the expiry thresholds and the ineligibility
 * banner behind the profile screen's qualification section.
 *
 * The boundaries are what matter here: 60 days is the last advisory day, 61 is
 * not advisory at all, 7 is the first urgent day, and 8 is advisory rather than
 * urgent. A screen test would exercise the same arithmetic through a render
 * tree; these assertions sit directly on it.
 *
 * **Validates: Requirements 12.3, 12.4, 12.5, 12.7**
 */

import {
  ADVISORY_WINDOW_DAYS,
  daysRemainingLabel,
  daysUntil,
  expiryUrgency,
  formatQualificationDate,
  ineligibilityReasons,
  NO_DATE,
  qualificationItems,
  qualificationStatusLabel,
  URGENT_WINDOW_DAYS,
  type DriverQualifications,
} from '@/lib/qualification-api';

/** Noon local, so a device offset cannot shift the reference calendar day. */
const NOW = new Date(2026, 6, 29, 12, 0, 0);

/** `YYYY-MM-DD` for the calendar day `days` out from {@link NOW}. */
function isoDaysFromNow(days: number): string {
  const anchor = new Date(
    Date.UTC(NOW.getFullYear(), NOW.getMonth(), NOW.getDate()),
  );
  anchor.setUTCDate(anchor.getUTCDate() + days);
  return anchor.toISOString().slice(0, 10);
}

function record(overrides: Partial<DriverQualifications> = {}): DriverQualifications {
  return {
    driver_id: 'drv-1',
    cdl_class: 'A',
    qualification_status: 'active',
    is_dispatch_eligible: true,
    ineligibility_reasons: [],
    cdl_expiry_date: isoDaysFromNow(400),
    medical_card_expiry_date: isoDaysFromNow(400),
    hazmat_endorsement_expiry_date: null,
    tanker_endorsement_expiry_date: null,
    last_drug_test_date: '2026-02-11',
    ...overrides,
  };
}

describe('days remaining', () => {
  it('counts whole calendar days, zero on the expiry day itself', () => {
    expect(daysUntil(isoDaysFromNow(0), NOW)).toBe(0);
    expect(daysUntil(isoDaysFromNow(1), NOW)).toBe(1);
    expect(daysUntil(isoDaysFromNow(60), NOW)).toBe(60);
    expect(daysUntil(isoDaysFromNow(-3), NOW)).toBe(-3);
  });

  it('answers null for an absent or unusable date rather than guessing', () => {
    expect(daysUntil(null, NOW)).toBeNull();
    expect(daysUntil(undefined, NOW)).toBeNull();
    expect(daysUntil('', NOW)).toBeNull();
    expect(daysUntil('not-a-date', NOW)).toBeNull();
    expect(daysUntil('2026-02-30', NOW)).toBeNull();
  });
});

describe('threshold classification', () => {
  it('treats 60 days as the outer edge of the advisory window (R12.3)', () => {
    expect(ADVISORY_WINDOW_DAYS).toBe(60);
    expect(expiryUrgency(60)).toBe('advisory');
    expect(expiryUrgency(59)).toBe('advisory');
    expect(expiryUrgency(61)).toBe('ok');
  });

  it('treats 7 days as the outer edge of the urgent window (R12.4)', () => {
    expect(URGENT_WINDOW_DAYS).toBe(7);
    expect(expiryUrgency(7)).toBe('urgent');
    expect(expiryUrgency(8)).toBe('advisory');
    expect(expiryUrgency(0)).toBe('urgent');
  });

  it('separates an already-lapsed item from a countdown', () => {
    expect(expiryUrgency(-1)).toBe('expired');
    expect(expiryUrgency(null)).toBe('unknown');
  });
});

describe('qualification items', () => {
  it('carries a days-remaining indicator on both indicator tiers', () => {
    const items = qualificationItems(
      record({
        cdl_expiry_date: isoDaysFromNow(45),
        medical_card_expiry_date: isoDaysFromNow(5),
      }),
      NOW,
    );
    const cdl = items.find((item) => item.key === 'cdl');
    const medical = items.find((item) => item.key === 'medical_card');

    expect(cdl).toMatchObject({ urgency: 'advisory', daysRemaining: 45 });
    expect(cdl?.indicatorLabel).toBe('Expires in 45 days');
    expect(medical).toMatchObject({ urgency: 'urgent', daysRemaining: 5 });
    expect(medical?.indicatorLabel).toBe('Expires in 5 days — renew now');
  });

  it('raises no indicator beyond the advisory window or without a date', () => {
    const items = qualificationItems(
      record({ cdl_expiry_date: isoDaysFromNow(61) }),
      NOW,
    );
    expect(items.find((item) => item.key === 'cdl')?.indicatorLabel).toBeNull();
    const hazmat = items.find((item) => item.key === 'hazmat_endorsement');
    expect(hazmat).toMatchObject({
      expiryDate: null,
      daysRemaining: null,
      urgency: 'unknown',
      indicatorLabel: null,
    });
  });

  it('shows the four expiring items and leaves the drug-test date out of them', () => {
    const keys = qualificationItems(record(), NOW).map((item) => item.key);
    expect(keys).toEqual([
      'cdl',
      'medical_card',
      'hazmat_endorsement',
      'tanker_endorsement',
    ]);
  });

  it('reads today and tomorrow as urgent with a readable count', () => {
    const items = qualificationItems(
      record({
        cdl_expiry_date: isoDaysFromNow(0),
        medical_card_expiry_date: isoDaysFromNow(1),
      }),
      NOW,
    );
    expect(items.find((item) => item.key === 'cdl')?.indicatorLabel).toBe(
      'Expires today — renew now',
    );
    expect(
      items.find((item) => item.key === 'medical_card')?.indicatorLabel,
    ).toBe('Expires in 1 day — renew now');
    expect(daysRemainingLabel(0)).toBe('today');
    expect(daysRemainingLabel(1)).toBe('1 day');
    expect(daysRemainingLabel(9)).toBe('9 days');
  });
});

describe('ineligibility banner (R12.5)', () => {
  it('lists the reasons the server computed, verbatim', () => {
    expect(
      ineligibilityReasons(
        record({
          is_dispatch_eligible: false,
          ineligibility_reasons: ['medical_card_expired', ' cdl_expired '],
        }),
        NOW,
      ),
    ).toEqual(['medical_card_expired', 'cdl_expired']);
  });

  it('names the expired items when the server sent no reasons', () => {
    const reasons = ineligibilityReasons(
      record({
        is_dispatch_eligible: false,
        ineligibility_reasons: [],
        medical_card_expiry_date: '2026-06-30',
      }),
      NOW,
    );
    expect(reasons).toEqual(['Medical card expired on Jun 30, 2026']);
  });

  it('never renders an empty banner', () => {
    const reasons = ineligibilityReasons(
      record({ is_dispatch_eligible: false, ineligibility_reasons: null }),
      NOW,
    );
    expect(reasons).toHaveLength(1);
    expect(reasons[0]).toContain('Contact the office');
  });
});

describe('the compliance vocabulary stays its own (R12.7)', () => {
  it('labels the compliance statuses, not the duty statuses', () => {
    expect(qualificationStatusLabel('active')).toBe('Qualified');
    expect(qualificationStatusLabel('suspended')).toBe('Suspended');
    expect(qualificationStatusLabel('expired')).toBe('Expired');
    // Duty-status values are not compliance values, so they resolve to nothing.
    expect(qualificationStatusLabel('on_break')).toBe('Unknown');
    expect(qualificationStatusLabel('off_duty')).toBe('Unknown');
  });
});

describe('date rendering', () => {
  it('renders the stored calendar day, not a timezone-shifted one', () => {
    expect(formatQualificationDate('2026-01-01')).toBe('Jan 1, 2026');
    expect(formatQualificationDate('2026-12-31')).toBe('Dec 31, 2026');
  });

  it('states that a date is absent rather than rendering an invalid one', () => {
    expect(formatQualificationDate(null)).toBe(NO_DATE);
    expect(formatQualificationDate('nope')).toBe(NO_DATE);
  });
});
