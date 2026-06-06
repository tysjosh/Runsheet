import {
  API_TIMEOUTS,
  ApiError,
  ApiTimeoutError,
  fetchWithSession,
} from "./api";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

export const LITERS_PER_GALLON = 3.785411784;

export function litersToGallons(liters: number | null | undefined): number {
  if (liters == null || Number.isNaN(liters)) return 0;
  return liters / LITERS_PER_GALLON;
}

export function gallonsToLiters(gallons: number | null | undefined): number {
  if (gallons == null || Number.isNaN(gallons)) return 0;
  return gallons * LITERS_PER_GALLON;
}

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

export type FuelType =
  | "DIESEL_2"
  | "GASOLINE_REG"
  | "GASOLINE_PREM"
  | "HEATING_OIL"
  | "PROPANE"
  | "KEROSENE"
  | "OFF_ROAD_DIESEL"
  | "DEF";
export type StationStatus = "normal" | "low" | "critical" | "empty";

export interface FuelStation {
  station_id: string;
  name: string;
  fuel_type: FuelType;
  capacity_gallons?: number;
  current_stock_gallons?: number;
  daily_consumption_rate_gallons?: number;
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
  quantity_gallons?: number;
  quantity_liters: number;
  asset_id: string;
  operator_id: string;
  odometer_reading?: number | null;
}

export interface RefillEvent {
  station_id: string;
  fuel_type: FuelType;
  quantity_gallons?: number;
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
  current_stock_gallons?: number;
  capacity_gallons?: number;
  current_stock_liters: number;
  capacity_liters: number;
  stock_percentage: number;
  days_until_empty: number;
  location_name?: string | null;
}

// ─── Metrics Types ───────────────────────────────────────────────────────────

export interface ConsumptionMetric {
  timestamp: string;
  total_gallons?: number;
  total_liters: number;
  event_count: number;
  station_id?: string | null;
  fuel_type?: string | null;
}

