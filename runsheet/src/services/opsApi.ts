import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import {
  buildQueryString,
  fetchWithTimeout,
  type PaginatedResponse,
} from "./utils";

// Re-export the shared pagination/response envelope types so existing
// downstream imports keep resolving them from this module (Req 2.4/4.3).
export type { PaginatedResponse, PaginationMeta } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Shared Types ────────────────────────────────────────────────────────────

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
  /** Backend returns `values` dict with `total` key and per-status counts */
  values?: Record<string, number>;
  /** Derived total count (may be populated from values.total) */
  count?: number;
  /** Derived per-status breakdown (may be populated from values minus total) */
  breakdown?: Record<string, number>;
}

export interface MetricsResponse {
  data: MetricsBucketEntry[];
  bucket: MetricsBucket;
  start_date: string;
  end_date: string;
  request_id: string;
}

// ─── SLA Types ────────────────────────────────────────────────────────────────

export interface SlaMetric {
  category: string;
  total_shipments: number;
  on_time: number;
  breached: number;
  compliance_rate: number;
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
  failure_reason?: string;
}

export interface RiderUtilizationFilters extends PaginationParams {
  status?: RiderStatus;
}

export interface MetricsFilters extends DateRangeParams {
  bucket?: MetricsBucket;
  failure_reason?: string;
}

// ─── HTTP Helper ─────────────────────────────────────────────────────────────

