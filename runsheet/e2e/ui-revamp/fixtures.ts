/**
 * List fixtures for the UI revamp specs (design.md §6). Pages measured in
 * Phase 2 (Dashboard, Dispatch Board, Jobs, Plans, Orders, Live) each get a
 * data row from these; Phase 3 pages add theirs as they migrate (until then
 * their chrome spec stays `test.fail` and some render an empty state). Values are
 * synthetic (QA- prefixes, example.com) and deterministic.
 */
export const TENANT = "e2e-tenant";
const DAY = "2026-10-08";
const iso = (h: number, m = 0) =>
  `${DAY}T${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:00Z`;

const PRODUCTS = [
  "DIESEL_2",
  "GASOLINE_REG",
  "HEATING_OIL",
  "OFF_ROAD_DIESEL",
  "PROPANE",
  "GASOLINE_PREM",
  "KEROSENE",
  "DEF",
];
const ORDER_STATUSES = [
  "placed",
  "confirmed",
  "on_hold",
  "assigned",
  "in_transit",
  "delivered",
];

export const ORDERS = Array.from({ length: 24 }, (_, i) => ({
  order_id: `QA-ORD-${String(1000 + i)}`,
  tenant_id: TENANT,
  customer_id: `QA-CUST-${i % 6}`,
  customer_name: `QA Customer ${i % 6}`,
  ship_to_address: `${100 + i} Example Rd, Springfield`,
  ship_to_lat: 39.78 + i / 1000,
  ship_to_lon: -89.65 - i / 1000,
  customer_tank_id: `QA-TANK-${i}`,
  product_code: PRODUCTS[i % PRODUCTS.length],
  gallons_requested: 1500 + i * 125,
  fill_to_full: i % 5 === 0,
  call_type: i % 3 === 0 ? "will_call" : "keep_full",
  delivery_window_start: iso(8 + (i % 8)),
  delivery_window_end: iso(10 + (i % 8)),
  hold_reason: ORDER_STATUSES[i % 6] === "on_hold" ? "Credit check" : null,
  po_number: null,
  special_instructions: null,
  intake_channel: "web",
  intake_channel_id: "QA-CH-1",
  status: ORDER_STATUSES[i % ORDER_STATUSES.length],
  assigned_driver_id: null,
  assigned_asset_id: null,
  assigned_run_id: null,
  source_schema_version: "1",
  trace_id: `trace-${i}`,
  created_at: iso(6, i),
  updated_at: iso(7, i),
  last_event_timestamp: iso(7, i),
}));

const JOB_STATUSES = [
  "scheduled",
  "assigned",
  "in_progress",
  "completed",
  "failed",
];
export const JOBS = Array.from({ length: 22 }, (_, i) => ({
  job_id: `QA-JOB-${String(500 + i)}`,
  job_type: "fuel_delivery",
  status: JOB_STATUSES[i % JOB_STATUSES.length],
  tenant_id: TENANT,
  asset_assigned: `QA-TRK-${100 + (i % 7)}`,
  origin: "QA Terminal North",
  destination: `QA Station ${i}`,
  scheduled_time: iso(6 + (i % 10)),
  estimated_arrival: iso(8 + (i % 10)),
  created_at: iso(5, i),
  updated_at: iso(6, i),
  priority: i % 4 === 0 ? "high" : "normal",
  delayed: i % 6 === 0,
  delay_duration_minutes: i % 6 === 0 ? 25 + i : undefined,
}));