export interface FuelNetworkSummary {
  total_stations: number;
  total_capacity_gallons?: number;
  total_current_stock_gallons?: number;
  total_daily_consumption_gallons?: number;
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

export function getFuelStationCapacityGallons(
  station:
    | Pick<FuelStation, "capacity_gallons" | "capacity_liters">
    | null
    | undefined,
): number {
  if (!station) return 0;
  return station.capacity_gallons ?? litersToGallons(station.capacity_liters);
}

export function getFuelStationCurrentStockGallons(
  station:
    | Pick<FuelStation, "current_stock_gallons" | "current_stock_liters">
    | null
    | undefined,
): number {
  if (!station) return 0;
  return (
    station.current_stock_gallons ??
    litersToGallons(station.current_stock_liters)
  );
}

export function getFuelStationDailyConsumptionGallons(
  station:
    | Pick<
        FuelStation,
        "daily_consumption_rate_gallons" | "daily_consumption_rate"
      >
    | null
    | undefined,
): number {
  if (!station) return 0;
  return (
    station.daily_consumption_rate_gallons ??
    litersToGallons(station.daily_consumption_rate)
  );
}

export function getEventQuantityGallons(
  event:
    | Pick<
        ConsumptionEvent | RefillEvent,
        "quantity_gallons" | "quantity_liters"
      >
    | null
    | undefined,
): number {
  if (!event) return 0;
  return event.quantity_gallons ?? litersToGallons(event.quantity_liters);
}

export function getNetworkCapacityGallons(
  summary:
    | Pick<
        FuelNetworkSummary,
        "total_capacity_gallons" | "total_capacity_liters"
      >
    | null
    | undefined,
): number {
  if (!summary) return 0;
  return (
    summary.total_capacity_gallons ??
    litersToGallons(summary.total_capacity_liters)
  );
}

export function getNetworkCurrentStockGallons(
  summary:
    | Pick<
        FuelNetworkSummary,
        "total_current_stock_gallons" | "total_current_stock_liters"
      >
    | null
    | undefined,
): number {
  if (!summary) return 0;
  return (
    summary.total_current_stock_gallons ??
    litersToGallons(summary.total_current_stock_liters)
  );
}

// ─── Efficiency Types ─────────────────────────────────────────────────────────

/**
 * Per-asset (truck/vehicle) fuel-economy row returned by
 * ``GET /fuel/metrics/efficiency``. Mirrors the backend
 * :class:`fuel.models.EfficiencyMetric` exactly.
 *
 * The backend aggregates ``fuel_events`` by ``asset_id`` and derives
 * distance from the min→max ``odometer_reading`` in the window, so
 * ``total_distance_km`` and the derived ``liters_per_km`` are ``null``
 * when no odometer data was recorded. The canonical efficiency figure is
 * **liters per km** (lower is better); the UI converts to km/L for
 * display via :func:`efficiencyKmPerLiter`.
 */
export interface EfficiencyMetric {
  asset_id: string;
  total_liters: number;
  total_distance_km?: number | null;
  liters_per_km?: number | null;
  event_count: number;
}

/**
 * Convert the backend's ``liters_per_km`` into km/L for display.
 * Returns ``null`` when efficiency is unknown (no odometer data) so the
 * UI can render a distinct "no data" state rather than a misleading 0.
 */
export function efficiencyKmPerLiter(
  metric: Pick<EfficiencyMetric, "liters_per_km">,
): number | null {
  const lpk = metric.liters_per_km;
  if (lpk == null || !Number.isFinite(lpk) || lpk <= 0) return null;
  return 1 / lpk;
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
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options?.headers as Record<string, string> | undefined),
    };

    // Session cookie + anti-CSRF token are attached by the SuperTokens SDK;
    // an auth failure triggers a refresh-then-retry, else a redirect to
    // sign-in (Req 8.4, 8.5).
    const response = await fetchWithSession(fetchWithTimeout, url, {
      ...options,
      headers,
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
  capacity_gallons: number;
  initial_stock_gallons: number;
  capacity_liters?: number;
  initial_stock_liters?: number;
  location?: GeoPoint;
  location_name?: string;
  alert_threshold_pct: number;
}

export interface UpdateStationPayload {
  name?: string;
  fuel_type?: FuelType;
  capacity_gallons?: number;
  capacity_liters?: number;
  location?: GeoPoint;
  location_name?: string;
  alert_threshold_pct?: number;
}

// ─── Plan Execution Lifecycle Types ──────────────────────────────────────────

export interface PlanListItem {
  plan_id: string;
  run_id?: string | null;
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
  plan_id?: string | null;
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
  quantity_gallons?: number;
  compartment_capacity_gallons?: number;
  quantity_liters: number;
  compartment_capacity_liters: number;
}

export function getAssignmentQuantityGallons(
  assignment:
    | Pick<CompartmentAssignment, "quantity_gallons" | "quantity_liters">
    | null
    | undefined,
): number {
  if (!assignment) return 0;
  return (
    assignment.quantity_gallons ?? litersToGallons(assignment.quantity_liters)
  );
}

export function getAssignmentCapacityGallons(
  assignment:
    | Pick<
        CompartmentAssignment,
        "compartment_capacity_gallons" | "compartment_capacity_liters"
      >
    | null
    | undefined,
): number {
  if (!assignment) return 0;
  return (
    assignment.compartment_capacity_gallons ??
    litersToGallons(assignment.compartment_capacity_liters)
  );
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
  return fuelRequest<PaginatedResponse<Forecast>>(`/fuel/mvp/forecasts${qs}`);
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
  return fuelRequest<PaginatedResponse<PlanListItem>>(`/fuel/mvp/plans${qs}`);
}

/** POST /api/fuel/mvp/plan/:id/approve — approve a draft plan */
export async function approvePlan(
  planId: string,
  tenantId: string,
  dispatcherId: string,
): Promise<{ plan_id: string; status: string }> {
  const qs = buildQueryString({
    tenant_id: tenantId,
    dispatcher_id: dispatcherId,
  });
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
  const qs = buildQueryString({
    tenant_id: tenantId,
    dispatcher_id: dispatcherId,
  });
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
): Promise<{
  plan_id: string;
  completed_stops: number;
  total_stops: number;
  status: string;
}> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<{
    plan_id: string;
    completed_stops: number;
    total_stops: number;
    status: string;
  }>(`/fuel/mvp/plan/${encodeURIComponent(planId)}/checkin${qs}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
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
): Promise<{
  data: {
    estimated: CostBreakdown;
    actual?: CostBreakdown;
    cost_variance_pct?: number;
  };
  request_id: string;
}> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return fuelRequest<{
    data: {
      estimated: CostBreakdown;
      actual?: CostBreakdown;
      cost_variance_pct?: number;
    };
    request_id: string;
  }>(`/fuel/mvp/plan/${encodeURIComponent(planId)}/costs${qs}`);
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

// ─── Customer Tank Types (Fuel Ops Hardening Req 1.1, 1.6) ───────────────────

/**
 * Customer tank segmentation used by the forecaster to pick a consumption
 * model multiplier. Mirrors the backend ``CustomerType`` literal enum.
 */
export type CustomerTankCustomerType =
  | "residential"
  | "commercial"
  | "keep_full"
  | "will_call"
  | "auto_fill";

/**
 * Narrow fuel-family enum used to pick a Consumption_Model strategy.
 * Distinct from the catalog ``fuel_product_code`` which holds the canonical
 * US product code (e.g. PROPANE, HEATING_OIL).
 */
export type CustomerTankFuelType =
  | "propane"
  | "heating_oil"
  | "diesel"
  | "generator_fuel"
  | "farm_fuel"
  | "gasoline";

/** Lifecycle status of a customer tank. Forecasts run only on ``active``. */
export type CustomerTankStatus = "active" | "inactive" | "maintenance";

/** Optional high-level use-case flag surfaced for storm-mode prioritization. */
export type CustomerTankUseCase =
  | "residential_heat"
  | "commercial_heat"
  | "generator"
  | "farm"
  | "other";

export interface CustomerTank {
  customer_tank_id: string;
  tenant_id: string;
  customer_id: string;
  last_refill_order_id?: string | null;
  customer_type: CustomerTankCustomerType;
  fuel_type: CustomerTankFuelType;
  fuel_product_code: string;
  capacity_gallons: number;
  current_level_gallons: number;
  last_reading_at?: string | null;
  location_lat: number;
  location_lon: number;
  zip_code: string;
  k_factor?: number | null;
  use_case?: CustomerTankUseCase | null;
  status: CustomerTankStatus;
  updated_at?: string | null;
  created_at?: string | null;
}

// ─── Cross-Module Resolver Links (cross-module-entity-linkage Req 7.2/7.3/5.4) ─

/**
 * A single resolved reference as returned in a customer-tank resolver read's
 * ``links`` object. Structurally identical to the backend ``RefResolver`` /
 * ``ResolvedRef`` payload and the ``ResolvedLink`` exported by ``ordersApi``,
 * and accepted directly by the shared ``<EntityLink>`` component.
 *
 * - ``resolved``  — the id resolved to a same-tenant entity; ``summary`` holds
 *   a small display payload.
 * - ``unresolved`` — an id was present but did not resolve; render "Unlinked".
 * - ``empty`` — no id was supplied (the reference is simply absent).
 */
export type CustomerTankResolvedLink =
  | { status: "resolved"; id: string; summary: Record<string, unknown> }
  | { status: "unresolved"; id: string }
  | { status: "empty"; id?: string | null };

/**
 * The ``links`` object on a customer-tank resolver read
 * (``GET /api/fuel/mvp/customer-tanks/{id}?expand=customer,last_refill_order``).
 * Each key is present only when requested via ``expand``.
 */
export interface CustomerTankLinks {
  customer?: CustomerTankResolvedLink;
  last_refill_order?: CustomerTankResolvedLink;
}

/** The entity references a customer-tank resolver read can expand. */
export type CustomerTankExpand = "customer" | "last_refill_order";

/** A customer tank plus its resolved cross-module ``links`` (expand read). */
export interface CustomerTankWithLinks extends CustomerTank {
  links: CustomerTankLinks;
}

export interface CustomerTankListResponse {
  items: CustomerTank[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

export interface CustomerTankCreatePayload {
  customer_tank_id?: string;
  customer_id: string;
  last_refill_order_id?: string;
  customer_type: CustomerTankCustomerType;
  fuel_type: CustomerTankFuelType;
  fuel_product_code: string;
  capacity_gallons: number;
  current_level_gallons: number;
  last_reading_at?: string;
  location_lat: number;
  location_lon: number;
  zip_code: string;
  k_factor?: number | null;
  use_case?: CustomerTankUseCase;
  status?: CustomerTankStatus;
}

export interface CustomerTankUpdatePayload {
  customer_id?: string;
  last_refill_order_id?: string;
  customer_type?: CustomerTankCustomerType;
  fuel_type?: CustomerTankFuelType;
  fuel_product_code?: string;
  capacity_gallons?: number;
  current_level_gallons?: number;
  last_reading_at?: string;
  location_lat?: number;
  location_lon?: number;
  zip_code?: string;
  k_factor?: number | null;
  use_case?: CustomerTankUseCase;
  status?: CustomerTankStatus;
}

export interface CustomerTankListFilters {
  status?: CustomerTankStatus;
  customer_id?: string;
  customer_type?: CustomerTankCustomerType;
  fuel_type?: CustomerTankFuelType;
  zip_code?: string;
  page?: number;
  size?: number;
}

// ─── Customer Tank Endpoints ─────────────────────────────────────────────────

/** GET /api/fuel/mvp/customer-tanks — list tanks for the tenant (Req 1.6.2). */
export async function listCustomerTanks(
  filters: CustomerTankListFilters = {},
): Promise<CustomerTankListResponse> {
  const qs = buildQueryString(filters);
  return fuelRequest<CustomerTankListResponse>(`/fuel/mvp/customer-tanks${qs}`);
}

/** GET /api/fuel/mvp/customer-tanks/{id} — fetch a single tank (Req 1.6.2). */
export async function getCustomerTank(
  customerTankId: string,
): Promise<CustomerTank> {
  return fuelRequest<CustomerTank>(
    `/fuel/mvp/customer-tanks/${encodeURIComponent(customerTankId)}`,
  );
}

/**
 * GET /api/fuel/mvp/customer-tanks/{id}?expand=... — fetch a tank together with
 * a resolved ``links`` object for the requested cross-module references
 * (customer, last_refill_order). Mirrors the order resolver read contract
 * (cross-module-entity-linkage Req 7.2, 7.3, 5.4).
 */
export async function getCustomerTankWithLinks(
  customerTankId: string,
  expand: CustomerTankExpand[] = ["customer", "last_refill_order"],
): Promise<CustomerTankWithLinks> {
  const qs = expand.length
    ? `?expand=${encodeURIComponent(expand.join(","))}`
    : "";
  return fuelRequest<CustomerTankWithLinks>(
    `/fuel/mvp/customer-tanks/${encodeURIComponent(customerTankId)}${qs}`,
  );
}

/** POST /api/fuel/mvp/customer-tanks — create a tank (Req 1.6.3). */
export async function createCustomerTank(
  payload: CustomerTankCreatePayload,
): Promise<CustomerTank> {
  return fuelRequest<CustomerTank>("/fuel/mvp/customer-tanks", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** PATCH /api/fuel/mvp/customer-tanks/{id} — partial update (Req 1.6.3). */
export async function updateCustomerTank(
  customerTankId: string,
  payload: CustomerTankUpdatePayload,
): Promise<CustomerTank> {
  return fuelRequest<CustomerTank>(
    `/fuel/mvp/customer-tanks/${encodeURIComponent(customerTankId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

// ─── Customer Tank Forecast Types (Fuel Ops Hardening Capability 1) ──────────

/**
 * A per-customer-tank runout forecast row as persisted in
 * ``mvp_tank_forecasts`` and returned by ``GET /api/fuel/mvp/forecasts``.
 *
 * This is the forecast-driven signal that distinguishes Customer Tanks
 * from Fuel Stations: the TankForecastingAgent runs a per-tank
 * Consumption_Model (propane k-factor, heating-oil HDD regression, etc.)
 * and writes ``hours_to_runout_p50/p90`` plus a 24-hour runout
 * probability. Only customer-tank forecasts carry ``customer_tank_id``;
 * retail-station forecasts key off ``station_id`` instead.
 *
 * Every field beyond the identity pair is optional so the type tolerates
 * both the legacy station shape and partial documents.
 */
export interface CustomerTankForecast {
  forecast_id?: string;
  customer_tank_id?: string | null;
  station_id?: string | null;
  customer_id?: string | null;
  customer_type?: string | null;
  fuel_type?: string | null;
  fuel_grade?: string | null;
  /** Median hours until the tank runs dry. */
  hours_to_runout_p50?: number | null;
  /** Conservative (90th percentile) hours until runout. */
  hours_to_runout_p90?: number | null;
  /** Probability (0–1) the tank runs out within the next 24 hours. */
  runout_risk_24h?: number | null;
  /** Model confidence (0–1). */
  confidence?: number | null;
  model_name?: string | null;
  anomaly_flags?: string[];
  timestamp?: string | null;
}

/** Filters accepted by ``GET /api/fuel/mvp/forecasts``. */
export interface CustomerTankForecastFilters {
  tenant_id: string;
  customer_tank_id?: string;
  customer_id?: string;
  customer_type?: CustomerTankCustomerType;
  fuel_type?: CustomerTankFuelType;
  page?: number;
  size?: number;
}

/**
 * GET ``/api/fuel/mvp/forecasts`` — paginated tank runout forecasts.
 *
 * Used by the Customer Tanks tab to join each tank to its latest runout
 * forecast. The backend sorts by ``timestamp`` desc, so the first
 * forecast seen per ``customer_tank_id`` is the freshest.
 */
export async function listCustomerTankForecasts(
  filters: CustomerTankForecastFilters,
): Promise<PaginatedResponse<CustomerTankForecast>> {
  const qs = buildQueryString(filters);
  return fuelRequest<PaginatedResponse<CustomerTankForecast>>(
    `/fuel/mvp/forecasts${qs}`,
  );
}

// ─── Emergency Stop Types (Fuel Ops Hardening Req 2.4.1, 2.4.4) ──────────────

/**
 * Request body for
 * ``POST /api/fuel/mvp/routes/{route_id}/emergency-stop``.
 *
 * Exactly one of ``station_id`` / ``customer_tank_id`` is required. The
 * backend canonicalizes ``fuel_grade`` via the US fuel product catalog so
 * supported supplier and terminal codes resolve to the persisted route key.
 * ``SLA_by`` is optional ISO-8601.
 */
export interface EmergencyStopRequest {
  station_id?: string;
  customer_tank_id?: string;
  fuel_grade: string;
  requested_gallons: number;
  priority_reason: string;
  SLA_by?: string;
}

/** Reason codes returned on HTTP 409 when insertion is infeasible. */
export type EmergencyStopReason =
  | "capacity_insufficient"
  | "sla_breach"
  | "truck_off_duty";

/** One entry in the per-stop ``diff.added_stops`` / ``diff.removed_stops``. */
export interface ReplanStopRef {
  stop_id: string;
  index: number;
  gallons?: number | null;
  product_code?: string | null;
  eta?: string | null;
}

export interface ReplanReorderedStop {
  stop_id: string;
  before_index: number;
  after_index: number;
}

export interface ReplanReassignedStop {
  stop_id: string;
  from_truck_id: string;
  to_truck_id: string;
}

export interface ReplanQuantityChange {
  stop_id: string;
  before_gallons: number;
  after_gallons: number;
  product_code?: string | null;
}

export interface ReplanEtaShift {
  stop_id: string;
  before_eta: string;
  after_eta: string;
  shift_minutes: number;
}

/**
 * Structured ReplanDiff matching ``Agents.support.replan_diff_models.ReplanDiff``.
 *
 * Used by the emergency-stop response (``diff`` field) and by the
 * ``GET /api/fuel/mvp/replans/{event_id}/diff`` endpoint.
 */
export interface ReplanDiff {
  diff_id: string;
  original_route_id: string;
  patched_route_id: string;
  added_stops: ReplanStopRef[];
  removed_stops: ReplanStopRef[];
  reordered_stops: ReplanReorderedStop[];
  reassigned_stops: ReplanReassignedStop[];
  quantity_changes: ReplanQuantityChange[];
  eta_shifts: ReplanEtaShift[];
  generated_at: string;
}

export interface EmergencyStopResponse {
  event_id: string;
  route_id: string;
  tenant_id: string;
  insert_index: number;
  stops_shifted_count: number;
  added_distance_km: number;
  risk_level: "medium" | "high";
  confirmation_method: string;
  approval_id?: string | null;
  sla_at_risk: boolean;
  diff: ReplanDiff;
}

/**
 * Envelope returned by ``GET /api/fuel/mvp/replans/{event_id}/diff``
 * (Req 2.5.3). ``replan_type`` and ``status`` come from the surrounding
 * ReplanEvent so the UI can render context alongside the diff.
 */
export interface ReplanDiffResponse {
  event_id: string;
  replan_type: string;
  status: string;
  diff: ReplanDiff;
}

// ─── Emergency Stop + Replan Diff Endpoints ──────────────────────────────────

/**
 * POST ``/api/fuel/mvp/routes/{route_id}/emergency-stop`` — insert an
 * urgent delivery into an active route (Task 4.9, Req 2.4.1).
 *
 * The backend may respond with HTTP 409 and one of the reason codes
 * ``capacity_insufficient``, ``sla_breach``, ``truck_off_duty`` — those
 * surface to the caller as an :class:`ApiError` whose ``message``
 * contains the reason.
 */
export async function insertEmergencyStop(
  routeId: string,
  body: EmergencyStopRequest,
): Promise<EmergencyStopResponse> {
  return fuelRequest<EmergencyStopResponse>(
    `/fuel/mvp/routes/${encodeURIComponent(routeId)}/emergency-stop`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/**
 * GET ``/api/fuel/mvp/replans/{event_id}/diff`` — fetch the structured
 * Replan_Diff persisted for a replan event (Task 4.10, Req 2.5.3).
 *
 * Returns HTTP 404 for missing or cross-tenant events; callers should
 * surface that distinction in the UI.
 */
export async function getReplanDiff(
  eventId: string,
): Promise<ReplanDiffResponse> {
  return fuelRequest<ReplanDiffResponse>(
    `/fuel/mvp/replans/${encodeURIComponent(eventId)}/diff`,
  );
}

// ─── Priorities: safe_to_delay + cluster extensions (Req 3.1.4, 3.4.3) ───────

/** Safe-to-delay bucket filter values for ``GET /api/fuel/mvp/priorities``. */
export type SafeToDelayBucket = "none" | "short" | "medium" | "long";

/**
 * Extended priority entry carried inside ``priorities`` on every
 * ``mvp_delivery_priorities`` document. All hardening fields are
 * optional so this type is backwards compatible with pre-Capability-3
 * runs that only persisted the original ``priority_score`` columns.
 */
export interface PriorityEntry {
  station_id?: string;
  customer_tank_id?: string;
  station_name?: string;
  fuel_grade: string;
  priority_score?: number;
  priority_bucket?: string;
  urgency?: string;
  timestamp?: string;
  // Req 3.1.3 — safe-to-delay scoring
  safe_to_delay_days?: number;
  safe_to_delay_bucket?: SafeToDelayBucket;
  // Req 3.3.3 / 3.3.4 — business impact
  business_impact_score?: number;
  business_impact_reasons?: string[];
  // Req 3.4.2 — priority clustering
  cluster_id?: string | null;
  cluster_size?: number | null;
}

/** Wrapper envelope as returned by ``GET /api/fuel/mvp/priorities``. */
export interface PriorityListEntry {
  run_id?: string;
  tenant_id?: string;
  timestamp?: string;
  priorities?: PriorityEntry[];
}

export interface PriorityListFilters {
  safe_to_delay_bucket?: SafeToDelayBucket;
  run_id?: string;
  page?: number;
  size?: number;
}

/**
 * GET ``/api/fuel/mvp/priorities`` — paginated list of priority-run
 * documents, optionally filtered by ``safe_to_delay_bucket`` (Req 3.1.4).
 */
export async function getPriorityLists(
  filters: PriorityListFilters = {},
): Promise<PaginatedResponse<PriorityListEntry>> {
  const qs = buildQueryString(filters);
  return fuelRequest<PaginatedResponse<PriorityListEntry>>(
    `/fuel/mvp/priorities${qs}`,
  );
}

// ─── Depot Types (Fuel Ops Hardening Req 2.2.1, 2.2.2) ───────────────────────

/**
 * Lifecycle status of a Depot. Only ``active`` depots participate in
 * route planning per the backend :class:`Depot` model.
 */
export type DepotStatus = "active" | "inactive";

/**
 * Depot record as returned by ``GET /api/fuel/mvp/depots`` and the
 * create / update endpoints. Mirrors the backend
 * :class:`fuel.depot_models.Depot` Pydantic model exactly so
 * ``JSON.stringify(depot)`` round-trips through the API without loss.
 *
 * ``fuel_types_supported`` always holds *canonical* US product codes
 * (e.g. ``DIESEL_2``, ``PROPANE``) so the UI can treat this list as
 * already-normalized.
 */
export interface Depot {
  depot_id: string;
  tenant_id: string;
  name: string;
  location_lat: number;
  location_lon: number;
  address: string;
  timezone: string;
  fuel_types_supported: string[];
  status: DepotStatus;
  /**
   * Whether this depot is the tenant's default. The backend
   * :class:`fuel.depot_models.Depot` model round-trips this flag on every
   * read (list and single-depot reads), so the UI reads it directly rather
   * than inferring the default from a loosely-typed shape (Req 10.3).
   */
  is_default?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

/** Body for ``POST /api/fuel/mvp/depots``. */
export interface DepotCreatePayload {
  /** Optional client-supplied id — backend mints ``depot_<uuid4>`` when omitted. */
  depot_id?: string;
  name: string;
  location_lat: number;
  location_lon: number;
  address: string;
  timezone: string;
  fuel_types_supported: string[];
  status?: DepotStatus;
}

/** Partial-update body for ``PATCH /api/fuel/mvp/depots/{depot_id}``. */
export interface DepotUpdatePayload {
  name?: string;
  location_lat?: number;
  location_lon?: number;
  address?: string;
  timezone?: string;
  fuel_types_supported?: string[];
  status?: DepotStatus;
  /**
   * Marks this depot as the tenant's default. The backend ``PATCH``
   * contract accepts this flag and enforces a single default per tenant:
   * setting one depot as default clears the flag on any other depot
   * (see :class:`fuel.depot_models.DepotRepository`).
   */
  is_default?: boolean;
}

/** Query filters accepted by ``GET /api/fuel/mvp/depots``. */
export interface DepotListFilters {
  status?: DepotStatus;
  fuel_type?: string;
  page?: number;
  size?: number;
}

/** Envelope returned by ``GET /api/fuel/mvp/depots``. */
export interface DepotListResponse {
  items: Depot[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

/** Expansions accepted by ``GET /api/fuel/mvp/depots/{depot_id}``. */
export type DepotExpand = "assets";

/**
 * One asset assigned to a depot, returned under ``assigned_assets`` by
 * ``GET /api/fuel/mvp/depots/{depot_id}?expand=assets`` (Req 10.2).
 */
export interface DepotAssetSummary {
  asset_id: string;
  name?: string | null;
  asset_type?: string | null;
  status?: string | null;
}

/**
 * Envelope returned by ``GET /api/fuel/mvp/depots/{depot_id}``. ``depot``
 * round-trips the full record (including ``is_default``); ``assigned_assets``
 * is present only when ``?expand=assets`` was requested (Req 10.2, 10.3).
 */
export interface DepotReadResponse {
  depot: Depot;
  assigned_assets?: DepotAssetSummary[] | null;
}

// ─── Depot Endpoints ─────────────────────────────────────────────────────────

/** GET /api/fuel/mvp/depots — list depots for the tenant (Req 2.2.2). */
export async function listDepots(
  filters: DepotListFilters = {},
): Promise<DepotListResponse> {
  const qs = buildQueryString(filters);
  return fuelRequest<DepotListResponse>(`/fuel/mvp/depots${qs}`);
}

/**
 * GET /api/fuel/mvp/depots/{depot_id} — fetch a single depot, round-tripping
 * the ``is_default`` flag (Req 10.3). Pass ``expand=["assets"]`` to enumerate
 * the assets assigned to the depot (Req 10.2).
 */
export async function getDepot(
  depotId: string,
  options: { expand?: DepotExpand[] } = {},
): Promise<DepotReadResponse> {
  const expand = options.expand?.length
    ? `?expand=${encodeURIComponent(options.expand.join(","))}`
    : "";
  return fuelRequest<DepotReadResponse>(
    `/fuel/mvp/depots/${encodeURIComponent(depotId)}${expand}`,
  );
}

/** POST /api/fuel/mvp/depots — create a depot (Req 2.2.2). */
export async function createDepot(payload: DepotCreatePayload): Promise<Depot> {
  return fuelRequest<Depot>("/fuel/mvp/depots", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** PATCH /api/fuel/mvp/depots/{depot_id} — partial update (Req 2.2.2). */
export async function updateDepot(
  depotId: string,
  payload: DepotUpdatePayload,
): Promise<Depot> {
  return fuelRequest<Depot>(`/fuel/mvp/depots/${encodeURIComponent(depotId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

/** DELETE /api/fuel/mvp/depots/{depot_id} — delete a depot (Req 2.2.2).
 *
 * The backend returns HTTP 204 with an empty body on success, so this
 * helper bypasses :func:`fuelRequest`'s unconditional ``response.json()``
 * decode and only validates the status code.
 */
export async function deleteDepot(depotId: string): Promise<void> {
  const url = `${API_BASE_URL}/fuel/mvp/depots/${encodeURIComponent(depotId)}`;
  let response: Response;
  try {
    response = await fetchWithTimeout(url, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    if (error instanceof ApiTimeoutError || error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      body.detail || body.message || `HTTP error! status: ${response.status}`,
      response.status,
    );
  }
}

// ─── Reconciliation Types (Fuel Ops Hardening Req 4.4.1, 4.4.2, 4.4.4) ───────

/**
 * Four-way reconciliation record returned by
 * ``GET /api/fuel/mvp/reconciliation``. Mirrors the backend
 * :class:`services.reconciliation_service.ReconciliationRecord` Pydantic
 * model, including nullable QBO-supplied fields (``invoice_id``,
 * ``invoiced_gallons``, ``variance_invoiced_vs_delivered_pct``) which
 * arrive seconds-to-minutes after the POD is finalized.
 *
 * ``alert_flags`` carries the tenant-configured threshold breach
 * markers — the backend emits ``variance_exceeds_threshold`` when any
 * of the three variance percentages crosses ``variance_alert_pct``
 * (default 3.0%) (Req 4.4.3).
 */
export interface ReconciliationRecord {
  reconciliation_id: string;
  tenant_id: string;
  order_id: string;
  plan_id: string;
  pod_id: string;
  invoice_id?: string | null;
  /**
   * Cross-module-entity-linkage Req 12.2: who/what was responsible for the
   * delivery, derived from the underlying order so a variance can be pivoted
   * to the responsible customer / asset / driver. Nullable/additive — orders
   * that predate the linkage fields leave these absent (rendered "unlinked").
   */
  customer_id?: string | null;
  assigned_asset_id?: string | null;
  assigned_driver_id?: string | null;
  ordered_gallons: number;
  loaded_gallons: number;
  delivered_gallons: number;
  invoiced_gallons?: number | null;
  variance_load_vs_order_pct: number;
  variance_delivered_vs_loaded_pct: number;
  variance_invoiced_vs_delivered_pct?: number | null;
  alert_flags: string[];
  generated_at: string;
}

/** Query filters accepted by ``GET /api/fuel/mvp/reconciliation``. */
export interface ReconciliationListFilters {
  order_id?: string;
  plan_id?: string;
  pod_id?: string;
  /**
   * Return only rows where the absolute value of *any* variance
   * percentage meets or exceeds this threshold. Non-negative.
   */
  min_variance_pct?: number;
  page?: number;
  size?: number;
}

/** Envelope returned by ``GET /api/fuel/mvp/reconciliation``. */
export interface ReconciliationListResponse {
  items: ReconciliationRecord[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

// ─── Reconciliation Endpoints ────────────────────────────────────────────────

/**
 * GET ``/api/fuel/mvp/reconciliation`` — paginated list of
 * :class:`ReconciliationRecord` rows scoped to the current tenant
 * (Req 4.4.4).
 */
export async function listReconciliationRecords(
  filters: ReconciliationListFilters = {},
): Promise<ReconciliationListResponse> {
  const qs = buildQueryString(filters);
  return fuelRequest<ReconciliationListResponse>(
    `/fuel/mvp/reconciliation${qs}`,
  );
}

// ─── BOL Download Types (Fuel Ops Hardening Req 4.3.4, 4.3.5) ────────────────

/**
 * Lifecycle status of a Bill-of-Lading row. ``generated`` means the PDF
 * was rendered and uploaded, ``pending_regeneration`` means the
 * synchronous generation failed and the row is a placeholder awaiting a
 * retry. The UI uses the two values to decide whether to surface the
 * download link or a retry hint.
 */
export type BOLStatus = "generated" | "pending_regeneration" | string;

/**
 * Response envelope for ``GET /api/fuel/pod/{pod_id}/bol``. The
 * ``download_url`` and ``expires_at`` fields are omitted when the BOL
 * is still in ``pending_regeneration`` — clients MUST handle the
 * absence rather than assuming a URL is always present (Req 4.3.4).
 */
export interface BOLDownloadResponse {
  bol_id: string;
  pod_id: string;
  status: BOLStatus;
  hash: string;
  generated_at?: string | null;
  file_ref?: string | null;
  download_url?: string | null;
  expires_at?: string | null;
  tenant_id: string;
}

/**
 * GET ``/api/fuel/pod/{pod_id}/bol`` — return a short-lived presigned
 * download URL for the Bill-of-Lading PDF tied to the POD. Returns a
 * ``pending_regeneration`` row (no ``download_url``) when the BOL has
 * not yet been generated successfully (Req 4.3.5).
 */
export async function getPodBol(podId: string): Promise<BOLDownloadResponse> {
  return fuelRequest<BOLDownloadResponse>(
    `/fuel/pod/${encodeURIComponent(podId)}/bol`,
  );
}

// ─── Storm_Mode Types (Fuel Ops Hardening Req 9.1.6, 9.4.2, 9.4.3) ───────────

/**
 * Effective Storm_Mode state reported by
 * ``GET /api/fuel/storm-mode/status``. Mirrors
 * :data:`fuel.services.storm_mode_evaluator.ACTIVE` /
 * :data:`fuel.services.storm_mode_evaluator.INACTIVE`. ``active`` means
 * the dispatcher banner SHOULD be pinned; ``inactive`` hides it.
 */
export type StormModeState = "active" | "inactive";

/**
 * Severity bucket on an ingested :class:`WeatherAlert`. The backend
 * ``WeatherAlertSeverity`` literal uses lower-case strings; Storm_Mode
 * flips ``active`` when any triggering alert meets or exceeds the
 * tenant-configured threshold (default ``severe``) — see Req 9.1.3.
 */
export type WeatherAlertSeverity = "minor" | "moderate" | "severe" | "extreme";

/**
 * Lifecycle status on a :class:`WeatherAlert`. ``forecast`` means the
 * alert has not fired yet but is within the activation window;
 * ``active`` means it has fired; ``cleared`` / ``cancelled`` are
 * terminal states the banner uses to decide whether to surface the
 * alert at all.
 */
export type WeatherAlertStatus =
  | "forecast"
  | "active"
  | "cleared"
  | "cancelled";

/**
 * Provenance of an ingested :class:`WeatherAlert`. ``noaa`` / ``nws``
 * come from the autonomous ingester (Task 10.2); ``manual`` covers
 * dispatcher-uploaded advisories; ``weather_provider`` is reserved for
 * the pluggable Req 1.2 adapter.
 */
export type WeatherAlertSource = "noaa" | "nws" | "manual" | "weather_provider";

/**
 * Override action an operator may submit through
 * ``POST /api/fuel/storm-mode/override``. Mirrors
 * :data:`fuel.storm_mode_models.StormModeOverrideAction`.
 *
 * * ``activate`` — force Storm_Mode on regardless of alerts.
 * * ``deactivate`` — force Storm_Mode off regardless of alerts.
 * * ``snooze`` — suppress automatic activation until ``expires_at``.
 * * ``clear`` — remove any prior override without changing state.
 */
export type StormModeOverrideAction =
  | "activate"
  | "deactivate"
  | "snooze"
  | "clear";

/**
 * Condensed WeatherAlert view embedded in the status response. Matches
 * the backend :class:`StormModeTriggeringAlert` Pydantic model — the
 * banner renders ``headline`` when present and falls back to
 * ``alert_type`` otherwise.
 */
export interface StormModeTriggeringAlert {
  alert_id: string;
  alert_type: string;
  severity: WeatherAlertSeverity;
  headline?: string | null;
  description?: string | null;
  expected_start_at: string;
  expected_end_at?: string | null;
  affected_zip_codes: string[];
  source: WeatherAlertSource;
  activation_status: WeatherAlertStatus;
}

/**
 * Condensed override view embedded in the status response when an
 * override is in effect. Mirrors :class:`StormModeActiveOverride`.
 */
export interface StormModeActiveOverride {
  override_id: string;
  action: StormModeOverrideAction;
  reason: string;
  actor_id: string;
  expires_at?: string | null;
}

/**
 * Activation window carried on every status response — ``lookahead_hours``
 * + ``severity_threshold`` describe *how* the evaluator decides to
 * transition, ``activated_at`` / ``clears_at`` describe *when* the
 * current posture began and is expected to end. ``clears_at`` is
 * nullable when the triggering alert has no ``expected_end_at``.
 */
export interface StormModeActivationWindow {
  lookahead_hours: number;
  severity_threshold: WeatherAlertSeverity;
  activated_at?: string | null;
  clears_at?: string | null;
}

/**
 * Full envelope for ``GET /api/fuel/storm-mode/status``. Mirrors the
 * backend :class:`StormModeStatusResponse` Pydantic model exactly.
 * ``state`` is the effective state (after override precedence) — the
 * banner should render off this field, not ``computed_state``.
 *
 * Validates: Requirements 9.1.6, 9.4.3.
 */
export interface StormModeStatusResponse {
  tenant_id: string;
  state: StormModeState;
  computed_state: StormModeState;
  override_active: boolean;
  override?: StormModeActiveOverride | null;
  triggering_alerts: StormModeTriggeringAlert[];
  activation_window: StormModeActivationWindow;
  updated_at?: string | null;
}

/**
 * Request body for ``POST /api/fuel/storm-mode/override``. Mirrors
 * :class:`StormModeOverrideCreateRequest` on the backend; ``tenant_id``
 * and ``override_id`` are stamped server-side from the JWT context so
 * the client cannot spoof ownership. ``reason`` and ``actor_id`` are
 * required non-blank strings (the backend strips and rejects whitespace
 * -only values with HTTP 422 ``validation_error``).
 *
 * Validates: Requirement 9.4.2.
 */
export interface StormModeOverridePayload {
  action: StormModeOverrideAction;
  reason: string;
  actor_id: string;
  expires_at?: string | null;
}

/**
 * Persisted override row returned by
 * ``POST /api/fuel/storm-mode/override`` (HTTP 201) — mirrors the
 * backend :class:`fuel.storm_mode_models.StormModeOverride` Pydantic
 * model. Carries the stamped ``override_id`` / ``tenant_id`` so the
 * dispatcher UI can reference them in follow-up audit links.
 */
export interface StormModeOverride {
  override_id: string;
  tenant_id: string;
  action: StormModeOverrideAction;
  reason: string;
  actor_id: string;
  expires_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

// ─── Storm_Mode Endpoints ────────────────────────────────────────────────────

/**
 * GET ``/api/fuel/storm-mode/status`` — return the current Storm_Mode
 * state for the requesting tenant alongside the triggering alerts, any
 * active manual override, and the activation window (Task 10.4,
 * Req 9.1.6, 9.4.3).
 *
 * The backend responds HTTP 503 ``storm_mode_evaluator_unavailable``
 * when the evaluator has not been wired by bootstrap. That surfaces as
 * an :class:`ApiError` with ``status === 503`` — the banner treats it
 * as "not configured yet, stay hidden" rather than a blocking error.
 */
export async function getStormModeStatus(): Promise<StormModeStatusResponse> {
  return fuelRequest<StormModeStatusResponse>("/fuel/storm-mode/status");
}

/**
 * POST ``/api/fuel/storm-mode/override`` — persist a dispatcher or
 * admin Storm_Mode override (Task 10.5, Req 9.4.2, 9.4.4).
 *
 * The backend enforces a dispatcher/admin role gate. Callers without
 * the required role receive HTTP 403 ``forbidden_role`` which surfaces
 * as an :class:`ApiError` with ``status === 403``.
 */
export async function submitStormModeOverride(
  payload: StormModeOverridePayload,
): Promise<StormModeOverride> {
  return fuelRequest<StormModeOverride>("/fuel/storm-mode/override", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ─── Capability 8 — Terminal / Rack Sourcing Intelligence ───────────────────
//
// Task 11.8 surfaces the terminal-sourcing UI at ``SourcingPage.tsx``. It
// consumes the backend endpoints declared by :mod:`fuel.api.fuel_ops_endpoints`
// (Tasks 7.3–7.10) and mirrors their Pydantic response shapes 1:1 so
// ``JSON.parse`` needs no post-processing:
//
//   * ``GET  /api/fuel/sourcing/recommendations``        (Task 7.10 / Req 8.5.4)
//   * ``GET  /api/fuel/rack-prices``                      (Task 7.5  / Req 8.2.6)
//   * ``GET  /api/fuel/terminals/{id}/wait-summary``      (Task 7.7  / Req 8.4.4)
//   * ``GET  /api/fuel/supplier-contracts``               (Task 7.6  / Req 8.3.2)
//   * ``GET  /api/fuel/supplier-contracts/{id}``          (Task 7.6  / Req 8.3.2)
//   * ``POST /api/fuel/supplier-contracts``               (Task 7.6  / Req 8.3.2)
//   * ``PATCH /api/fuel/supplier-contracts/{id}``         (Task 7.6  / Req 8.3.2)
//   * ``DELETE /api/fuel/supplier-contracts/{id}``        (Task 7.6  / Req 8.3.2)
//
// All endpoints are tenant-scoped server-side via the JWT-derived
// :class:`TenantContext`; the frontend never appends ``tenant_id`` to
// these query strings (unlike the older ``/fuel/mvp/*`` surface).

// ─── Sourcing Types (Req 8.5.4) ──────────────────────────────────────────────

/**
 * One ranked terminal within a :class:`SourcingRecommendation`.
 *
 * Mirrors :class:`fuel.terminal_models.TerminalCandidate`. ``score`` is
 * normalized 0.0–1.0, ``reasons`` is the human-readable ranking
 * explanation the dispatcher UI renders alongside the row, and
 * ``wait_warning`` is ``true`` when the terminal's rolling 2-hour
 * ``avg_wait_minutes`` exceeds the tenant's
 * ``terminal_wait_warning_minutes`` threshold (default 60 — Req 8.4.5).
 */
export interface SourcingTerminalCandidate {
  terminal_id: string;
  price_per_gallon_usd: number;
  branded_flag: boolean;
  contract_id?: string | null;
  avg_wait_minutes: number;
  distance_km_from_start: number;
  score: number;
  reasons: string[];
  wait_warning: boolean;
}

/**
 * Persisted audit record of a Sourcing_Recommender invocation. Matches
 * :class:`fuel.terminal_models.SourcingRecommendation`. ``candidates``
 * is ordered by ``score`` descending and may be empty when every
 * terminal was disqualified. ``wait_warning_terminal_ids`` is the
 * pre-computed list of every candidate whose ``wait_warning`` flag is
 * true — the UI uses it to render a single "terminal wait warning"
 * summary badge without iterating candidates (Req 8.4.5).
 *
 * ``rack_price_fallback`` is ``true`` when the recommender fell back
 * to the most recent cached rack price because the live provider
 * timed out (Req 8.2.5). Consumers should badge the recommendation so
 * operators know the prices may be slightly stale.
 */
export interface SourcingRecommendation {
  recommendation_id: string;
  request_id: string;
  tenant_id: string;
  truck_id?: string | null;
  run_id?: string | null;
  product_code: string;
  volume_gallons: number;
  origin_lat: number;
  origin_lon: number;
  candidates: SourcingTerminalCandidate[];
  rack_price_fallback: boolean;
  wait_warning_terminal_ids: string[];
  generated_at: string;
  updated_at?: string | null;
  created_at?: string | null;
}

/**
 * Query parameters accepted by ``GET /api/fuel/sourcing/recommendations``.
 * ``product_code`` and ``volume_gallons`` are required; everything else
 * is optional. ``terminal_ids`` is a CSV string (the backend parses it
 * into a de-duplicated list).
 */
export interface SourcingRecommendationsQuery {
  product_code: string;
  volume_gallons: number;
  origin_lat: number;
  origin_lon: number;
  as_of?: string;
  branded?: boolean;
  truck_id?: string;
  run_id?: string;
  /** Comma-separated terminal_id list to restrict the candidate slate. */
  terminal_ids?: string;
}

// ─── Rack Price Types (Req 8.2.6) ────────────────────────────────────────────

/**
 * A single rack-price observation for a (tenant, terminal, product)
 * tuple. Mirrors :class:`integrations.rack_price_provider_base.RackPrice`
 * including the canonical ``product_code`` returned by the rack provider.
 */
export interface RackPrice {
  rack_price_id: string;
  tenant_id: string;
  terminal_id: string;
  product_code: string;
  price_per_gallon_usd: number;
  branded_flag: boolean;
  supplier_brand?: string | null;
  provider: string;
  effective_at: string;
  retrieved_at: string;
}

/** Envelope returned by ``GET /api/fuel/rack-prices``. */
export interface RackPriceListResponse {
  items: RackPrice[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

/** Query filters accepted by ``GET /api/fuel/rack-prices``. */
export interface RackPriceListFilters {
  terminal_id?: string;
  /** Canonical product_code or legacy alias; server canonicalizes. */
  product_code?: string;
  branded_flag?: boolean;
  page?: number;
  size?: number;
}

// ─── Terminal Wait Summary (Req 8.4.4, 8.4.5) ────────────────────────────────

/**
 * Rolling 2-hour wait summary envelope returned by
 * ``GET /api/fuel/terminals/{terminal_id}/wait-summary``. Mirrors the
 * backend :class:`TerminalWaitSummaryResponse`. ``source`` is a
 * debugging field — clients can safely ignore it but it is surfaced so
 * the Sourcing_Recommender audit trail can pin provenance.
 */
export interface TerminalWaitSummary {
  terminal_id: string;
  tenant_id: string;
  window_minutes: number;
  avg_wait_minutes: number;
  sample_count: number;
  max_wait_minutes?: number | null;
  most_recent_report_at?: string | null;
  wait_warning_threshold_minutes: number;
  wait_warning_exceeded: boolean;
  window_start: string;
  window_end: string;
  generated_at: string;
  source: "cache" | "computed";
}

// ─── Supplier Contract Types (Req 8.3.2, 8.3.4) ──────────────────────────────

/** Active/inactive lifecycle status on supplier contracts and terminals. */
export type SupplierContractStatus = "active" | "inactive";

// ─── Terminal Types (Req 8.1.2 / cross-module-entity-linkage Req 9) ──────────

/** Terminal lifecycle status (alias of {@link SupplierContractStatus}). */
export type TerminalStatus = "active" | "inactive";

/**
 * Canonical Terminal record — matches the backend
 * :class:`fuel.terminal_models.Terminal`. The Sourcing UI uses the
 * ``terminal_id`` → ``name`` mapping to render a picker instead of a
 * free-text id box, and `<EntityLink type="terminal">` resolves the same
 * canonical record (cross-module-entity-linkage Req 9.1, 9.2, 13.1).
 */
export interface Terminal {
  terminal_id: string;
  tenant_id: string;
  name: string;
  operator: string;
  location_lat: number;
  location_lon: number;
  address: string;
  timezone: string;
  supported_products: string[];
  branded: boolean;
  supplier_brand?: string | null;
  status: TerminalStatus;
  created_at?: string | null;
  updated_at?: string | null;
}

/** Envelope returned by ``GET /api/fuel/terminals``. */
export interface TerminalListResponse {
  items: Terminal[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

/** Query filters accepted by ``GET /api/fuel/terminals``. */
export interface TerminalListFilters {
  status?: TerminalStatus;
  operator?: string;
  /** Canonical product_code or legacy alias; server canonicalizes. */
  product_code?: string;
  page?: number;
  size?: number;
}

/** Supplier_Contract persisted shape — matches backend pydantic model. */
export interface SupplierContract {
  contract_id: string;
  tenant_id: string;
  supplier_name: string;
  /** Canonical product_code (server canonicalizes legacy aliases). */
  product_code: string;
  preferred_terminal_ids: string[];
  contract_price_per_gallon_usd?: number | null;
  branded_required: boolean;
  minimum_lift_gallons_per_month?: number | null;
  rebate_terms?: string | null;
  effective_from: string; // ISO date (YYYY-MM-DD)
  effective_to?: string | null;
  status: SupplierContractStatus;
  created_at?: string | null;
  updated_at?: string | null;
}

/**
 * Monthly rolling-lift summary embedded on each contract response.
 * Mirrors the backend :class:`SupplierContractLiftSummary` — the admin
 * UI reads ``below_minimum`` to render a "below minimum" warning
 * without a second request (Req 8.3.4).
 */
export interface SupplierContractLiftSummary {
  yyyy_mm: string;
  gallons_lifted_this_month: number;
  minimum_lift_gallons_per_month?: number | null;
  percent_of_minimum?: number | null;
  below_minimum: boolean;
}

/** Single-record envelope for the Supplier_Contract CRUD endpoints. */
export interface SupplierContractResponse {
  contract: SupplierContract;
  lift_summary: SupplierContractLiftSummary;
}

/** Envelope returned by ``GET /api/fuel/supplier-contracts``. */
export interface SupplierContractListResponse {
  items: SupplierContractResponse[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

/** Query filters accepted by ``GET /api/fuel/supplier-contracts``. */
export interface SupplierContractListFilters {
  status?: SupplierContractStatus;
  supplier_name?: string;
  /** Canonical product_code or legacy alias; server canonicalizes. */
  product_code?: string;
  preferred_terminal_id?: string;
  page?: number;
  size?: number;
}

/** Body for ``POST /api/fuel/supplier-contracts``. */
export interface SupplierContractCreatePayload {
  contract_id?: string;
  supplier_name: string;
  product_code: string;
  preferred_terminal_ids?: string[];
  contract_price_per_gallon_usd?: number | null;
  branded_required?: boolean;
  minimum_lift_gallons_per_month?: number | null;
  rebate_terms?: string | null;
  effective_from: string; // ISO date YYYY-MM-DD
  effective_to?: string | null;
  status?: SupplierContractStatus;
}

/** Body for ``PATCH /api/fuel/supplier-contracts/{contract_id}``. */
export interface SupplierContractUpdatePayload {
  supplier_name?: string;
  product_code?: string;
  preferred_terminal_ids?: string[];
  contract_price_per_gallon_usd?: number | null;
  branded_required?: boolean;
  minimum_lift_gallons_per_month?: number | null;
  rebate_terms?: string | null;
  effective_from?: string;
  effective_to?: string | null;
  status?: SupplierContractStatus;
}

// ─── Sourcing / Rack / Contract Endpoints ───────────────────────────────────

/**
 * GET ``/api/fuel/sourcing/recommendations`` — rank loading terminals
 * for a (product, volume, origin, as_of) query (Task 7.10, Req 8.5.4).
 *
 * The backend persists every ranked result to the
 * ``sourcing_recommendations`` index for audit and broadcasts a
 * ``sourcing_recommendation_ready`` event on ``/ws/fuel-planning``.
 * Callers SHOULD render ``rack_price_fallback`` and
 * ``wait_warning_terminal_ids`` prominently so operators know when
 * prices are stale or wait warnings are active.
 */
export async function getSourcingRecommendations(
  params: SourcingRecommendationsQuery,
): Promise<SourcingRecommendation> {
  const qs = buildQueryString(
    params as unknown as Record<string, string | number | boolean | undefined>,
  );
  return fuelRequest<SourcingRecommendation>(
    `/fuel/sourcing/recommendations${qs}`,
  );
}

/**
 * GET ``/api/fuel/rack-prices`` — paginated latest rack prices for the
 * tenant (Task 7.5, Req 8.2.6). Reads from the ``rack_prices`` index
 * directly, so calling this endpoint never triggers an upstream provider
 * fetch.
 */
export async function listRackPrices(
  filters: RackPriceListFilters = {},
): Promise<RackPriceListResponse> {
  const qs = buildQueryString(filters);
  return fuelRequest<RackPriceListResponse>(`/fuel/rack-prices${qs}`);
}

/**
 * GET ``/api/fuel/terminals/{terminal_id}/wait-summary`` — rolling
 * 2-hour wait-time average for a terminal (Task 7.7, Req 8.4.4, 8.4.5).
 * Returns HTTP 404 with ``terminal_not_found`` when the id does not
 * exist for the tenant.
 */
export async function getTerminalWaitSummary(
  terminalId: string,
): Promise<TerminalWaitSummary> {
  return fuelRequest<TerminalWaitSummary>(
    `/fuel/terminals/${encodeURIComponent(terminalId)}/wait-summary`,
  );
}

/**
 * GET ``/api/fuel/terminals`` — paginated canonical Terminals for the
 * tenant (Req 8.1.2). Used by the Sourcing UI to back the terminal
 * picker (replacing the free-text id box) and to resolve a
 * ``terminal_id`` to its canonical display name (cross-module-entity-
 * linkage Req 9.1, 9.2, 13.1).
 */
export async function listTerminals(
  filters: TerminalListFilters = {},
): Promise<TerminalListResponse> {
  const qs = buildQueryString(filters);
  return fuelRequest<TerminalListResponse>(`/fuel/terminals${qs}`);
}

/**
 * GET ``/api/fuel/supplier-contracts`` — paginated Supplier_Contracts
 * for the tenant with embedded monthly lift summary (Task 7.6,
 * Req 8.3.2, 8.3.4).
 */
export async function listSupplierContracts(
  filters: SupplierContractListFilters = {},
): Promise<SupplierContractListResponse> {
  const qs = buildQueryString(filters);
  return fuelRequest<SupplierContractListResponse>(
    `/fuel/supplier-contracts${qs}`,
  );
}

/**
 * GET ``/api/fuel/supplier-contracts/{contract_id}`` — a single
 * contract with its current lift summary (Task 7.6, Req 8.3.2).
 * Surfaces HTTP 404 when the contract is missing or cross-tenant.
 */
export async function getSupplierContract(
  contractId: string,
): Promise<SupplierContractResponse> {
  return fuelRequest<SupplierContractResponse>(
    `/fuel/supplier-contracts/${encodeURIComponent(contractId)}`,
  );
}

/**
 * POST ``/api/fuel/supplier-contracts`` — create a new Supplier_Contract
 * (Task 7.6, Req 8.3.2). Returns HTTP 201 with the created row and its
 * initial lift summary.
 */
export async function createSupplierContract(
  payload: SupplierContractCreatePayload,
): Promise<SupplierContractResponse> {
  return fuelRequest<SupplierContractResponse>("/fuel/supplier-contracts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/**
 * PATCH ``/api/fuel/supplier-contracts/{contract_id}`` — partial update
 * (Task 7.6, Req 8.3.2). Immutable fields (``contract_id``,
 * ``tenant_id``, ``created_at``) are not accepted by the backend.
 */
export async function updateSupplierContract(
  contractId: string,
  payload: SupplierContractUpdatePayload,
): Promise<SupplierContractResponse> {
  return fuelRequest<SupplierContractResponse>(
    `/fuel/supplier-contracts/${encodeURIComponent(contractId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

/**
 * DELETE ``/api/fuel/supplier-contracts/{contract_id}`` (Task 7.6,
 * Req 8.3.2). The backend returns HTTP 204 with an empty body on
 * success, so this helper bypasses :func:`fuelRequest`'s unconditional
 * JSON decode and only validates the status code.
 */
export async function deleteSupplierContract(
  contractId: string,
): Promise<void> {
  const url = `${API_BASE_URL}/fuel/supplier-contracts/${encodeURIComponent(contractId)}`;
  let response: Response;
  try {
    response = await fetchWithTimeout(url, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    if (error instanceof ApiTimeoutError || error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      body.detail || body.message || `HTTP error! status: ${response.status}`,
      response.status,
    );
  }
}

// ─── Truck Compartments + Cleaning Events (Fuel Ops Hardening Req 7.1.1–7.1.4) ─

/**
 * Lifecycle state of a truck compartment.
 *
 *  * ``clean`` — empty and safe to load any allowed grade.
 *  * ``loaded`` — currently holds ``last_loaded_product``; the
 *    Compartment_Loading_Agent has committed an assignment but the
 *    delivery may still be pending.
 *  * ``needs_cleaning`` — the previous load triggered a
 *    ``requires_cleaning`` compatibility rule. The compartment is
 *    blocked until a Cleaning_Event is recorded.
 *
 * Mirrors the backend ``CompartmentLifecycleState`` literal.
 */
export type CompartmentLifecycleState = "clean" | "loaded" | "needs_cleaning";

/** Cleaning regimes mandated by Requirement 7.1.4. */
export type CleaningMethod = "flush" | "purge" | "sanitize";

/**
 * One row returned by
 * ``GET /api/fuel/mvp/trucks/{truck_id}/compartments``.
 *
 * Combines the static compartment configuration (capacity, ``allowed_grades``,
 * ``position_index``) with the lifecycle state
 * (``state``, ``last_loaded_product``, ``last_loaded_at``,
 * ``last_cleaned_at``). Timestamps are ISO-8601 UTC strings.
 */
export interface TruckCompartmentState {
  compartment_id: string;
  truck_id: string;
  capacity_gallons?: number;
  capacity_liters: number;
  allowed_grades: string[];
  position_index: number;
  state: CompartmentLifecycleState;
  last_loaded_product?: string | null;
  last_loaded_at?: string | null;
  last_cleaned_at?: string | null;
}

export function getTruckCompartmentCapacityGallons(
  compartment:
    | Pick<TruckCompartmentState, "capacity_gallons" | "capacity_liters">
    | null
    | undefined,
): number {
  if (!compartment) return 0;
  return (
    compartment.capacity_gallons ?? litersToGallons(compartment.capacity_liters)
  );
}

/** Envelope for ``GET /api/fuel/mvp/trucks/{truck_id}/compartments``. */
export interface TruckCompartmentListResponse {
  truck_id: string;
  items: TruckCompartmentState[];
  total: number;
}

/** One truck that has at least one compartment configured. */
export interface CompartmentTruckSummary {
  truck_id: string;
  compartment_count: number;
}

/** Envelope for ``GET /api/fuel/mvp/compartment-trucks``. */
export interface CompartmentTrucksResponse {
  items: CompartmentTruckSummary[];
  total: number;
}

/**
 * Body for ``POST /api/fuel/mvp/compartments/{id}/cleaning-events``.
 *
 * ``evidence_refs`` are ``file_ref``s returned by
 * {@link presignPodUpload} — each must belong to the caller's tenant or
 * the backend returns HTTP 403 ``cross_tenant_file_ref``.
 */
export interface CleaningEventCreateRequest {
  method: CleaningMethod;
  actor_id: string;
  /**
   * Canonical, resolvable driver reference for the actor that performed the
   * cleaning (cross-module-entity-linkage Req 8.2). Supersedes the free-text
   * ``actor_id`` alias. Optional/nullable; when supplied the backend validates
   * it against the Drivers module and rejects a non-existent driver with
   * HTTP 400 ``driver_not_found``.
   */
  driver_id?: string;
  notes?: string;
  evidence_refs?: string[];
}

/**
 * Response body for ``POST /api/fuel/mvp/compartments/{id}/cleaning-events``.
 *
 * Mirrors the backend :class:`CleaningEvent` model. Server-computed
 * fields (``cleaning_event_id``, ``tenant_id``, ``truck_id``,
 * ``cleaned_at``, ``created_at``, ``updated_at``) are always returned
 * so the UI can confirm the write and refresh the compartment row
 * without a second list fetch.
 */
export interface CleaningEvent {
  cleaning_event_id: string;
  tenant_id: string;
  compartment_id: string;
  truck_id: string;
  method: CleaningMethod;
  actor_id: string;
  /**
   * Canonical, resolvable driver reference (cross-module-entity-linkage
   * Req 8.2). Nullable for legacy events that only carried ``actor_id``.
   */
  driver_id?: string | null;
  notes?: string | null;
  evidence_refs: string[];
  cleaned_at: string;
  created_at: string;
  updated_at: string;
}

/**
 * GET ``/api/fuel/mvp/trucks/{truck_id}/compartments`` — return the
 * compartment roster + lifecycle state for a truck (Task 11.9 /
 * Req 7.1.1–7.1.3).
 *
 * The backend sorts by ``position_index`` so the client can render
 * compartments in physical loading order without a secondary sort. An
 * empty result (``items: []``) is returned for unknown or
 * unconfigured trucks rather than HTTP 404 so the UI can show a
 * "configure compartments" empty state.
 */
export async function listTruckCompartments(
  truckId: string,
): Promise<TruckCompartmentListResponse> {
  return fuelRequest<TruckCompartmentListResponse>(
    `/fuel/mvp/trucks/${encodeURIComponent(truckId)}/compartments`,
  );
}

/**
 * GET ``/api/fuel/mvp/compartment-trucks`` — list trucks that have at
 * least one compartment configured, with a per-truck compartment count.
 *
 * Powers the Truck Compartments tab's truck picker so dispatchers can
 * select a tanker from a dropdown instead of having to know its id
 * up-front. An empty result returns ``items: []`` rather than 404.
 */
export async function listCompartmentTrucks(): Promise<CompartmentTrucksResponse> {
  return fuelRequest<CompartmentTrucksResponse>("/fuel/mvp/compartment-trucks");
}

/**
 * POST ``/api/fuel/mvp/compartments/{id}/cleaning-events`` — record a
 * Cleaning_Event and reset the compartment's lifecycle state to
 * ``clean`` (Task 6.3 / Req 7.1.4).
 *
 * The call is strictly tenant-scoped via the JWT context. Callers
 * upload any evidence photos via {@link presignPodUpload} +
 * {@link putPresignedFile} first and pass the resulting
 * ``file_ref``s in ``evidence_refs``.
 *
 * Error modes surfaced as :class:`ApiError` ``status``:
 *   * 403 — one of ``evidence_refs`` belongs to another tenant, or
 *     the compartment is owned by another tenant.
 *   * 404 — compartment not found.
 *   * 409 — optimistic concurrency conflict during state reset; retry.
 *   * 422 — method outside ``{flush, purge, sanitize}`` or other
 *     body validation failure.
 */
export async function recordCleaningEvent(
  compartmentId: string,
  body: CleaningEventCreateRequest,
): Promise<CleaningEvent> {
  return fuelRequest<CleaningEvent>(
    `/fuel/mvp/compartments/${encodeURIComponent(compartmentId)}/cleaning-events`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

// ─── Fuel Product Catalog + Delivery Destinations (Task 2.5, Req 6.1.3, 6.2.4) ─

/**
 * One row in ``GET /api/fuel/products``. Mirrors the backend
 * :class:`FuelProductItem` Pydantic response 1:1 so the admin UI can
 * render the catalog without post-processing. ``aliases`` includes alternate
 * supplier and terminal codes that resolve to ``product_code`` server-side via
 * the fuel product catalog canonicalizer.
 */
export interface FuelProductItem {
  product_code: string;
  display_name: string;
  category: string;
  density_lbs_per_gallon: number;
  tax_class: string;
  aliases: string[];
  region_availability: string[];
}

/** Envelope returned by ``GET /api/fuel/products``. */
export interface FuelProductsResponse {
  /** Tenant region echoed from the JWT context. */
  region: string;
  items: FuelProductItem[];
  total: number;
}

/**
 * Discriminator flag for every :class:`DeliveryDestination`. Mirrors
 * :data:`fuel.services.delivery_destination_service.DestinationType`.
 */
export type DeliveryDestinationType = "retail_station" | "customer_tank";

/** Coordinate pair surfaced on every :class:`DeliveryDestination`. */
export interface DeliveryDestinationLocation {
  lat: number;
  lon: number;
}

/**
 * Unified Delivery_Destination record returned by
 * ``GET /api/fuel/destinations``. All volumes are normalized to US
 * gallons regardless of which source index backed the record so the
 * UI never has to unit-convert (Req 6.2.1).
 */
export interface DeliveryDestination {
  destination_id: string;
  destination_type: DeliveryDestinationType;
  tenant_id: string;
  customer_id?: string | null;
  name: string;
  location?: DeliveryDestinationLocation | null;
  address?: string | null;
  zip_code?: string | null;
  fuel_products: string[];
  capacity_gallons?: number | null;
  current_level_gallons?: number | null;
  status?: string | null;
  updated_at?: string | null;
  created_at?: string | null;
  raw?: Record<string, unknown> | null;
}

/** Envelope returned by ``GET /api/fuel/destinations``. */
export interface DeliveryDestinationsResponse {
  items: DeliveryDestination[];
  total: number;
}

/** Query filters accepted by ``GET /api/fuel/destinations``. */
export interface DeliveryDestinationFilters {
  destination_type?: DeliveryDestinationType;
  /** Canonical product_code or legacy alias; server canonicalizes. */
  fuel_product?: string;
  zip_code?: string;
}

/**
 * GET ``/api/fuel/products`` — tenant-scoped catalog of fuel products
 * filtered to the tenant's Region (Task 2.5, Req 6.1.3).
 *
 * The backend stamps the Region from the JWT context so the caller
 * never supplies it. An empty ``items`` list is returned when no
 * catalog rows match the Region rather than HTTP 404 — the admin UI
 * surfaces that as a "no products configured" setup task.
 */
export async function listFuelProducts(): Promise<FuelProductsResponse> {
  return fuelRequest<FuelProductsResponse>("/fuel/products");
}

/**
 * GET ``/api/fuel/destinations`` — unified list of retail stations and
 * customer tanks for the tenant (Task 2.5, Req 6.2.4).
 *
 * Optional filters narrow by destination type, fuel product (canonical
 * or legacy alias), or zip code. The server re-validates every row's
 * ``tenant_id`` against the caller's context so cross-tenant rows can
 * never leak through a mis-labelled source document.
 */
export async function listDeliveryDestinations(
  filters: DeliveryDestinationFilters = {},
): Promise<DeliveryDestinationsResponse> {
  const qs = buildQueryString(filters);
  return fuelRequest<DeliveryDestinationsResponse>(`/fuel/destinations${qs}`);
}

// ─── Compartment load-eligibility (Task 6.7, Req 7.2.5) ──────────────────────

/**
 * Narrow compartment-state view echoed back on every load-eligibility
 * decision so dispatchers can see *why* the answer is what it is.
 * Timestamps are ISO-8601 strings (``null`` when absent).
 */
export interface LoadEligibilityCompartmentState {
  compartment_id: string;
  truck_id: string;
  state: CompartmentLifecycleState;
  last_loaded_product?: string | null;
  last_loaded_at?: string | null;
  last_cleaned_at?: string | null;
}

/** Compatibility decision surfaced by the load-eligibility endpoint. */
export type LoadEligibilityDecision =
  | "allowed"
  | "blocked"
  | "requires_cleaning";

/**
 * Response envelope for
 * ``GET /api/fuel/mvp/compartments/{id}/load-eligibility``.
 *
 * ``governing_rule`` exposes the matrix cell that drove the decision
 * (``allowed`` | ``blocked`` | ``requires_cleaning``). When the
 * decision is ``allowed`` but the governing rule is
 * ``requires_cleaning``, the compartment was freshly cleaned and the
 * rule downgraded per Req 7.2.4.
 */
export interface LoadEligibilityResponse {
  compartment_id: string;
  proposed_product: string;
  previous_product?: string | null;
  decision: LoadEligibilityDecision;
  /** ``cross_contamination_blocked`` on ``blocked``; ``cleaning_required`` on requires_cleaning; null on allowed. */
  reason?: string | null;
  governing_rule: LoadEligibilityDecision;
  compartment_state: LoadEligibilityCompartmentState;
}

/**
 * GET ``/api/fuel/mvp/compartments/{compartment_id}/load-eligibility``
 * — preview the Compartment_Loading_Agent's compatibility decision
 * for a proposed product without committing a loading plan (Task 6.7,
 * Req 7.2.5).
 *
 * The ``product_code`` accepts canonical codes and legacy aliases;
 * the backend canonicalizes before consulting the matrix.
 */
export async function checkCompartmentLoadEligibility(
  compartmentId: string,
  productCode: string,
): Promise<LoadEligibilityResponse> {
  const qs = buildQueryString({ product_code: productCode });
  return fuelRequest<LoadEligibilityResponse>(
    `/fuel/mvp/compartments/${encodeURIComponent(compartmentId)}/load-eligibility${qs}`,
  );
}

// ─── Priority clusters (Task 5.4, Req 3.4.3) ─────────────────────────────────

/** WGS84 centroid for a priority cluster. */
export interface PriorityClusterCentroid {
  lat: number;
  lon: number;
}

/** One dense cluster row returned by ``GET /api/fuel/mvp/priority-clusters``. */
export interface PriorityClusterItem {
  cluster_id: string;
  centroid: PriorityClusterCentroid;
  member_count: number;
  highest_priority_bucket?: "critical" | "high" | "medium" | "low" | null;
  fuel_grades: string[];
}

/** Envelope returned by ``GET /api/fuel/mvp/priority-clusters``. */
export interface PriorityClustersResponse {
  run_id?: string | null;
  eps_miles: number;
  min_samples: number;
  items: PriorityClusterItem[];
  total: number;
}

/** Query parameters for ``GET /api/fuel/mvp/priority-clusters``. */
export interface PriorityClustersQuery {
  eps_miles?: number;
  min_samples?: number;
}

/**
 * GET ``/api/fuel/mvp/priority-clusters`` — DBSCAN-computed clusters
 * over the tenant's latest priority run (Task 5.4, Req 3.4.3,
 * 3.4.4).
 *
 * Noise points (clusters smaller than ``min_samples``) are omitted
 * from the response per Req 3.4.3's "one row per cluster" wording.
 */
export async function listPriorityClusters(
  query: PriorityClustersQuery = {},
): Promise<PriorityClustersResponse> {
  const qs = buildQueryString(query);
  return fuelRequest<PriorityClustersResponse>(
    `/fuel/mvp/priority-clusters${qs}`,
  );
}

// ─── Combinable groups (Task 5.6, Req 3.2.4) ─────────────────────────────────

/** One member inside a :class:`CombinableGroup`. */
export interface CombinableGroupMember {
  destination_type: "station" | "customer_tank";
  destination_id: string;
  station_id?: string | null;
  customer_tank_id?: string | null;
  fuel_grade: string;
  product_code: string;
  estimated_gallons: number;
  location: { lat: number; lon: number };
}

/**
 * Combinable_Group persisted shape — one connected component of the
 * pairwise-combinable graph computed by the Delivery_Prioritization_Agent.
 * Mirrors :class:`fuel.combinable_group_models.CombinableGroup`.
 */
export interface CombinableGroup {
  group_id: string;
  tenant_id: string;
  run_id: string;
  members: CombinableGroupMember[];
  fuel_grades: string[];
  estimated_combined_gallons: number;
  centroid: { lat: number; lon: number };
  generated_at: string;
  updated_at?: string | null;
  created_at?: string | null;
}

/** Envelope returned by ``GET /api/fuel/mvp/combinable-groups``. */
export interface CombinableGroupListResponse {
  items: CombinableGroup[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

/** Query filters accepted by ``GET /api/fuel/mvp/combinable-groups``. */
export interface CombinableGroupListFilters {
  run_id?: string;
  /** Canonical product_code or legacy alias; server canonicalizes. */
  fuel_grade?: string;
  min_members?: number;
  page?: number;
  size?: number;
}

/**
 * GET ``/api/fuel/mvp/combinable-groups`` — paginated combinable
 * groups for the tenant filtered by run / fuel grade / minimum size
 * (Task 5.6, Req 3.2.4).
 */
export async function listCombinableGroups(
  filters: CombinableGroupListFilters = {},
): Promise<CombinableGroupListResponse> {
  const qs = buildQueryString(filters);
  return fuelRequest<CombinableGroupListResponse>(
    `/fuel/mvp/combinable-groups${qs}`,
  );
}

// ─── POD hash-proof + hash-chain verify (Task 8.11, Req 4.5.3, 4.5.4) ────────

/**
 * Response envelope for ``GET /api/fuel/pod/{pod_id}/hash-proof``.
 *
 * Auditors can re-serialize ``canonical_payload`` with
 * ``JSON.stringify(payload)`` (after sorting keys) and verify
 * ``sha256(...) === pod_hash`` locally — no additional normalization
 * is required. ``canonical_payload_bytes`` is the exact UTF-8 JSON
 * string the backend hashed for callers that prefer byte-level
 * verification.
 */
export interface HashProofResponse {
  pod_id: string;
  tenant_id: string;
  pod_hash: string;
  previous_pod_hash: string;
  canonical_payload: Record<string, unknown>;
  canonical_payload_bytes: string;
}

/** Selector for the hash-chain verify endpoint. Exactly one mode may be supplied. */
export interface HashChainVerifyRequest {
  /** Explicit ordered list of pod_ids to verify (oldest first). */
  pod_ids?: string[];
  /** Inclusive start of the range to verify. Paired with ``to_pod_id``. */
  from_pod_id?: string;
  /** Inclusive end of the range to verify. */
  to_pod_id?: string;
  /** Maximum number of PODs to walk in range mode (1–500, default 100). */
  limit?: number;
}

/** One mismatch entry in a hash-chain verify response. */
export interface HashChainMismatch {
  pod_id: string;
  reason:
    | "pod_not_found"
    | "missing_stored_hash"
    | "stored_hash_mismatch"
    | "previous_hash_mismatch";
  expected_hash?: string | null;
  stored_hash?: string | null;
  computed_hash?: string | null;
  message: string;
}

/**
 * Response envelope for ``POST /api/fuel/pod/hash-chain/verify``.
 * ``valid`` is ``true`` iff every POD in the window matches its
 * stored hash *and* chain linkage; otherwise ``first_mismatch``
 * carries the first failure encountered (Req 4.5.5).
 */
export interface HashChainVerifyResponse {
  tenant_id: string;
  verified_count: number;
  total_requested: number;
  valid: boolean;
  first_mismatch?: HashChainMismatch | null;
  pod_ids_checked: string[];
}

/**
 * GET ``/api/fuel/pod/{pod_id}/hash-proof`` — return the canonical
 * POD payload + computed hashes so auditors can verify tamper-evidence
 * offline (Task 8.11, Req 4.5.3).
 */
export async function getPodHashProof(
  podId: string,
): Promise<HashProofResponse> {
  return fuelRequest<HashProofResponse>(
    `/fuel/pod/${encodeURIComponent(podId)}/hash-proof`,
  );
}

/**
 * POST ``/api/fuel/pod/hash-chain/verify`` — recompute and verify
 * POD hashes for a list or range of pod_ids; report the first
 * mismatch when the chain is broken (Task 8.11, Req 4.5.4, 4.5.5).
 *
 * Supply *either* ``pod_ids`` (explicit list) *or* ``from_pod_id`` +
 * ``to_pod_id`` (inclusive range); mixing the two returns HTTP 400.
 */
export async function verifyPodHashChain(
  body: HashChainVerifyRequest,
): Promise<HashChainVerifyResponse> {
  return fuelRequest<HashChainVerifyResponse>("/fuel/pod/hash-chain/verify", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ─── Terminal wait-reports (Task 7.7, Req 8.4.2) ─────────────────────────────

/** Source field on a :class:`TerminalWaitReport`. */
export type TerminalWaitReportSource =
  | "driver_report"
  | "eld_geofence"
  | "connector_import";

/**
 * Persisted Terminal_Wait_Report row. Mirrors
 * :class:`fuel.terminal_models.TerminalWaitReport`. The server mints
 * ``report_id`` / ``tenant_id`` / ``retrieved_at`` so the client
 * only supplies the observation itself.
 */
export interface TerminalWaitReport {
  report_id: string;
  tenant_id: string;
  terminal_id: string;
  wait_minutes: number;
  source: TerminalWaitReportSource;
  reporter_id?: string | null;
  truck_id?: string | null;
  observed_at: string;
  retrieved_at: string;
  updated_at?: string | null;
  created_at?: string | null;
}

/** Request body for ``POST /api/fuel/terminals/{terminal_id}/wait-reports``. */
export interface TerminalWaitReportCreateRequest {
  wait_minutes: number;
  source: TerminalWaitReportSource;
  /** Required when ``source`` is ``driver_report``. */
  reporter_id?: string;
  truck_id?: string;
  observed_at: string;
  /**
   * Optional free-form dispatcher / driver note. Capped at 1000 chars by
   * the backend model; whitespace-only values are coerced to ``None``
   * server-side (see :class:`fuel.terminal_models.TerminalWaitReport`).
   */
  notes?: string;
}

/**
 * POST ``/api/fuel/terminals/{terminal_id}/wait-reports`` — submit a
 * wait-time observation. Used by the driver app's self-report button
 * and by the Geotab geofence importer (Task 7.7, 7.8, Req 8.4.2).
 *
 * Returns HTTP 201 with the persisted row (including the
 * server-minted ``report_id``). Returns HTTP 422 when
 * ``source === "driver_report"`` but ``reporter_id`` is missing.
 */
export async function submitTerminalWaitReport(
  terminalId: string,
  body: TerminalWaitReportCreateRequest,
): Promise<TerminalWaitReport> {
  return fuelRequest<TerminalWaitReport>(
    `/fuel/terminals/${encodeURIComponent(terminalId)}/wait-reports`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

// ─── Storm-mode road restrictions (Task 10.8, Req 9.3.3, 9.3.5) ──────────────

/**
 * Persisted StormRoadRestriction row. Mirrors
 * :class:`fuel.storm_mode_models.StormRoadRestriction`. ``polygon`` is
 * a GeoJSON ``Polygon`` or ``MultiPolygon`` object with WGS84
 * ``[lon, lat]`` coordinates — surfaced verbatim so the dispatcher
 * map can hand it to Mapbox / Leaflet without reshaping.
 */
export interface StormRoadRestriction {
  restriction_id: string;
  tenant_id: string;
  polygon: Record<string, unknown>;
  effective_from: string;
  effective_to?: string | null;
  source: string;
  severity: WeatherAlertSeverity;
  reason?: string | null;
  updated_at?: string | null;
  created_at?: string | null;
}

/** Request body for ``POST /api/fuel/storm-mode/road-restrictions``. */
export interface StormRoadRestrictionCreateRequest {
  /** GeoJSON ``Polygon`` or ``MultiPolygon`` geometry (WGS84 ``[lon, lat]``). */
  polygon: Record<string, unknown>;
  effective_from: string;
  effective_to?: string | null;
  source: string;
  severity: WeatherAlertSeverity;
  reason?: string | null;
}

/** Envelope returned by ``GET /api/fuel/storm-mode/road-restrictions``. */
export interface StormRoadRestrictionListResponse {
  items: StormRoadRestriction[];
  total: number;
}

/**
 * POST ``/api/fuel/storm-mode/road-restrictions`` — upload a
 * dispatcher-authored road-closure polygon (Task 10.8, Req 9.3.3).
 * Dispatchers and admins only; other roles receive HTTP 403
 * ``forbidden_role``.
 */
export async function uploadStormRoadRestriction(
  body: StormRoadRestrictionCreateRequest,
): Promise<StormRoadRestriction> {
  return fuelRequest<StormRoadRestriction>(
    "/fuel/storm-mode/road-restrictions",
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/**
 * GET ``/api/fuel/storm-mode/road-restrictions`` — the tenant's
 * currently active road restrictions for the map overlay
 * (Task 10.8, Req 9.3.5). The server caps the response size so
 * runaway polygon counts don't blow up the dispatcher UI.
 */
export async function listStormRoadRestrictions(): Promise<StormRoadRestrictionListResponse> {
  return fuelRequest<StormRoadRestrictionListResponse>(
    "/fuel/storm-mode/road-restrictions",
  );
}