async function opsRequest<T>(
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
      const detail = body.detail;
      const message =
        typeof detail === "string"
          ? detail
          : detail?.message ||
            body.message ||
            `HTTP error! status: ${response.status}`;
      throw new ApiError(message, response.status);
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

/** GET /ops/riders/utilization — riders with utilization metrics */
export async function getRiderUtilization(
  filters: RiderUtilizationFilters = {},
): Promise<PaginatedResponse<RiderUtilization>> {
  const qs = buildQueryString(filters);
  return opsRequest<PaginatedResponse<RiderUtilization>>(
    `/ops/riders/utilization${qs}`,
  );
}

// ─── Metrics Endpoints ───────────────────────────────────────────────────────

/** GET /ops/metrics/failures — failure counts by reason */
export async function getFailureMetrics(
  filters: MetricsFilters = {},
): Promise<MetricsResponse> {
  const qs = buildQueryString(filters);
  return opsRequest<MetricsResponse>(`/ops/metrics/failures${qs}`);
}

/** GET /ops/metrics/shipments — shipment volume by status in time buckets */
export async function getShipmentMetrics(
  filters: MetricsFilters = {},
): Promise<MetricsResponse> {
  const qs = buildQueryString(filters);
  return opsRequest<MetricsResponse>(`/ops/metrics/shipments${qs}`);
}

/** GET /ops/metrics/sla — SLA compliance metrics (time-bucketed like shipment metrics) */
export async function getSlaMetrics(
  filters: MetricsFilters = {},
): Promise<MetricsResponse> {
  const qs = buildQueryString(filters);
  return opsRequest<MetricsResponse>(`/ops/metrics/sla${qs}`);
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

// ─── Rider List / Detail Endpoints ───────────────────────────────────────────

export interface RiderListFilters extends PaginationParams {
  status?: RiderStatus;
}

/** A rider with the shipments currently assigned to them. */
export interface RiderDetail extends OpsRider {
  assigned_shipments?: OpsShipment[];
}

/** GET /ops/riders — paginated rider records from riders_current */
export async function getRiders(
  filters: RiderListFilters = {},
): Promise<PaginatedResponse<OpsRider>> {
  const qs = buildQueryString(filters);
  return opsRequest<PaginatedResponse<OpsRider>>(`/ops/riders${qs}`);
}

/** GET /ops/riders/:id — single rider with assigned shipment details */
export async function getRiderById(
  riderId: string,
): Promise<{ data: RiderDetail; request_id: string }> {
  return opsRequest<{ data: RiderDetail; request_id: string }>(
    `/ops/riders/${encodeURIComponent(riderId)}`,
  );
}

// ─── Event List Endpoint ──────────────────────────────────────────────────────

export interface EventFilters extends PaginationParams, DateRangeParams {
  shipment_id?: string;
  event_type?: string;
}

/** GET /ops/events — paginated events from shipment_events */
export async function getEvents(
  filters: EventFilters = {},
): Promise<PaginatedResponse<OpsEvent>> {
  const qs = buildQueryString(filters);
  return opsRequest<PaginatedResponse<OpsEvent>>(`/ops/events${qs}`);
}

// ─── Rider Metrics Endpoint ───────────────────────────────────────────────────

/** GET /ops/metrics/riders — rider utilization/availability in time buckets */
export async function getRiderMetrics(
  filters: MetricsFilters = {},
): Promise<MetricsResponse> {
  const qs = buildQueryString(filters);
  return opsRequest<MetricsResponse>(`/ops/metrics/riders${qs}`);
}

// ─── Prometheus Metrics Endpoint ──────────────────────────────────────────────

/**
 * GET /ops/metrics/prometheus — Prometheus text-exposition metrics.
 *
 * Unlike the other ops endpoints this returns ``text/plain`` rather than
 * JSON, so it bypasses the shared ``opsRequest`` helper and returns the raw
 * exposition string for a dashboard "raw metrics" view or scrape preview.
 */
export async function getPrometheusMetrics(): Promise<string> {
  const url = `${API_BASE_URL}/ops/metrics/prometheus`;
  const headers: Record<string, string> = {};
  try {
    // Session cookie + anti-CSRF token are attached by the SuperTokens SDK;
    // an auth failure triggers a refresh-then-retry, else a redirect to
    // sign-in (Req 8.4, 8.5).
    const response = await fetchWithSession(fetchWithTimeout, url, { headers });
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new ApiError(
        body || `HTTP error! status: ${response.status}`,
        response.status,
      );
    }
    return await response.text();
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

// ─── Replay / Backfill Endpoints ──────────────────────────────────────────────

export interface ReplayTriggerPayload {
  tenant_id: string;
  /** ISO 8601 start of the backfill range. */
  start_time: string;
  /** ISO 8601 end of the backfill range (must be after start_time). */
  end_time: string;
}

export type ReplayJobState = "pending" | "running" | "completed" | "failed";

export interface ReplayJobStatus {
  job_id: string;
  tenant_id: string;
  status: ReplayJobState;
  total_records: number;
  processed_count: number;
  failed_count: number;
  skipped_count: number;
  estimated_remaining?: string | null;
  started_at: string;
  completed_at?: string | null;
}

/**
 * POST /ops/replay/trigger — start a backfill job for a tenant + time range.
 *
 * The job runs in the background; poll {@link getReplayStatus} with the
 * returned ``job_id`` to track progress.
 */
export async function triggerReplay(
  payload: ReplayTriggerPayload,
): Promise<{ data: ReplayJobStatus; request_id: string }> {
  return opsRequest<{ data: ReplayJobStatus; request_id: string }>(
    "/ops/replay/trigger",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** GET /ops/replay/status/:jobId — poll a backfill job's progress */
export async function getReplayStatus(
  jobId: string,
): Promise<{ data: ReplayJobStatus; request_id: string }> {
  return opsRequest<{ data: ReplayJobStatus; request_id: string }>(
    `/ops/replay/status/${encodeURIComponent(jobId)}`,
  );
}

// ─── Drift Detection Endpoint ─────────────────────────────────────────────────

export interface DriftRunPayload {
  tenant_id: string;
  /** ISO 8601 start; defaults server-side to the last 24 hours when omitted. */
  start_time?: string;
  /** ISO 8601 end; defaults server-side to now when omitted. */
  end_time?: string;
}

export interface DriftResult {
  tenant_id: string;
  checked_at: string;
  shipment_count_dinee: number;
  shipment_count_runsheet: number;
  rider_count_dinee: number;
  rider_count_runsheet: number;
  divergent_shipments: Record<string, unknown>[];
  divergent_riders: Record<string, unknown>[];
  channel_statuses: Record<string, string>;
  divergent_orders: Record<string, unknown>[];
  drift_percentage: number;
  alert_triggered: boolean;
}

/**
 * POST /ops/drift/run — compare the upstream (Dinee) source state against the
 * Runsheet ES read-model and return divergence results for a tenant.
 */
export async function runDriftDetection(
  payload: DriftRunPayload,
): Promise<{ data: DriftResult; request_id: string }> {
  return opsRequest<{ data: DriftResult; request_id: string }>(
    "/ops/drift/run",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

// ─── Feature Flag Admin Endpoints ─────────────────────────────────────────────

export interface FeatureFlagResult {
  tenant_id: string;
  status: "enabled" | "disabled" | "rolled_back";
  ws_clients_disconnected?: number;
  purge_data?: boolean;
}

/** POST /ops/admin/feature-flags/:tenantId/enable — enable Ops Intelligence */
export async function enableOpsFeatureFlag(
  tenantId: string,
): Promise<{ data: FeatureFlagResult; request_id: string }> {
  return opsRequest<{ data: FeatureFlagResult; request_id: string }>(
    `/ops/admin/feature-flags/${encodeURIComponent(tenantId)}/enable`,
    { method: "POST" },
  );
}

/** POST /ops/admin/feature-flags/:tenantId/disable — disable Ops Intelligence */
export async function disableOpsFeatureFlag(
  tenantId: string,
): Promise<{ data: FeatureFlagResult; request_id: string }> {
  return opsRequest<{ data: FeatureFlagResult; request_id: string }>(
    `/ops/admin/feature-flags/${encodeURIComponent(tenantId)}/disable`,
    { method: "POST" },
  );
}

/**
 * POST /ops/admin/feature-flags/:tenantId/rollback — disable the flag and
 * optionally purge the tenant's data from every ops ES index.
 */
export async function rollbackOpsFeatureFlag(
  tenantId: string,
  purgeData = false,
): Promise<{ data: FeatureFlagResult; request_id: string }> {
  const qs = buildQueryString({ purge_data: purgeData });
  return opsRequest<{ data: FeatureFlagResult; request_id: string }>(
    `/ops/admin/feature-flags/${encodeURIComponent(tenantId)}/rollback${qs}`,
    { method: "POST" },
  );
}
