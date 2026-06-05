import type {
  CargoItemStatus,
  Job,
  JobEvent,
  JobStatus,
  JobType,
  Priority,
  SchedulingCargoItem,
} from "../types/api";
import { getAuthToken } from "../utils/auth";
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

export interface SingleResponse<T> {
  data: T;
  request_id: string;
}

// ─── Filter Types ────────────────────────────────────────────────────────────

export interface JobFilters {
  job_type?: JobType;
  status?: JobStatus;
  asset_assigned?: string;
  origin?: string;
  destination?: string;
  start_date?: string;
  end_date?: string;
  page?: number;
  size?: number;
  sort_by?: string;
  sort_order?: "asc" | "desc";
}

export interface CargoSearchFilters {
  container_number?: string;
  description?: string;
  item_status?: CargoItemStatus;
  page?: number;
  size?: number;
}

export interface MetricsFilters {
  bucket?: "hourly" | "daily";
  start_date?: string;
  end_date?: string;
}

// ─── Request Payloads ────────────────────────────────────────────────────────

export interface CreateJobPayload {
  job_type: JobType;
  origin: string;
  destination: string;
  scheduled_time: string;
  asset_assigned?: string;
  cargo_manifest?: Omit<SchedulingCargoItem, "item_id">[];
  priority?: Priority;
  notes?: string;
  created_by?: string;
}

export interface StatusTransitionPayload {
  status: JobStatus;
  failure_reason?: string;
}

// ─── Metrics Response Types ──────────────────────────────────────────────────

export interface JobMetricsBucket {
  timestamp: string;
  counts_by_status: Record<string, number>;
  counts_by_type: Record<string, number>;
}

export interface CompletionMetric {
  job_type: string;
  total: number;
  completed: number;
  completion_rate: number;
  avg_completion_minutes: number;
}

export interface AssetUtilizationMetric {
  asset_id: string;
  asset_type: string;
  total_jobs: number;
  active_jobs: number;
  completed_jobs: number;
  total_active_hours: number;
  idle_hours: number;
}

