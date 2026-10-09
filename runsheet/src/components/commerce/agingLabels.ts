/**
 * Display labels for AR aging buckets. Invoices are aged by days past their
 * due date (F12); "Current" is not yet due. Kept outside commerceApi so
 * tests that mock the API module still get real labels.
 */
export const AGING_LABELS = {
  current: "Current (not yet due)",
  d1_30: "1–30 days past due",
  d31_60: "31–60 days past due",
  d61_90: "61–90 days past due",
  d90_plus: "90+ days past due",
} as const;
