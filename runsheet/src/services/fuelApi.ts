import { API_TIMEOUTS, ApiError, ApiTimeoutError } from "./api";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Shared Types ────────────────────────────────────────────────────────────

export interface GeoPoint {
  lat: number;
  lon: number;
}

export interface PaginationMeta {
  page: number;
  size: number;
  total: number;
  total_pages: number;
}

export interface PaginatedResponse<T> {
  data: T[];
  pagination: PaginationMeta;
  request_id: string;
}

// ─── Fuel Station Types ──────────────────────────────────────────────────────

export type FuelType = "AGO" | "PMS" | "ATK" | "LPG";
export type StationStatus = "normal" | "low" | "critical" | "empty";

export interface FuelStation {
  station_id: string;
  name: string;
  fuel_type: FuelType;
  capacity_liters: number;
  current_stock_liters: number;
  daily_consumption_rate: number;
  days_until_empty: number;
  alert_threshold_pct: number;
  status: StationStatus;
  location?: GeoPoint | null;
  location_name?: string | null;
  tenant_id: string;
  last_updated: string;
}

export interface FuelStationDetail {
  station: FuelStation;
  recent_consumption_events: ConsumptionEvent[];
  recent_refill_events: RefillEvent[];
}

export interface ConsumptionEvent {
  station_id: string;
  fuel_type: FuelType;
  quantity_liters: number;
  asset_id: string;
  operator_id: string;
  odometer_reading?: number | null;
}

export interface RefillEvent {
  station_id: string;
  fuel_type: FuelType;
  quantity_liters: number;
  supplier: string;
  delivery_reference?: string | null;
  operator_id: string;
}

// ─── Alert Types ─────────────────────────────────────────────────────────────

export interface FuelAlert {
  station_id: string;
  name: string;
  fuel_type: FuelType;
  status: "low" | "critical" | "empty";
  current_stock_liters: number;
  capacity_liters: number;
  stock_percentage: number;
  days_until_empty: number;
  location_name?: string | null;
}

// ─── Metrics Types ───────────────────────────────────────────────────────────

export interface ConsumptionMetric {
  timestamp: string;
  total_liters: number;
  event_count: number;
  station_id?: string | null;
  fuel_type?: string | null;
}

export interface EfficiencyMetric {
  asset_id: string;
  total_liters: number;
  total_distance_km?: number | null;
  liters_per_km?: number | null;
  event_count: number;
}

export interface FuelNetworkSummary {
  total_stations: number;
  total_capacity_liters: number;
  total_current_stock_liters: number;
  total_daily_consumption: number;
  average_days_until_empty: number;
  stations_normal: number;
  stations_low: number;
  stations_critical: number;
  stations_empty: number;
  active_alerts: number;
}

// ─── Filter Types ────────────────────────────────────────────────────────────

export interface StationFilters {
  fuel_type?: FuelType;
  status?: StationStatus;
  location?: string;
  page?: number;
  size?: number;
}

export interface ConsumptionMetricsFilters {
  bucket?: "hourly" | "daily" | "weekly";
  station_id?: string;
  fuel_type?: FuelType;
  asset_id?: string;
  start_date?: string;
  end_date?: string;
}

export interface EfficiencyMetricsFilters {
  asset_id?: string;
  start_date?: string;
  end_date?: string;
}

// ─── HTTP Helper ─────────────────────────────────────────────────────────────

async function fetchWithTimeout(
  url: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUTS.STANDARD,
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    return response;
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiTimeoutError(
        `Request timed out after ${timeout / 1000} seconds`,
      );
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

function buildQueryString(
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null && v !== "",
  );
  if (entries.length === 0) return "";
  const searchParams = new URLSearchParams();
  for (const [key, value] of entries) {
    searchParams.set(key, String(value));
  }
  return `?${searchParams.toString()}`;
}

async function fuelRequest<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  try {
    const response = await fetchWithTimeout(url, {
      headers: {
        "Content-Type": "application/json",
        ...options?.headers,
      },
      ...options,
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(
        body.detail || body.message || `HTTP error! status: ${response.status}`,
        response.status,
      );
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiTimeoutError || error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
}

// ─── Station Endpoints ───────────────────────────────────────────────────────

/** GET /fuel/stations — list stations with filters */
export async function getStations(
  filters: StationFilters = {},
): Promise<PaginatedResponse<FuelStation>> {
  const qs = buildQueryString(filters);
  return fuelRequest<PaginatedResponse<FuelStation>>(`/fuel/stations${qs}`);
}

/** GET /fuel/stations/:id — station detail with recent events */
export async function getStation(
  stationId: string,
): Promise<{ data: FuelStationDetail; request_id: string }> {
  return fuelRequest<{ data: FuelStationDetail; request_id: string }>(
    `/fuel/stations/${encodeURIComponent(stationId)}`,
  );
}

// ─── Alert Endpoints ─────────────────────────────────────────────────────────

/** GET /fuel/alerts — active alerts across all stations */
export async function getAlerts(): Promise<{
  data: FuelAlert[];
  request_id: string;
}> {
  return fuelRequest<{ data: FuelAlert[]; request_id: string }>("/fuel/alerts");
}

// ─── Metrics Endpoints ───────────────────────────────────────────────────────

/** GET /fuel/metrics/consumption — consumption aggregated by time bucket */
export async function getConsumptionMetrics(
  filters: ConsumptionMetricsFilters = {},
): Promise<{ data: ConsumptionMetric[]; request_id: string }> {
  const qs = buildQueryString(filters);
  return fuelRequest<{ data: ConsumptionMetric[]; request_id: string }>(
    `/fuel/metrics/consumption${qs}`,
  );
}

/** GET /fuel/metrics/efficiency — fuel efficiency per asset */
export async function getEfficiencyMetrics(
  filters: EfficiencyMetricsFilters = {},
): Promise<{ data: EfficiencyMetric[]; request_id: string }> {
  const qs = buildQueryString(filters);
  return fuelRequest<{ data: EfficiencyMetric[]; request_id: string }>(
    `/fuel/metrics/efficiency${qs}`,
  );
}

/** GET /fuel/metrics/summary — network-wide fuel summary */
export async function getNetworkSummary(): Promise<{
  data: FuelNetworkSummary;
  request_id: string;
}> {
  return fuelRequest<{ data: FuelNetworkSummary; request_id: string }>(
    "/fuel/metrics/summary",
  );
}
