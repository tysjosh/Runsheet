/**
 * Fixtures for the Phase 3 pages (Fleet, Fuel, Customers, Billing,
 * Compliance, Analytics, Settings), kept apart from `fixtures.ts` so the
 * Phase 2 and Phase 3 streams don't edit the same block. `phase3Response`
 * returns the JSON body for a GET it knows, or `undefined` to fall through
 * to the generic empty list. Values are synthetic (QA- prefixes) and
 * deterministic, with ≥ 20 rows per list so every page has a first row.
 */
import { TENANT } from "./fixtures";

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
const STATION_STATUSES = ["normal", "low", "critical", "normal", "empty"];

export const STATIONS = Array.from({ length: 22 }, (_, i) => ({
  station_id: `QA-FS-${String(100 + i)}`,
  name: `QA Station ${100 + i}`,
  fuel_type: PRODUCTS[i % PRODUCTS.length],
  // Litre-based capacities on purpose: they convert to fractional gallons.
  capacity_liters: 20_000 + i * 1_000,
  current_stock_liters: (20_000 + i * 1_000) * ((i % 9) + 1) * 0.1,
  daily_consumption_rate: 400 + i * 10,
  days_until_empty: 2.5 + i,
  alert_threshold_pct: 20,
  status: STATION_STATUSES[i % STATION_STATUSES.length],
  location_name: `QA District ${i % 4}`,
  tenant_id: TENANT,
  last_updated: iso(7, i),
}));

const STATION_SUMMARY = {
  total_stations: STATIONS.length,
  total_capacity_liters: STATIONS.reduce((s, x) => s + x.capacity_liters, 0),
  total_current_stock_liters: STATIONS.reduce(
    (s, x) => s + x.current_stock_liters,
    0,
  ),
  total_daily_consumption: 9_000,
  average_days_until_empty: 6.4,
  stations_normal: STATIONS.filter((s) => s.status === "normal").length,
  stations_low: STATIONS.filter((s) => s.status === "low").length,
  stations_critical: STATIONS.filter((s) => s.status === "critical").length,
  stations_empty: STATIONS.filter((s) => s.status === "empty").length,
  active_alerts: 3,
};

function paginated<T>(rows: T[]) {
  return {
    data: rows,
    items: rows,
    entries: rows,
    total: rows.length,
    page: 1,
    size: Math.max(20, rows.length),
    has_more: false,
    cursor: null,
    pagination: { page: 1, size: 50, total: rows.length, total_pages: 1 },
    request_id: "e2e",
  };
}

const CUSTOMER_ROWS = Array.from({ length: 22 }, (_, i) => ({
  customer_id: `QA-CUST-${String(200 + i)}`,
  tenant_id: TENANT,
  display_name: `QA Customer ${200 + i}`,
  legal_name: `QA Customer ${200 + i} LLC`,
  primary_email: `qa-${200 + i}@example.com`,
  tax_id: null,
  status: i % 6 === 5 ? "archived" : "active",
  account_count: (i % 3) + 1,
  open_balance_cents: 125_000 + i * 3_517,
  created_at: iso(5, i),
  updated_at: iso(6, i),
  external_refs: {},
  metadata: {},
}));
const DELIVERY = ["delivered", "sent", "failed", "pending", "delivered"];
const NOTIFICATIONS = Array.from({ length: 22 }, (_, i) => ({
  notification_id: `QA-NOTIF-${String(300 + i)}`,
  notification_type: ["delay_alert", "eta_change", "delivery_confirmation"][
    i % 3
  ],
  channel: ["sms", "email", "whatsapp"][i % 3],
  recipient_name: `QA Customer ${200 + (i % 10)}`,
  recipient_reference: `+1555010${String(i).padStart(2, "0")}`,
  subject: `QA delivery update ${i}`,
  message_body: "QA fixture message.",
  delivery_status: DELIVERY[i % DELIVERY.length],
  failure_reason:
    DELIVERY[i % DELIVERY.length] === "failed" ? "QA bounce" : null,
  related_entity_id: `QA-ORD-${i}`,
  related_entity_type: "order",
  retry_count: 0,
  tenant_id: TENANT,
  created_at: iso(8, i),
  updated_at: iso(8, i),
  sent_at: null,
  delivered_at: null,
  failed_at: null,
}));

