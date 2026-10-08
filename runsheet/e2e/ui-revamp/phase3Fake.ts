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
  if (path === "/compliance/asset-certifications/dashboard")
    return {
      data: {
        tenant_id: TENANT,
        total_expired: 1,
        total_expiring_soon: 2,
        assets: [],
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