export interface DelayMetrics {
  total_delayed: number;
  avg_delay_minutes: number;
  delays_by_type: Record<string, number>;
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

async function schedulingRequest<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  try {
    // Get auth token if available (async)
    const token = await getAuthToken();
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options?.headers as Record<string, string> | undefined),
    };

    // Add Authorization header if token exists
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }

    const response = await fetchWithTimeout(url, {
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

// ─── Job Endpoints ───────────────────────────────────────────────────────────

/** GET /scheduling/jobs — paginated job list with filters and sorting */
export async function getJobs(
  filters: JobFilters = {},
): Promise<PaginatedResponse<Job>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return schedulingRequest<PaginatedResponse<Job>>(`/scheduling/jobs${qs}`);
}

/** GET /scheduling/jobs/:id — single job with event history */
export async function getJob(
  jobId: string,
): Promise<SingleResponse<Job & { events?: JobEvent[] }>> {
  return schedulingRequest<SingleResponse<Job & { events?: JobEvent[] }>>(
    `/scheduling/jobs/${encodeURIComponent(jobId)}`,
  );
}

/** GET /scheduling/jobs/active — active jobs (scheduled, assigned, in_progress) */
export async function getActiveJobs(): Promise<SingleResponse<Job[]>> {
  return schedulingRequest<SingleResponse<Job[]>>("/scheduling/jobs/active");
}

/** GET /scheduling/jobs/delayed — delayed in-progress jobs */
export async function getDelayedJobs(): Promise<SingleResponse<Job[]>> {
  return schedulingRequest<SingleResponse<Job[]>>("/scheduling/jobs/delayed");
}

/** POST /scheduling/jobs — create a new job */
export async function createJob(
  data: CreateJobPayload,
): Promise<SingleResponse<Job>> {
  return schedulingRequest<SingleResponse<Job>>("/scheduling/jobs", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// ─── Status Transition Endpoint ──────────────────────────────────────────────

/** PATCH /scheduling/jobs/:id/status — transition job status */
export async function transitionStatus(
  jobId: string,
  data: StatusTransitionPayload,
): Promise<SingleResponse<Job>> {
  return schedulingRequest<SingleResponse<Job>>(
    `/scheduling/jobs/${encodeURIComponent(jobId)}/status`,
    {
      method: "PATCH",
      body: JSON.stringify(data),
    },
  );
}

// ─── Cargo Endpoints ─────────────────────────────────────────────────────────

/** GET /scheduling/jobs/:id/cargo — get cargo manifest for a job */
export async function getCargo(
  jobId: string,
): Promise<SingleResponse<SchedulingCargoItem[]>> {
  return schedulingRequest<SingleResponse<SchedulingCargoItem[]>>(
    `/scheduling/jobs/${encodeURIComponent(jobId)}/cargo`,
  );
}

/** PATCH /scheduling/jobs/:id/cargo — update cargo manifest */
export async function updateCargo(
  jobId: string,
  items: SchedulingCargoItem[],
): Promise<SingleResponse<SchedulingCargoItem[]>> {
  return schedulingRequest<SingleResponse<SchedulingCargoItem[]>>(
    `/scheduling/jobs/${encodeURIComponent(jobId)}/cargo`,
    {
      method: "PATCH",
      body: JSON.stringify({ items }),
    },
  );
}

/** PATCH /scheduling/jobs/:id/cargo/:itemId/status — update cargo item status */
export async function updateCargoItemStatus(
  jobId: string,
  itemId: string,
  status: CargoItemStatus,
): Promise<SingleResponse<SchedulingCargoItem>> {
  return schedulingRequest<SingleResponse<SchedulingCargoItem>>(
    `/scheduling/jobs/${encodeURIComponent(jobId)}/cargo/${encodeURIComponent(itemId)}/status`,
    {
      method: "PATCH",
      body: JSON.stringify({ item_id: itemId, item_status: status }),
    },
  );
}

/** GET /scheduling/cargo/search — search cargo items across all jobs */
export async function searchCargo(
  filters: CargoSearchFilters = {},
): Promise<PaginatedResponse<SchedulingCargoItem & { job_id: string }>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return schedulingRequest<
    PaginatedResponse<SchedulingCargoItem & { job_id: string }>
  >(`/scheduling/cargo/search${qs}`);
}

// ─── Metrics Endpoints ───────────────────────────────────────────────────────

/** GET /scheduling/metrics/jobs — job counts by status/type in time buckets */
export async function getJobMetrics(
  filters: MetricsFilters = {},
): Promise<SingleResponse<JobMetricsBucket[]>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return schedulingRequest<SingleResponse<JobMetricsBucket[]>>(
    `/scheduling/metrics/jobs${qs}`,
  );
}

/** GET /scheduling/metrics/completion — completion rate and avg time by job_type */
export async function getCompletionMetrics(
  filters: MetricsFilters = {},
): Promise<SingleResponse<CompletionMetric[]>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return schedulingRequest<SingleResponse<CompletionMetric[]>>(
    `/scheduling/metrics/completion${qs}`,
  );
}

/** GET /scheduling/metrics/assets — asset utilization metrics */
export async function getAssetUtilization(
  filters: MetricsFilters = {},
): Promise<SingleResponse<AssetUtilizationMetric[]>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return schedulingRequest<SingleResponse<AssetUtilizationMetric[]>>(
    `/scheduling/metrics/assets${qs}`,
  );
}

/** GET /scheduling/metrics/delays — delay statistics */
export async function getDelayMetrics(
  filters: MetricsFilters = {},
): Promise<SingleResponse<DelayMetrics>> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined>,
  );
  return schedulingRequest<SingleResponse<DelayMetrics>>(
    `/scheduling/metrics/delays${qs}`,
  );
}