const loc = (name: string, i: number) => ({
  id: `QA-LOC-${i}`,
  name,
  type: "depot",
  coordinates: { lat: 29.7 + i * 0.01, lon: -95.3 - i * 0.01 },
  address: name,
});
const TRUCK_STATUSES = ["on_time", "delayed", "stopped", "loading", "on_time"];
export const TRUCKS = Array.from({ length: 22 }, (_, i) => ({
  id: `QA-TRK-${String(100 + i)}`,
  assetType: "vehicle",
  assetSubtype: "fuel_truck",
  name: `QA Tanker ${100 + i}`,
  plateNumber: `QA-${String(100 + i)}`,
  status: TRUCK_STATUSES[i % TRUCK_STATUSES.length],
  currentLocation: loc(`QA Depot ${i % 3}`, i),
  destination: loc(`QA Site ${i}`, i + 50),
  route: {
    id: `QA-RT-${i}`,
    origin: loc(`QA Depot ${i % 3}`, i),
    destination: loc(`QA Site ${i}`, i + 50),
    waypoints: [],
    distance: 20 + i,
    estimatedDuration: 60,
  },
  estimatedArrival: iso(10 + (i % 6), i),
  lastUpdate: iso(8, i),
}));
const INVENTORY = Array.from({ length: 22 }, (_, i) => ({
  item_id: `QA-INV-${String(400 + i)}`,
  name: `QA Part ${400 + i}`,
  category: ["tires", "filters", "fluids", "fuel_equipment"][i % 4],
  location: `QA Depot ${i % 3}`,
  quantity: 1_000 + i * 37,
  unit: "pieces",
  status: ["in_stock", "low_stock", "in_stock", "out_of_stock"][i % 4],
  min_threshold: 50,
  max_capacity: 5_000,
  updated_at: iso(7, i),
}));
const UTILIZATION = Array.from({ length: 22 }, (_, i) => ({
  driver_id: `QA-DRV-${String(500 + i)}`,
  driver_name: `QA Driver ${500 + i}`,
  status: ["active", "on_break", "off_duty", "active"][i % 4],
  active_order_count: i % 9,
  completed_today: i % 5,
  last_seen: iso(9, i),
  medical_card_expiry: "2027-06-30",
  assigned_truck_id: `QA-TRK-${String(100 + i)}`,
}));
const DRIVERS = UTILIZATION.map((u, i) => ({
  driver_id: u.driver_id,
  tenant_id: TENANT,
  full_name: u.driver_name,
  cdl_number: `QA${String(9000 + i)}`,
  cdl_state: "TX",
  cdl_class: "A",
  cdl_expiry_date: "2027-01-31",
  medical_card_expiry_date: "2026-11-15",
  hazmat_endorsement_expiry_date: null,
  tanker_endorsement_expiry_date: null,
  last_drug_test_date: null,
  last_mvr_date: null,
  status: ["active", "suspended", "active", "expired"][i % 4],
  created_at: iso(5, i),
  updated_at: iso(5, i),
}));

// ─── Billing and Compliance (tasks 3.4, 3.5) ────────────────────────────────

const N = 22;
const range = <T>(f: (i: number) => T) =>
  Array.from({ length: N }, (_, i) => f(i));

