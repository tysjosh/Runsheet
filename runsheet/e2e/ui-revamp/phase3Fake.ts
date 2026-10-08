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

/** Body for a Phase 3 GET, or undefined when this fake doesn't own it. */
export function phase3Response(path: string, url: URL): unknown | undefined {
  if (path === "/fuel/stations") {
    const status = url.searchParams.get("status");
    return paginated(
      status ? STATIONS.filter((s) => s.status === status) : STATIONS,
    );
  }
  if (path === "/fuel/metrics/summary")
    return { data: STATION_SUMMARY, request_id: "e2e" };
  if (path === "/fuel/metrics/consumption")
    return { data: [], request_id: "e2e" };
  return undefined;
}