const INVOICE_STATUSES = [
  "draft",
  "finalized",
  "sent",
  "paid",
  "overdue",
  "void",
];
export const INVOICES = Array.from({ length: 21 }, (_, i) => ({
  invoice_id: `QA-INV-${String(2000 + i)}`,
  tenant_id: TENANT,
  customer_id: `QA-CUST-${i % 6}`,
  account_id: `QA-ACCT-${i % 6}`,
  order_id: `QA-ORD-${1000 + i}`,
  invoice_number: `QA-${String(2000 + i)}`,
  status: INVOICE_STATUSES[i % INVOICE_STATUSES.length],
  total_cents: 450000 + i * 1234,
  amount_paid_cents: i % 6 === 3 ? 450000 + i * 1234 : 0,
  remaining_cents: i % 6 === 3 ? 0 : 450000 + i * 1234,
  tax_cents: 30000,
  subtotal_cents: 420000 + i * 1234,
  line_items: [],
  issued_at: iso(9, i),
  due_date: "2026-11-08",
  finalized_at: iso(9, i),
  voided_at: null,
  void_reason: null,
  qbo_push_state: "not_pushed",
  qbo_push_attempts: 0,
  qbo_push_last_error: null,
  external_refs: {},
  created_at: iso(9, i),
  updated_at: iso(9, i),
}));

export const CUSTOMERS = Array.from({ length: 20 }, (_, i) => ({
  customer_id: `QA-CUST-${i}`,
  tenant_id: TENANT,
  display_name: `QA Customer ${i}`,
  legal_name: `QA Customer ${i} LLC`,
  status: "active",
  email: `qa-${i}@example.com`,
  phone: null,
  external_refs: {},
  metadata: {},
  created_at: iso(5, i),
  updated_at: iso(5, i),
}));

export const PROFILE = {
  user_id: "qa-e2e-user",
  email: "qa-dispatcher@example.com",
  tenant_id: TENANT,
  roles: ["admin", "dispatcher"],
  has_pii_access: true,
};
// ── Phase 2 fixtures (Dashboard, Live, Plans) ───────────────────────────────
const PLAN_STATUSES = [
  "draft",
  "proposed",
  "approved",
  "dispatched",
  "rejected",
];
export const PLANS = Array.from({ length: 20 }, (_, i) => ({
  plan_id: `QA-PLAN-${String(300 + i)}`,
  run_id: `QA-RUN-${300 + i}`,
  status: PLAN_STATUSES[i % PLAN_STATUSES.length],
  truck_id: `QA-TRK-${100 + (i % 7)}`,
  created_at: iso(5, i),
  total_utilization_pct: 60 + (i % 35),
  execution_status: null,
}));
export const ASSETS = Array.from({ length: 8 }, (_, i) => ({
  id: `QA-TRK-${100 + i}`,
  name: `QA-TRK-${100 + i}`,
  assetType: "vehicle",
  status: "active",
  currentLocation: {
    coordinates: { lat: 39.78 + i / 50, lon: -89.65 - i / 50 },
  },
}));
export const FUEL_ALERTS = Array.from({ length: 3 }, (_, i) => ({
  station_id: `QA-STN-${i}`,
  name: `QA Station tank ${i}`,
  fuel_type: ["GASOLINE_REG", "KEROSENE", "DIESEL_2"][i],
  status: ["critical", "low", "empty"][i],
  current_stock_gallons: 400 + i * 300,
  capacity_gallons: 6000,
  current_stock_liters: 1500 + i * 1100,
  capacity_liters: 22700,
  stock_percentage: [12, 23, 4][i],
  days_until_empty: [0.6, 1.3, 0.2][i],
  location_name: "Springfield",
}));
export const APPROVALS = Array.from({ length: 3 }, (_, i) => ({
  action_id: `QA-ACT-${i}`,
  action_type: "route_plan",
  tool_name: ["route_plan", "refill_proposal", "reassign_driver"][i],
  parameters: {},
  risk_level: ["low", "medium", "high"][i],
  proposed_by: ["route_agent", "fuel_agent", "dispatch_agent"][i],
  proposed_at: iso(13, i * 5),
  status: "pending",
  reviewed_by: null,
  reviewed_at: null,
  expiry_time: "2099-01-01T00:00:00Z",
  impact_summary: [
    "Route plan for 4 loads",
    "Refill proposal: QA Station tank 0",
    "Swap driver on QA-TRK-102",
  ][i],
  tenant_id: TENANT,
}));