export const ACCOUNTS = range((i) => ({
  account_id: `QA-ACC-${300 + i}`,
  tenant_id: TENANT,
  customer_id: `QA-CUST-${i % 6}`,
  display_name: `QA Account ${300 + i}`,
  status: ["active", "active", "suspended", "closed"][i % 4],
  credit_limit_cents: 5_000_000 + i * 10_000,
  open_balance_cents: 125_000 + i * 3_333,
  available_credit_cents: 4_000_000,
  credit_balance_cents: 0,
  credit_state: ["ok", "hold", "override"][i % 3],
  credit_override_expires_at: null,
  net_terms_days: [15, 30, 45][i % 3],
  tier: ["default", "gold", "silver", "platinum", "bronze"][i % 5],
  billing_address: null,
  payment_method_preference: "invoice",
  created_at: iso(1),
  updated_at: iso(2),
  external_refs: {},
}));

export const PAYMENTS = range((i) => ({
  payment_id: `QA-PAY-${400 + i}`,
  tenant_id: TENANT,
  invoice_id: `QA-INV-${2000 + i}`,
  account_id: `QA-ACC-${300 + i}`,
  amount_cents: 45_050 + i * 1_234,
  source: ["stripe", "manual", "qbo"][i % 3],
  method: ["card", "ach", "check", "wire"][i % 4],
  external_id: null,
  reference: i % 2 ? `QA-REF-${i}` : null,
  status: i % 7 === 6 ? "reversed" : "applied",
  received_at: iso(3, i),
  applied_at: iso(3, i),
  reversed_at: null,
}));

export const PRICE_BOOKS = range((i) => ({
  price_book_id: `QA-PB-${500 + i}`,
  tenant_id: TENANT,
  name: `QA Price book ${500 + i}`,
  description: i % 2 ? "Commercial" : null,
  status: ["active", "draft", "archived"][i % 3],
  rule_count: i % 5,
  created_at: iso(1),
  updated_at: iso(2, i),
}));

export const PRICING_RULES = range((i) => ({
  rule_id: `QA-PR-${600 + i}`,
  tenant_id: TENANT,
  customer_id: i % 3 ? `QA-CUST-${i % 6}` : null,
  product_code: PRODUCTS[i % PRODUCTS.length],
  strategy: ["posted_price", "rack_plus_margin", "tiered_volume", "cost_plus"][
    i % 4
  ],
  priority: 10 + i,
  posted_price_cents: 350 + i,
  margin_cents: 15,
  freight_rate_cents_per_mile: 5,
  tier_thresholds: [{ min_gallons: 0, max_gallons: null, price_cents: 340 }],
  effective_date: "2026-01-01",
  expiry_date: null,
}));

export const CONTRACTS = range((i) => ({
  contract_id: `QA-PPC-${700 + i}`,
  tenant_id: TENANT,
  customer_id: `QA-CUST-${i % 6}`,
  account_id: `QA-ACC-${300 + i}`,
  product_code: PRODUCTS[i % PRODUCTS.length],
  contract_type: ["fixed_price", "cap_price", "collar"][i % 3],
  start_date: "2026-01-01",
  end_date: "2026-12-31",
  contracted_gallons: 10_000,
  remaining_gallons: 4_000 + i * 100,
  price_cap_cents: 400,
  price_floor_cents: 300,
  fixed_price_cents: 350,
  status: ["active", "exhausted", "expired"][i % 3],
}));

export const RECONCILIATION = range((i) => ({
  reconciliation_id: `QA-REC-${800 + i}`,
  tenant_id: TENANT,
  order_id: `QA-ORD-${100 + i}`,
  plan_id: `QA-PLAN-${i % 4}`,
  pod_id: `QA-POD-${900 + i}`,
  invoice_id: `QA-INV-${2000 + i}`,
  customer_id: `QA-CUST-${i % 6}`,
  assigned_asset_id: `QA-TRK-${100 + (i % 8)}`,
  assigned_driver_id: `QA-DRV-${i % 8}`,
  ordered_gallons: 5_000 + i * 10,
  loaded_gallons: 4_990 + i * 10,
  delivered_gallons: 4_980 + i * 10,
  invoiced_gallons: 4_980 + i * 10,
  variance_load_vs_order_pct: i % 5 === 0 ? 4.2 : 0.2,
  variance_delivered_vs_loaded_pct: 0.2,
  variance_invoiced_vs_delivered_pct: 0,
  alert_flags: i % 5 === 0 ? ["variance_exceeds_threshold"] : [],
  generated_at: iso(6, i),
}));

