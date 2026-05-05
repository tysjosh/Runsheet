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

// ─── Efficiency Types ─────────────────────────────────────────────────────────

export interface EfficiencyMetric {
  asset_id: string;
  distance_km: number;
  fuel_consumed_liters: number;
  efficiency_km_per_liter: number;
  period_start?: string;
  period_end?: string;
}

export interface EfficiencyFilters {
  asset_id?: string;
  start_date?: string;
  end_date?: string;
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
  params: Record<string, string | number | boolean | undefined | null> | object,
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

/** GET /fuel/metrics/summary — network-wide fuel summary */
export async function getNetworkSummary(): Promise<{
  data: FuelNetworkSummary;
  request_id: string;
}> {
  return fuelRequest<{ data: FuelNetworkSummary; request_id: string }>(
    "/fuel/metrics/summary",
  );
}

/** GET /fuel/metrics/efficiency — per-asset fuel efficiency */
export async function getEfficiencyMetrics(
  filters: EfficiencyFilters = {},
): Promise<{ data: EfficiencyMetric[]; request_id: string }> {
  const qs = buildQueryString(filters);
  return fuelRequest<{ data: EfficiencyMetric[]; request_id: string }>(
    `/fuel/metrics/efficiency${qs}`,
  );
}

// ─── Station CRUD Types ──────────────────────────────────────────────────────

export interface CreateStationPayload {
  station_id: string;
  name: string;
  fuel_type: FuelType;
  capacity_liters: number;
  initial_stock_liters: number;
  location?: GeoPoint;
  location_name?: string;
  alert_threshold_pct: number;
}

export interface UpdateStationPayload {
  name?: string;
  fuel_type?: FuelType;
  capacity_liters?: number;
  location?: GeoPoint;
  location_name?: string;
  alert_threshold_pct?: number;
}

// ─── Plan Execution Lifecycle Types ──────────────────────────────────────────

export interface PlanListItem {
  plan_id: string;
  status: string;
  truck_id: string;
  created_at: string;
  total_utilization_pct: number;
  estimated_cost?: number;
  actual_cost?: number;
  cost_variance_pct?: number;
}

export interface CheckinRequest {
  route_id: string;
  station_id: string;
  sequence: number;
  actual_quantities: Record<string, number>;
}

export interface StopVariance {
  station_id: string;
  sequence: number;
  quantity_variance_pct: number;
  time_variance_minutes: number;
  status: string;
}

export interface PlanOutcome {
  outcome_id: string;
  plan_id: string;
  stop_variances: StopVariance[];
  aggregate_quantity_variance_pct: number;
  aggregate_time_variance_minutes: number;
  missed_stops_count: number;
}

export interface CostBreakdown {
  fuel_cost: number;
  driver_cost: number;
  total_estimated_cost?: number;
  total_actual_cost?: number;
  distance_km: number;
  driver_hours: number;
  currency: string;
}

export interface CostConfig {
  fuel_consumption_rate: number;
  fuel_price_per_liter: number;
  driver_hourly_rate: number;
  currency: string;
}

// ─── Fuel Distribution MVP Types ─────────────────────────────────────────────

export interface GeneratePlanResponse {
  run_id: string;
  status: string;
}

export interface ReplanRequest {
  disruption_type: string;
  description: string;
  entity_id: string;
}

export interface ReplanResponse {
  plan_id: string;
  status: string;
  disruption_type: string;
}

export interface ForecastFilters {
  tenant_id: string;
  station_id?: string;
  fuel_grade?: string;
  page?: number;
  size?: number;
}

export interface PaginationFilters {
  tenant_id: string;
  page?: number;
  size?: number;
}

export interface CompartmentAssignment {
  compartment_id: string;
  station_id: string;
  fuel_grade: string;
  quantity_liters: number;
  compartment_capacity_liters: number;
}

export interface LoadingPlan {
  plan_id: string;
  truck_id: string;
  assignments: CompartmentAssignment[];
  total_utilization_pct: number;
  unserved_demand_liters: number;
  total_weight_kg: number;
  tenant_id: string;
  run_id: string;
  created_at: string;
  status: string;
}

export interface RouteStop {
  station_id: string;
  eta: string;
  drop: Record<string, number>;
  sequence: number;
}

export interface RouteAssignment {
  route_id: string;
  truck_id: string;
  plan_id: string;
  stops: RouteStop[];
  distance_km: number;
  eta_confidence: number;
  objective_value: number;
  tenant_id: string;
  run_id: string;
  timestamp: string;
  status: string;
}

export interface RoutePlan {
  plan_id: string;
  tenant_id: string;
  routes: RouteAssignment[];
  timestamp: string;
}

export interface PlanDetail {
  plan_id: string;
  loading_plan: LoadingPlan | null;
  route_plan: RoutePlan | null;
}

export interface Forecast {
  station_id: string;
  fuel_grade: string;
  current_stock_liters: number;
  predicted_stock_liters: number;
  days_until_empty: number;
  timestamp: string;
}

export interface DeliveryPriority {
  station_id: string;
  station_name: string;
  fuel_grade: string;
  priority_score: number;
  urgency: "low" | "medium" | "high" | "critical";
  timestamp: string;
}

// ─── Fuel Distribution MVP Endpoints ─────────────────────────────────────────

/** POST /api/fuel/mvp/plan/generate — trigger a full pipeline run */
export async function generatePlan(
  tenantId: string,
): Promise<GeneratePlanResponse> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<GeneratePlanResponse>(`/fuel/mvp/plan/generate${qs}`, {
    method: "POST",
  });
}

