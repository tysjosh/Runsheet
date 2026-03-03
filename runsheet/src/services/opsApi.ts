import { API_TIMEOUTS, ApiError, ApiTimeoutError } from "./api";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Shared Types ────────────────────────────────────────────────────────────

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

export interface PaginationParams {
  page?: number;
  size?: number;
}

export interface SortParams {
  sort_by?: string;
  sort_order?: "asc" | "desc";
}

export interface DateRangeParams {
  start_date?: string;
  end_date?: string;
}

// ─── Shipment Types ──────────────────────────────────────────────────────────

export interface OpsShipment {
  shipment_id: string;
  status: ShipmentStatus;
  tenant_id: string;
  rider_id?: string;
  origin?: string;
  destination?: string;
  created_at?: string;
  updated_at?: string;
  estimated_delivery?: string;
  current_location?: GeoPoint;
  failure_reason?: string;
  last_event_timestamp?: string;
  source_schema_version?: string;
  trace_id?: string;
  ingested_at?: string;
}

export type ShipmentStatus =
  | "pending"
  | "in_transit"
  | "delivered"
  | "failed"
  | "returned";

export interface ShipmentDetail extends OpsShipment {
  events?: OpsEvent[];
}

// ─── Rider Types ─────────────────────────────────────────────────────────────

export interface OpsRider {
  rider_id: string;
  rider_name?: string;
  status: RiderStatus;
  tenant_id: string;
  availability?: string;
  last_seen?: string;
  current_location?: GeoPoint;
  active_shipment_count: number;
  completed_today: number;
  last_event_timestamp?: string;
  source_schema_version?: string;
  trace_id?: string;
  ingested_at?: string;
}

export type RiderStatus = "active" | "idle" | "offline";

export interface RiderDetail extends OpsRider {
  assigned_shipments?: OpsShipment[];
}

export interface RiderUtilization extends OpsRider {
  utilization_percentage?: number;
  idle_minutes?: number;
}

// ─── Event Types ─────────────────────────────────────────────────────────────

export interface OpsEvent {
  event_id: string;
  shipment_id: string;
  event_type: string;
  tenant_id: string;
  event_timestamp: string;
  event_payload?: Record<string, unknown>;
  location?: GeoPoint;
  source_schema_version?: string;
  trace_id?: string;
  ingested_at?: string;
}

// ─── Metrics Types ───────────────────────────────────────────────────────────

export type MetricsBucket = "hourly" | "daily";

export interface MetricsBucketEntry {
  timestamp: string;
  count: number;
  breakdown?: Record<string, number>;
}

export interface MetricsResponse {
  data: MetricsBucketEntry[];
  bucket: MetricsBucket;
  start_date: string;
  end_date: string;
  request_id: string;
}

// ─── Monitoring Types ────────────────────────────────────────────────────────

export interface IngestionMetrics {
  events_received: number;
  events_processed: number;
  events_failed: number;
  avg_latency_ms: number;
  request_id: string;
}

export interface IndexingMetrics {
  documents_indexed: number;
  indexing_errors: number;
  bulk_success_rate: number;
  avg_latency_ms: number;
  request_id: string;
}

export interface PoisonQueueMetrics {
  queue_depth: number;
  oldest_event_age_seconds: number;
  pending_count: number;
  permanently_failed_count: number;
  request_id: string;
}

// ─── Common Types ────────────────────────────────────────────────────────────

export interface GeoPoint {
  lat: number;
  lon: number;
}

// ─── Filter Types ────────────────────────────────────────────────────────────

export interface ShipmentFilters
  extends PaginationParams,
    SortParams,
    DateRangeParams {
  status?: ShipmentStatus;
  rider_id?: string;
}

export interface SlaBreachFilters extends PaginationParams {
  status?: ShipmentStatus;
  rider_id?: string;
}

export interface FailureFilters extends PaginationParams, DateRangeParams {
  rider_id?: string;
}

export interface RiderFilters extends PaginationParams {
  status?: RiderStatus;
}

export interface RiderUtilizationFilters extends PaginationParams {
  status?: RiderStatus;
}

export interface EventFilters extends PaginationParams, DateRangeParams {
  shipment_id?: string;
  event_type?: string;
}