const CERT_TYPES = ["V_test", "K_test", "I_test", "P_test", "meter_seal"];
export const CERT_ENTRIES = range((i) => ({
  cert_id: `QA-CERT-${i}`,
  asset_id: `QA-TRK-${100 + i}`,
  certification_type: CERT_TYPES[i % CERT_TYPES.length],
  status: i === 0 ? "expired" : i < 3 ? "expiring_soon" : "valid",
  expiry_date: "2027-01-31",
  days_until_expiry: i === 0 ? -3 : i < 3 ? 12 : 120 + i,
}));

export const METERS = range((i) => ({
  meter_id: `QA-MTR-${i}`,
  tenant_id: TENANT,
  meter_number: `QA-MTR-${100 + i}`,
  truck_id: `QA-TRK-${100 + i}`,
  calibration_certificate_number: `QA-CAL-${i}`,
  calibration_date: "2026-01-15",
  calibration_expiry_date: i % 6 === 0 ? "2026-10-20" : "2027-01-15",
  weights_measures_authority: "QA Weights & Measures",
}));

export const TERMINAL_BOLS = range((i) => ({
  bol_id: `QA-BOL-${i}`,
  tenant_id: TENANT,
  load_number: `QA-LD-${1000 + i}`,
  product_code: PRODUCTS[i % PRODUCTS.length],
  gross_gallons: 8_000 + i * 12.345,
  net_gallons: 7_950 + i * 12.1,
  supplier_name: "QA Supplier",
  terminal_name: `QA Terminal ${i % 3}`,
  driver_id: `QA-DRV-${i % 8}`,
  timestamp: iso(5, i),
  status: ["ingested", "pending_confirmation", "linked"][i % 3],
}));

export const IFTA_TRUCKS = range((i) => ({
  truck_id: `QA-TRK-${100 + i}`,
  truck_name: `QA Truck ${100 + i}`,
  total_miles: 4_200 + i * 37.5,
  total_gallons: 700 + i * 6.2,
  fleet_mpg: 6.1,
  jurisdictions: [
    {
      jurisdiction: "TX",
      total_miles: 3_000,
      taxable_miles: 3_000,
      tax_paid_gallons: 500,
      net_taxable_gallons: 20,
      tax_rate: 20,
      tax_due: 400,
    },
  ],
}));

export const TAX_RATES = range((i) => ({
  jurisdiction_id: `QA-TAX-${i}`,
  tenant_id: TENANT,
  fips_code: i % 2 ? "48" : "48201",
  jurisdiction_level: ["federal", "state", "county", "city"][i % 4],
  tax_type: ["excise", "ust", "spcc", "environmental"][i % 4],
  product_codes: [PRODUCTS[i % PRODUCTS.length]],
  rate_cents_per_gallon: 184 + i,
  effective_date: "2026-01-01",
  expiry_date: null,
}));

export const EXEMPTIONS = range((i) => ({
  exemption_id: `QA-EX-${i}`,
  tenant_id: TENANT,
  customer_id: `QA-CUST-${i % 6}`,
  exemption_type: ["dyed_diesel", "farm_agricultural", "government"][i % 3],
  certificate_number: `QA-637M-${i}`,
  expiry_date: i % 4 ? "2027-06-30" : "2026-10-20",
}));

export const DEPOTS = range((i) => ({
  depot_id: `QA-DEP-${i}`,
  tenant_id: TENANT,
  name: `QA Depot ${i}`,
  location_lat: 41.8 + i * 0.01,
  location_lon: -87.6 - i * 0.01,
  address: `${100 + i} QA Yard Rd`,
  timezone: "America/Chicago",
  fuel_types_supported: [PRODUCTS[i % PRODUCTS.length], "DIESEL_2"],
  status: i % 5 === 4 ? "inactive" : "active",
  is_default: i === 0,
}));