/** GET /scheduling/jobs/:id/eta — get ETA for a job */
export async function getJobEta(jobId: string): Promise<
  SingleResponse<{
    eta_minutes: number;
    estimated_arrival: string;
    calculated_at: string;
  }>
> {
  return schedulingRequest<
    SingleResponse<{
      eta_minutes: number;
      estimated_arrival: string;
      calculated_at: string;
    }>
  >(`/scheduling/jobs/${encodeURIComponent(jobId)}/eta`);
}

/** PATCH /scheduling/jobs/:id/reassign — reassign asset to a job */
export async function reassignAsset(
  jobId: string,
  newAssetId: string,
): Promise<SingleResponse<Job>> {
  return schedulingRequest<SingleResponse<Job>>(
    `/scheduling/jobs/${encodeURIComponent(jobId)}/reassign`,
    {
      method: "PATCH",
      body: JSON.stringify({ asset_id: newAssetId }),
    },
  );
}

// ─── Driver Acknowledgment Endpoints ─────────────────────────────────────────

/** Result of a driver ack/accept/reject action (job lifecycle event). */
export interface DriverActionResult {
  job_id: string;
  action: "ack" | "accept" | "reject";
  actor_id: string;
  timestamp: string;
  previous_status?: JobStatus;
  new_status?: JobStatus;
  reason?: string;
  device_id?: string | null;
}

/**
 * POST /scheduling/jobs/:id/ack — driver acknowledges an assignment.
 *
 * The job must be in ``assigned`` status. Appends an ``ack`` event to the
 * job timeline. This is the action the DriverNudgeAgent escalates when it
 * is missing past the nudge timeout.
 */
export async function ackJob(
  jobId: string,
  deviceId?: string,
): Promise<SingleResponse<DriverActionResult>> {
  return schedulingRequest<SingleResponse<DriverActionResult>>(
    `/scheduling/jobs/${encodeURIComponent(jobId)}/ack`,
    {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId ?? null }),
    },
  );
}

/**
 * POST /scheduling/jobs/:id/accept — driver accepts an assignment.
 *
 * A ``scheduled`` job transitions to ``assigned`` (claiming it for the
 * acting driver); an already-``assigned`` job is confirmed.
 */
export async function acceptJob(
  jobId: string,
): Promise<SingleResponse<DriverActionResult>> {
  return schedulingRequest<SingleResponse<DriverActionResult>>(
    `/scheduling/jobs/${encodeURIComponent(jobId)}/accept`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

/**
 * POST /scheduling/jobs/:id/reject — driver rejects an assignment.
 *
 * Requires a ``reason``. An ``assigned`` job reverts to ``scheduled`` so it
 * can be re-dispatched.
 */
export async function rejectJob(
  jobId: string,
  reason: string,
): Promise<SingleResponse<DriverActionResult>> {
  return schedulingRequest<SingleResponse<DriverActionResult>>(
    `/scheduling/jobs/${encodeURIComponent(jobId)}/reject`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}

// ─── Job Reroute Endpoint ────────────────────────────────────────────────────

export interface RerouteJobPayload {
  new_destination: string;
  reason?: string;
}

/**
 * POST /api/v1/scheduling/jobs/:id/reroute — reroute an in-flight job to a
 * new destination. Mounted under the ``/api/v1/scheduling`` prefix (distinct
 * from the other scheduling routes), so the leading ``/api`` is stripped from
 * the base before composing the v1 path.
 */
export async function rerouteJob(
  jobId: string,
  payload: RerouteJobPayload,
): Promise<SingleResponse<Job>> {
  // API_BASE_URL ends in `/api`; the reroute route lives at `/api/v1/...`.
  const base = API_BASE_URL.replace(/\/api$/, "");
  const url = `${base}/api/v1/scheduling/jobs/${encodeURIComponent(jobId)}/reroute`;
  const token = await getAuthToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  try {
    const response = await fetchWithTimeout(url, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
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