/** GET /api/fuel/mvp/plan/:id — retrieve a complete plan (loading + route) */
export async function getPlan(
  planId: string,
  tenantId: string,
): Promise<PlanDetail> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<PlanDetail>(
    `/fuel/mvp/plan/${encodeURIComponent(planId)}${qs}`,
  );
}

/** POST /api/fuel/mvp/plan/:id/replan — trigger exception replanning */
export async function replan(
  planId: string,
  body: ReplanRequest,
  tenantId: string,
): Promise<ReplanResponse> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<ReplanResponse>(
    `/fuel/mvp/plan/${encodeURIComponent(planId)}/replan${qs}`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/** GET /api/fuel/mvp/forecasts — paginated tank forecasts with optional filters */
export async function getForecasts(
  filters: ForecastFilters,
): Promise<PaginatedResponse<Forecast>> {
  const qs = buildQueryString(filters);
  return fuelRequest<PaginatedResponse<Forecast>>(
    `/fuel/mvp/forecasts${qs}`,
  );
}

/** GET /api/fuel/mvp/priorities — paginated delivery priority rankings */
export async function getPriorities(
  filters: PaginationFilters,
): Promise<PaginatedResponse<DeliveryPriority>> {
  const qs = buildQueryString(filters);
  return fuelRequest<PaginatedResponse<DeliveryPriority>>(
    `/fuel/mvp/priorities${qs}`,
  );
}

// ─── Station CRUD Endpoints ──────────────────────────────────────────────────

/** POST /fuel/stations — create a new fuel station */
export async function createStation(
  data: CreateStationPayload,
  tenantId: string,
): Promise<FuelStation> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<FuelStation>(`/fuel/stations${qs}`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** PATCH /fuel/stations/:id — update an existing fuel station */
export async function updateStation(
  stationId: string,
  data: UpdateStationPayload,
  tenantId: string,
): Promise<FuelStation> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<FuelStation>(
    `/fuel/stations/${encodeURIComponent(stationId)}${qs}`,
    {
      method: "PATCH",
      body: JSON.stringify(data),
    },
  );
}

/** PATCH /fuel/stations/:id/threshold — update a station's alert threshold */
export async function updateStationThreshold(
  stationId: string,
  threshold: number,
  tenantId: string,
): Promise<FuelStation> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<FuelStation>(
    `/fuel/stations/${encodeURIComponent(stationId)}/threshold${qs}`,
    {
      method: "PATCH",
      body: JSON.stringify({ alert_threshold_pct: threshold }),
    },
  );
}

// ─── Fuel Event Recording Endpoints ──────────────────────────────────────────