export const MARGIN_RECORDS = range((i) => ({
  record_id: `QA-MR-${i}`,
  stage: "invoice",
  source_key: `QA-INV-${i}:0`,
  order_id: `QA-ORD-${i}`,
  invoice_id: `QA-INV-${i}`,
  line_index: 0,
  customer_id: `QA-CUST-${i}`,
  account_id: `QA-ACC-${i}`,
  product_code: PRODUCTS[i % PRODUCTS.length],
  terminal_id: "QA-TERM-1",
  gallons_ugal: (4_000 + i * 125) * 1_000_000,
  unit_price_micros: 3_120_000,
  revenue_cents: (4_000 + i * 125) * 312,
  method: i % 6 === 5 ? "none" : "wac",
  product_cost_micros: i % 6 === 5 ? null : 2_810_000,
  adders_micros: i % 6 === 5 ? null : 60_000,
  landed_cost_micros: i % 6 === 5 ? null : 2_870_000,
  cost_cents: i % 6 === 5 ? null : (4_000 + i * 125) * 287,
  margin_cents: i % 6 === 5 ? null : (4_000 + i * 125) * 25,
  margin_per_gallon_micros: i % 6 === 5 ? null : 250_000,
  margin_bp: i % 6 === 5 ? null : 801,
  margin_pct: i % 6 === 5 ? null : "8.01",
  no_cost_reason: i % 6 === 5 ? "no_lots_no_rack" : null,
  flags: i % 6 === 5 ? ["missing_cost"] : [],
  floor_micros_used: 0,
  cost_snapshot: {},
  as_of: iso(8),
  version: 1,
  status: "active",
  origin: "live",
  frozen_at: null,
  computed_at: iso(8),
}));

function sized<T>(rows: T[], url: URL) {
  const size = Number(url.searchParams.get("size") ?? rows.length);
  const page = Number(url.searchParams.get("page") ?? 1);
  return {
    ...paginated(rows.slice((page - 1) * size, page * size)),
    pagination: {
      page,
      size,
      total: rows.length,
      total_pages: Math.max(1, Math.ceil(rows.length / size)),
    },
  };
}

