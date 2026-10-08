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