export interface MetricsFilters extends DateRangeParams {
  bucket?: MetricsBucket;
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

async function opsRequest<T>(
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

// ─── Shipment Endpoints ──────────────────────────────────────────────────────

/** GET /ops/shipments — paginated list with filters and sorting */
export async function getShipments(
  filters: ShipmentFilters = {},
): Promise<PaginatedResponse<OpsShipment>> {
  const qs = buildQueryString(filters);
  return opsRequest<PaginatedResponse<OpsShipment>>(`/ops/shipments${qs}`);
}

/** GET /ops/shipments/:id — single shipment with full event history */
export async function getShipmentById(
  shipmentId: string,
): Promise<{ data: ShipmentDetail; request_id: string }> {
  return opsRequest<{ data: ShipmentDetail; request_id: string }>(
    `/ops/shipments/${encodeURIComponent(shipmentId)}`,
  );
}

/** GET /ops/shipments/sla-breaches — shipments past estimated delivery */
export async function getSlaBreaches(
  filters: SlaBreachFilters = {},
): Promise<PaginatedResponse<OpsShipment>> {
  const qs = buildQueryString(filters);
  return opsRequest<PaginatedResponse<OpsShipment>>(
    `/ops/shipments/sla-breaches${qs}`,
  );
}

/** GET /ops/shipments/failures — failed shipments with failure reason */
export async function getShipmentFailures(
  filters: FailureFilters = {},
): Promise<PaginatedResponse<OpsShipment>> {
  const qs = buildQueryString(filters);
  return opsRequest<PaginatedResponse<OpsShipment>>(
    `/ops/shipments/failures${qs}`,
  );
}

// ─── Rider Endpoints ─────────────────────────────────────────────────────────

/** GET /ops/riders — paginated rider list */
export async function getRiders(
  filters: RiderFilters = {},
): Promise<PaginatedResponse<OpsRider>> {
  const qs = buildQueryString(filters);
  return opsRequest<PaginatedResponse<OpsRider>>(`/ops/riders${qs}`);
}

/** GET /ops/riders/:id — single rider with assigned shipments */
export async function getRiderById(
  riderId: string,
): Promise<{ data: RiderDetail; request_id: string }> {
  return opsRequest<{ data: RiderDetail; request_id: string }>(
    `/ops/riders/${encodeURIComponent(riderId)}`,
  );
}

/** GET /ops/riders/utilization — riders with utilization metrics */
export async function getRiderUtilization(
  filters: RiderUtilizationFilters = {},
): Promise<PaginatedResponse<RiderUtilization>> {
  const qs = buildQueryString(filters);
  return opsRequest<PaginatedResponse<RiderUtilization>>(
    `/ops/riders/utilization${qs}`,
  );
}

// ─── Event Endpoints ─────────────────────────────────────────────────────────

/** GET /ops/events — paginated event list with filters */
export async function getEvents(
  filters: EventFilters = {},
): Promise<PaginatedResponse<OpsEvent>> {
  const qs = buildQueryString(filters);
  return opsRequest<PaginatedResponse<OpsEvent>>(`/ops/events${qs}`);
}

// ─── Metrics Endpoints ───────────────────────────────────────────────────────

/** GET /ops/metrics/shipments — shipment counts by status in time buckets */
export async function getShipmentMetrics(
  filters: MetricsFilters = {},
): Promise<MetricsResponse> {
  const qs = buildQueryString(filters);
  return opsRequest<MetricsResponse>(`/ops/metrics/shipments${qs}`);
}

/** GET /ops/metrics/sla — SLA compliance and breach counts */
export async function getSlaMetrics(
  filters: MetricsFilters = {},
): Promise<MetricsResponse> {
  const qs = buildQueryString(filters);
  return opsRequest<MetricsResponse>(`/ops/metrics/sla${qs}`);
}

/** GET /ops/metrics/riders — rider utilization and availability metrics */
export async function getRiderMetrics(
  filters: MetricsFilters = {},
): Promise<MetricsResponse> {
  const qs = buildQueryString(filters);
  return opsRequest<MetricsResponse>(`/ops/metrics/riders${qs}`);
}

/** GET /ops/metrics/failures — failure counts by reason */
export async function getFailureMetrics(
  filters: MetricsFilters = {},
): Promise<MetricsResponse> {
  const qs = buildQueryString(filters);
  return opsRequest<MetricsResponse>(`/ops/metrics/failures${qs}`);
}

// ─── Monitoring Endpoints ────────────────────────────────────────────────────

/** GET /ops/monitoring/ingestion — ingestion pipeline health */
export async function getIngestionMonitoring(): Promise<IngestionMetrics> {
  return opsRequest<IngestionMetrics>("/ops/monitoring/ingestion");
}

/** GET /ops/monitoring/indexing — ES indexing health */
export async function getIndexingMonitoring(): Promise<IndexingMetrics> {
  return opsRequest<IndexingMetrics>("/ops/monitoring/indexing");
}

/** GET /ops/monitoring/poison-queue — poison queue stats */
export async function getPoisonQueueMonitoring(): Promise<PoisonQueueMetrics> {
  return opsRequest<PoisonQueueMetrics>("/ops/monitoring/poison-queue");
}