/** Body for a Phase 3 GET, or undefined when this fake doesn't own it. */
export function phase3Response(path: string, url: URL): unknown | undefined {
  if (path === "/fuel/stations") {
    const status = url.searchParams.get("status");
    return paginated(
      status ? STATIONS.filter((s) => s.status === status) : STATIONS,
    );
  }
  if (path === "/commerce/ar-aging")
    return {
      data: {
        tenant_id: TENANT,
        total_open_cents: 5_312_300,
        bucket_current_cents: 800_000,
        bucket_0_30_cents: 2_100_000,
        bucket_31_60_cents: 1_200_000,
        bucket_61_90_cents: 712_300,
        bucket_90_plus_cents: 500_000,
        by_account: ACCOUNTS.map((a, i) => ({
          account_id: a.account_id,
          display_name: a.display_name,
          bucket_current_cents: 30_000,
          bucket_0_30_cents: 100_000 + i * 1_000,
          bucket_31_60_cents: 50_000,
          bucket_61_90_cents: 20_000,
          bucket_90_plus_cents: i % 3 ? 0 : 15_000,
          total_open_cents: 200_000 + i * 1_000 + (i % 3 ? 0 : 15_000),
        })),
        as_of: iso(8),
      },
      request_id: "e2e",
    };
  if (path === "/commerce/ar-aging/history")
    return { data: [], request_id: "e2e" };
  // Analytics Overview (F2/F3/F13 shape): one daily snapshot plus as_of.
  if (path === "/analytics/metrics")
    return {
      data: {
        delivery_performance: { title: "Delivery Performance", value: "92.5%" },
        average_delay: { title: "Average Delay", value: "14.0 min" },
        fleet_utilization: { title: "Fleet Utilization", value: "78.0%" },
      },
      as_of: iso(6),
      success: true,
    };
  if (path === "/analytics/routes")
    return {
      data: [
        { name: "QA-Route North", performance: 96.0, orders_scored: 40 },
        { name: "QA-Route East", performance: 88.5, orders_scored: 25 },
      ],
      success: true,
    };
  if (path === "/analytics/timeseries")
    return {
      data: [
        { timestamp: "2026-10-06T00:00:00Z", value: 90.1 },
        { timestamp: "2026-10-07T00:00:00Z", value: 91.4 },
        { timestamp: "2026-10-08T00:00:00Z", value: 92.5 },
      ],
      success: true,
    };
  if (path === "/fleet/trucks")
    return { data: TRUCKS, success: true, timestamp: iso(8) };
  if (path === "/fleet/summary")
    return {
      data: {
        totalTrucks: TRUCKS.length,
        activeTrucks: 18,
        onTimeTrucks: 9,
        delayedTrucks: 4,
        averageDelay: 6,
        byType: { vehicle: TRUCKS.length },
        bySubtype: { fuel_truck: TRUCKS.length },
      },
      success: true,
      timestamp: iso(8),
    };
  if (/^\/fleet\/assets\/[^/]+\/compliance$/.test(path))
    return { asset_id: path.split("/")[3], overall_status: "valid" };
  if (path === "/inventory/items")
    return { data: INVENTORY, success: true, timestamp: iso(8) };
  if (path === "/inventory/summary")
    return {
      data: {
        total_items: INVENTORY.length,
        in_stock: INVENTORY.filter((x) => x.status === "in_stock").length,
        low_stock: INVENTORY.filter((x) => x.status === "low_stock").length,
        out_of_stock: INVENTORY.filter((x) => x.status === "out_of_stock")
          .length,
        total_value: 84_250.5,
      },
    };
  if (path === "/inventory/alerts")
    return { data: INVENTORY.filter((x) => x.status !== "in_stock") };
  if (path === "/ops/drivers/utilization") return { items: UTILIZATION };
  if (/^\/ops\/drivers\/[^/]+\/profile$/.test(path))
    return {
      driver_id: path.split("/")[3],
      qualification: {
        status: "resolved",
        summary: { overall_status: "valid" },
      },
    };
  if (path === "/compliance/drivers") return sized(DRIVERS, url);
  if (path === "/compliance/drivers/dashboard")
    return {
      data: {
        tenant_id: TENANT,
        total_drivers: DRIVERS.length,
        active_drivers: DRIVERS.filter((d) => d.status === "active").length,
        suspended_drivers: DRIVERS.filter((d) => d.status === "suspended")
          .length,
        expired_drivers: DRIVERS.filter((d) => d.status === "expired").length,
        expiring_drivers: 3,
        expiring_within_7_days: 1,
        expiring_within_30_days: 2,
        expiring_within_60_days: 3,
        drug_test_overdue: 0,
        drivers: DRIVERS.slice(0, 5).map((d) => ({
          driver_id: d.driver_id,
          full_name: d.full_name,
          status: d.status,
          qualifications: [
            {
              qualification_type: "medical_card",
              expiry_date: "2026-11-15",
              days_until_expiry: 38,
              alert_level: "warning",
              status: "expiring_soon",
            },
          ],
        })),
        generated_at: iso(8),
      },
    };
  if (path === "/fuel/mvp/depots")
    return {
      items: DEPOTS,
      total: DEPOTS.length,
      page: 1,
      size: 20,
      has_next: false,
    };
  if (/^\/ops\/admin\/feature-flags\/[^/]+\/order-intake-pipeline$/.test(path))
    return { data: { state: "shadow" }, request_id: "e2e" };
  // Billing and Compliance (tasks 3.4, 3.5)
  if (path === "/commerce/accounts") {
    const status = url.searchParams.get("status");
    return sized(
      status ? ACCOUNTS.filter((a) => a.status === status) : ACCOUNTS,
      url,
    );
  }
  if (path === "/commerce/payments") return paginated(PAYMENTS);
  if (path === "/commerce/price-books")
    return { data: PRICE_BOOKS, request_id: "e2e" };
  if (path === "/commerce/pricing-rules") return sized(PRICING_RULES, url);
  if (path === "/commerce/price-protection-contracts")
    return sized(CONTRACTS, url);
  if (path === "/fuel/mvp/reconciliation")
    return {
      items: RECONCILIATION,
      total: RECONCILIATION.length,
      page: 1,
      size: 25,
      has_next: false,
    };
  if (path === "/compliance/meters") return sized(METERS, url);
  if (path === "/compliance/terminal-bols") return sized(TERMINAL_BOLS, url);
  if (path === "/compliance/tax-jurisdictions") return sized(TAX_RATES, url);
  if (path === "/compliance/exemptions") return sized(EXEMPTIONS, url);
  if (path === "/compliance/ifta/report")
    return {
      data: {
        tenant_id: TENANT,
        quarter: url.searchParams.get("quarter") ?? "2026-Q4",
        trucks: IFTA_TRUCKS,
        fleet_mpg: 6.1,
        incomplete_trucks: [],
        generated_at: iso(8),
      },
      request_id: "e2e",
    };
  if (path === "/compliance/asset-certifications/dashboard")
    return {
      data: {
        tenant_id: TENANT,
        total_valid: CERT_ENTRIES.length - 3,
        total_expired: 1,
        total_expiring_soon: 2,
        assets: CERT_ENTRIES,
      },
    };
  if (path === "/commerce/customers") {
    const status = url.searchParams.get("status");
    return sized(
      status ? CUSTOMER_ROWS.filter((c) => c.status === status) : CUSTOMER_ROWS,
      url,
    );
  }
  if (path === "/notifications") {
    const status = url.searchParams.get("delivery_status");
    return sized(
      status
        ? NOTIFICATIONS.filter((n) => n.delivery_status === status)
        : NOTIFICATIONS,
      url,
    );
  }
  if (path === "/notifications/summary") {
    const by_status: Record<string, number> = {};
    for (const n of NOTIFICATIONS)
      by_status[n.delivery_status] = (by_status[n.delivery_status] ?? 0) + 1;
    return {
      total: NOTIFICATIONS.length,
      by_status,
      by_type: {},
      by_channel: {},
    };
  }
  if (path === "/fuel/metrics/summary")
    return { data: STATION_SUMMARY, request_id: "e2e" };
  if (path === "/fuel/metrics/consumption")
    return { data: [], request_id: "e2e" };
  // Billing → Margin (task 3.4): the feed is on for the e2e admin.
  // marginApi reads the `data` envelope.
  if (path === "/commerce/margin/settings")
    return {
      data: {
        wac_window_days: 30,
        rack_staleness_days: 3,
        floor_micros: 0,
        product_floors: {},
        timezone: "America/Chicago",
        feed_activated_at: iso(8),
        updated_by: null,
        updated_at: null,
        persisted: true,
      },
    };
  if (path === "/commerce/margin/records")
    return {
      data: {
        items: MARGIN_RECORDS,
        next_cursor: null,
        total: MARGIN_RECORDS.length,
        timezone: "America/Chicago",
      },
    };
  if (path === "/commerce/margin/alerts")
    return { data: { items: [], next_cursor: null, total: 0 } };
  if (path === "/ops/monitoring/poison-queue")
    return {
      data: {
        queue_depth: 7,
        oldest_event_age_seconds: 420,
        pending_count: 5,
        permanently_failed_count: 2,
      },
      request_id: "e2e",
    };
  return undefined;
}