/** POST /fuel/consumption — record a fuel dispensing event */
export async function recordConsumption(
  data: ConsumptionEvent,
): Promise<{ data: Record<string, unknown>; request_id: string }> {
  return fuelRequest<{ data: Record<string, unknown>; request_id: string }>(
    "/fuel/consumption",
    {
      method: "POST",
      body: JSON.stringify(data),
    },
  );
}

/** POST /fuel/refill — record a fuel delivery/refill event */
export async function recordRefill(
  data: RefillEvent,
): Promise<{ data: Record<string, unknown>; request_id: string }> {
  return fuelRequest<{ data: Record<string, unknown>; request_id: string }>(
    "/fuel/refill",
    {
      method: "POST",
      body: JSON.stringify(data),
    },
  );
}

// ─── Plan Execution Lifecycle Endpoints ──────────────────────────────────────

/** GET /api/fuel/mvp/plans — list plans with optional filters and pagination */
export async function listPlans(
  tenantId: string,
  page?: number,
  size?: number,
  status?: string,
): Promise<PaginatedResponse<PlanListItem>> {
  const qs = buildQueryString({ tenant_id: tenantId, page, size, status });
  return fuelRequest<PaginatedResponse<PlanListItem>>(
    `/fuel/mvp/plans${qs}`,
  );
}

/** POST /api/fuel/mvp/plan/:id/approve — approve a draft plan */
export async function approvePlan(
  planId: string,
  tenantId: string,
  dispatcherId: string,
): Promise<{ plan_id: string; status: string }> {
  const qs = buildQueryString({ tenant_id: tenantId, dispatcher_id: dispatcherId });
  return fuelRequest<{ plan_id: string; status: string }>(
    `/fuel/mvp/plan/${encodeURIComponent(planId)}/approve${qs}`,
    { method: "POST" },
  );
}

/** POST /api/fuel/mvp/plan/:id/reject — reject a draft plan */
export async function rejectPlan(
  planId: string,
  tenantId: string,
  dispatcherId: string,
  reason?: string,
): Promise<{ plan_id: string; status: string }> {
  const qs = buildQueryString({ tenant_id: tenantId, dispatcher_id: dispatcherId });
  return fuelRequest<{ plan_id: string; status: string }>(
    `/fuel/mvp/plan/${encodeURIComponent(planId)}/reject${qs}`,
    {
      method: "POST",
      body: reason ? JSON.stringify({ reason }) : undefined,
    },
  );
}

/** POST /api/fuel/mvp/plan/:id/checkin — record a driver check-in at a stop */
export async function checkinStop(
  planId: string,
  tenantId: string,
  body: CheckinRequest,
): Promise<{ plan_id: string; completed_stops: number; total_stops: number; status: string }> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<{ plan_id: string; completed_stops: number; total_stops: number; status: string }>(
    `/fuel/mvp/plan/${encodeURIComponent(planId)}/checkin${qs}`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/** GET /api/fuel/mvp/plan/:id/outcomes — get plan vs actual outcome comparison */
export async function getPlanOutcomes(
  planId: string,
  tenantId: string,
): Promise<{ data: PlanOutcome; request_id: string }> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<{ data: PlanOutcome; request_id: string }>(
    `/fuel/mvp/plan/${encodeURIComponent(planId)}/outcomes${qs}`,
  );
}

/** GET /api/fuel/mvp/plan/:id/costs — get cost breakdown for a plan */
export async function getPlanCosts(
  planId: string,
  tenantId: string,
): Promise<{ data: { estimated: CostBreakdown; actual?: CostBreakdown; cost_variance_pct?: number }; request_id: string }> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<{ data: { estimated: CostBreakdown; actual?: CostBreakdown; cost_variance_pct?: number }; request_id: string }>(
    `/fuel/mvp/plan/${encodeURIComponent(planId)}/costs${qs}`,
  );
}

/** PUT /api/fuel/mvp/cost-config — upsert tenant cost configuration */
export async function updateCostConfig(
  tenantId: string,
  config: CostConfig,
): Promise<{ data: CostConfig; request_id: string }> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<{ data: CostConfig; request_id: string }>(
    `/fuel/mvp/cost-config${qs}`,
    {
      method: "PUT",
      body: JSON.stringify(config),
    },
  );
}
