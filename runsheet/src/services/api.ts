import Session from "supertokens-auth-react/recipe/session";
import type {
  ApiResponse,
  Asset,
  AssetSubtype,
  AssetSummary,
  AssetType,
  FleetFilters,
  Truck,
} from "../types/api";
import { getAuthToken } from "../utils/auth";

// Filters for the multi-asset /fleet/assets endpoint
export interface AssetFilters {
  asset_type?: AssetType;
  asset_subtype?: AssetSubtype;
  status?: string;
}

// Payload for creating a new asset via POST /fleet/assets
export interface CreateAssetPayload {
  asset_id: string;
  asset_type: AssetType;
  asset_subtype: AssetSubtype;
  name: string;
  status?: string;
  current_location: {
    lat: number;
    lon: number;
  };
  plate_number?: string;
  driver_id?: string;
  driver_name?: string;
  vessel_name?: string;
  container_number?: string;
}

// ─── Driver correlation profile (cross-module-entity-linkage task 4) ─────────

/** A resolved reference marker mirroring the backend RefResolver payload. */
export type ResolvedRefStatus = "resolved" | "unresolved" | "empty";

export interface DriverProfileAssetRef {
  status: ResolvedRefStatus;
  id?: string | null;
  summary?: {
    asset_id?: string;
    name?: string;
    asset_type?: string;
    asset_subtype?: string;
    status?: string;
  };
}

export interface DriverQualificationItem {
  qualification_type: string;
  expiry_date?: string | null;
  days_until_expiry?: number | null;
  alert_level: string;
  status: string;
}

export interface DriverQualificationRef {
  status: ResolvedRefStatus;
  driver_id?: string;
  summary?: {
    driver_id: string;
    full_name: string;
    driver_status: string;
    /** Chip-friendly collapsed signal: valid | expiring | expired. */
    overall_status: "valid" | "expiring" | "expired";
    qualifications: DriverQualificationItem[];
  };
}

export interface DriverProfile {
  driver_id: string;
  utilization: Record<string, unknown> & {
    driver_id: string;
    assigned_truck_id?: string | null;
  };
  assigned_truck: DriverProfileAssetRef;
  qualification: DriverQualificationRef;
}

/** A single compliance record contributing to an asset's status. */
export interface AssetComplianceItem {
  kind: "certification" | "meter";
  reference_id: string;
  label: string;
  status: "valid" | "expiring" | "expired";
  expiry_date?: string | null;
  days_until_expiry?: number | null;
  detail?: string | null;
}

/**
 * Per-asset compliance signal for the Fleet assignment surface
 * (GET /api/fleet/assets/{asset_id}/compliance). `overall_status` collapses the
 * contributing certification/meter items (worst wins). `unknown` /
 * `has_records: false` means the asset has no compliance records — rendered as
 * an "unlinked" affordance rather than a misleading green chip.
 */
export interface AssetComplianceSummary {
  asset_id: string;
  overall_status: "valid" | "expiring" | "expired" | "unknown";
  has_records: boolean;
  items: AssetComplianceItem[];
}

// API base URL - replace with actual API endpoint
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// Timeout configuration (in milliseconds)
// Requirement 9.4: Configurable timeouts - 30s for standard calls, 120s for AI streaming
export const API_TIMEOUTS = {
  STANDARD: 30000, // 30 seconds for standard API calls
  AI_STREAMING: 120000, // 120 seconds for AI streaming responses
} as const;

// Custom error class for timeout errors
export class ApiTimeoutError extends Error {
  constructor(message: string = "Request timed out") {
    super(message);
    this.name = "ApiTimeoutError";
  }
}

// Custom error class for API errors
export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// HTTP statuses that indicate the session is missing/expired and should
// trigger a refresh-then-redirect cycle (Req 8.5).
const AUTH_FAILURE_STATUSES = new Set([401, 403]);

// Path the user is redirected to when session recovery fails (Req 8.5).
export const SIGN_IN_PATH = "/signin";

/**
 * Merge SuperTokens session credentials into a request's options.
 *
 * The SuperTokens SDK attaches the session cookie + anti-CSRF token to
 * `fetch`/`XMLHttpRequest` automatically once `credentials: "include"` is set,
 * so the API_Client no longer reads a token from `sessionStorage` or sets an
 * `Authorization` header itself (Req 8.4). `credentials: "include"` ensures the
 * HttpOnly session cookie rides along on cross-origin API calls.
 */
export function withSessionCredentials(options: RequestInit = {}): RequestInit {
  return {
    ...options,
    credentials: "include",
  };
}

/**
 * Redirect the browser to the sign-in page. No-op on the server.
 */
function redirectToSignIn(): void {
  if (typeof window === "undefined") return;
  window.location.assign(SIGN_IN_PATH);
}

/**
 * Recover from an authentication-failure response.
 *
 * Attempts a SuperTokens session refresh (Req 8.5). Returns `true` when the
 * session was successfully refreshed and the caller should retry the request.
 * When refresh fails (no recoverable session), redirects the user to sign-in
 * and returns `false`.
 */
export async function handleAuthFailure(): Promise<boolean> {
  if (typeof window === "undefined") return false;

  try {
    const refreshed = await Session.attemptRefreshingSession();
    if (refreshed) {
      return true;
    }
  } catch {
    // Fall through to redirect — the session is unrecoverable.
  }

  redirectToSignIn();
  return false;
}

/**
 * Returns true when the response is an authentication failure that the
 * session-recovery flow should act on.
 */
export function isAuthFailure(status: number): boolean {
  return AUTH_FAILURE_STATUSES.has(status);
}

/**
 * Issue a request with SuperTokens session credentials attached, transparently
 * recovering from an authentication failure.
 *
 * `fetcher` is the caller's own timeout/retry-aware fetch wrapper. The request
 * is sent with `credentials: "include"` so the SDK attaches the session cookie
 * + anti-CSRF token (Req 8.4). When the response is an auth failure (401/403),
 * a single SuperTokens session refresh is attempted; on success the request is
 * retried once, and on failure the user is redirected to sign-in (Req 8.5).
 */
export async function fetchWithSession(
  fetcher: (
    url: string,
    options: RequestInit,
    timeout?: number,
  ) => Promise<Response>,
  url: string,
  options: RequestInit = {},
  timeout?: number,
): Promise<Response> {
  const requestInit = withSessionCredentials(options);
  let response = await fetcher(url, requestInit, timeout);

  if (isAuthFailure(response.status)) {
    const refreshed = await handleAuthFailure();
    if (refreshed) {
      response = await fetcher(url, requestInit, timeout);
    }
  }

  return response;
}

// Helper function to create a fetch with timeout
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

// Retry configuration
const RETRY_CONFIG = {
  maxRetries: 2,
  initialDelayMs: 500,
  backoffMultiplier: 2,
  retryableStatuses: new Set([408, 429, 502, 503, 504]),
} as const;

// Helper function to fetch with timeout and retry
async function fetchWithRetry(
  url: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUTS.STANDARD,
): Promise<Response> {
  let lastError: Error | null = null;

  for (let attempt = 0; attempt <= RETRY_CONFIG.maxRetries; attempt++) {
    try {
      const response = await fetchWithTimeout(url, options, timeout);

      // Don't retry successful responses or non-retryable errors
      if (response.ok || !RETRY_CONFIG.retryableStatuses.has(response.status)) {
        return response;
      }

      // Retryable HTTP status — treat as error for retry
      lastError = new ApiError(
        `HTTP error! status: ${response.status}`,
        response.status,
      );
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error));

      // Don't retry non-transient errors
      if (
        error instanceof ApiError &&
        !RETRY_CONFIG.retryableStatuses.has(error.status)
      ) {
        throw error;
      }
    }

    // Wait before retrying (skip delay on last attempt)
    if (attempt < RETRY_CONFIG.maxRetries) {
      const delay =
        RETRY_CONFIG.initialDelayMs * RETRY_CONFIG.backoffMultiplier ** attempt;
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }

  throw lastError ?? new Error("Request failed after retries");
}

// Types for other components
export interface InventoryItem {
  id: string;
  name: string;
  category: string;
  quantity: number;
  unit: string;
  location: string;
  status: "in_stock" | "low_stock" | "out_of_stock";
  lastUpdated: string;
}

export interface SupportTicket {
  id: string;
  customer: string;
  issue: string;
  description: string;
  priority: "low" | "medium" | "high" | "urgent";
  status: "open" | "in_progress" | "resolved" | "closed";
  createdAt: string;
  assignedTo?: string;
  relatedOrder?: string;
}

export interface AnalyticsMetrics {
  delivery_performance: {
    title: string;
    value: string;
    change: string;
    trend: "up" | "down";
  };
  average_delay: {
    title: string;
    value: string;
    change: string;
    trend: "up" | "down";
  };
  fleet_utilization: {
    title: string;
    value: string;
    change: string;
    trend: "up" | "down";
  };
  customer_satisfaction: {
    title: string;
    value: string;
    change: string;
    trend: "up" | "down";
  };
}

class ApiService {
  private async request<T>(
    endpoint: string,
    options?: RequestInit,
    timeout: number = API_TIMEOUTS.STANDARD,
  ): Promise<ApiResponse<T>> {
    try {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        ...(options?.headers as Record<string, string> | undefined),
      };

      // The SuperTokens SDK attaches the session cookie + anti-CSRF token to
      // the request automatically; we only need to opt into credentialed
      // requests and stop reading a token from sessionStorage (Req 8.4).
      const requestInit = withSessionCredentials({ ...options, headers });

      let response = await fetchWithRetry(
        `${API_BASE_URL}${endpoint}`,
        requestInit,
        timeout,
      );

      // On an auth failure, attempt a session refresh and retry once; if the
      // refresh fails the user is redirected to sign-in (Req 8.5).
      if (isAuthFailure(response.status)) {
        const refreshed = await handleAuthFailure();
        if (refreshed) {
          response = await fetchWithRetry(
            `${API_BASE_URL}${endpoint}`,
            requestInit,
            timeout,
          );
        }
      }

      if (!response.ok) {
        throw new ApiError(
          `HTTP error! status: ${response.status}`,
          response.status,
        );
      }

      return await response.json();
    } catch (error) {
      if (error instanceof ApiTimeoutError) {
        console.error("API request timed out:", error.message);
        throw error;
      }
      if (error instanceof ApiError) {
        console.error("API request failed:", error.message);
        throw error;
      }
      console.error("API request failed:", error);
      throw error;
    }
  }

  // Driver utilization (Drivers → Utilization tab)
  //
  // Session-aware fetch of the per-driver utilization summary
  // (GET /api/ops/drivers/utilization). Returns the items array; an optional
  // status filter narrows the result server-side.
  async getDriverUtilization(status?: string): Promise<unknown[]> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    const response = await fetchWithSession(
      fetchWithTimeout,
      `${API_BASE_URL}/ops/drivers/utilization${qs}`,
      { method: "GET", headers: { "Content-Type": "application/json" } },
    );
    if (!response.ok) {
      throw new ApiError(
        `Failed to load driver utilization (status ${response.status})`,
        response.status,
      );
    }
    const json = await response.json();
    const data = json?.items ?? json?.data ?? json;
    return Array.isArray(data) ? data : [];
  }

  // Driver correlation profile (Drivers → Utilization)
  //
  // Session-aware fetch of the correlated driver profile
  // (GET /api/ops/drivers/{driver_id}/profile). Joins the utilization record
  // with the assigned_truck_id resolved to a fleet asset and the compliance
  // qualification summary keyed by the same driver_id. References that do not
  // resolve are returned with an explicit { status: "unresolved" | "empty" }
  // marker rather than omitted.
  async getDriverProfile(driverId: string): Promise<DriverProfile> {
    const response = await fetchWithSession(
      fetchWithTimeout,
      `${API_BASE_URL}/ops/drivers/${encodeURIComponent(driverId)}/profile`,
      { method: "GET", headers: { "Content-Type": "application/json" } },
    );
    if (!response.ok) {
      throw new ApiError(
        `Failed to load driver profile (status ${response.status})`,
        response.status,
      );
    }
    return (await response.json()) as DriverProfile;
  }

  // Asset compliance status (Fleet assignment surface)
  //
  // Session-aware fetch of the per-asset compliance signal
  // (GET /api/fleet/assets/{asset_id}/compliance). Collapses the asset's
  // certification + meter-calibration records into a single overall_status
  // (valid | expiring | expired | unknown) so the Fleet assignment surface can
  // flag a non-compliant asset before dispatch (cross-module-entity-linkage
  // task 10.2 / Req 11.2). An asset with no compliance records returns
  // overall_status="unknown" / has_records=false rather than an error.
  async getAssetCompliance(assetId: string): Promise<AssetComplianceSummary> {
    const response = await fetchWithSession(
      fetchWithTimeout,
      `${API_BASE_URL}/fleet/assets/${encodeURIComponent(assetId)}/compliance`,
      { method: "GET", headers: { "Content-Type": "application/json" } },
    );
    if (!response.ok) {
      throw new ApiError(
        `Failed to load asset compliance (status ${response.status})`,
        response.status,
      );
    }
    const json = await response.json();
    return (json?.data ?? json) as AssetComplianceSummary;
  }
  //
  // Fetch the signed-in user's identity (email / roles / tenant), derived by
  // the backend from the verified session. Returns null when unauthenticated.
  async getAccountProfile(): Promise<{
    user_id: string;
    email: string;
    tenant_id: string;
    roles: string[];
    has_pii_access: boolean;
  } | null> {
    const response = await fetchWithSession(
      fetchWithTimeout,
      `${API_BASE_URL}/auth/account/me`,
      { method: "GET", headers: { "Content-Type": "application/json" } },
    );
    if (!response.ok) return null;
    return response.json();
  }

  // Change the signed-in user's password. Uses the session-aware fetch so the
  // SuperTokens cookie + anti-CSRF token ride along, and surfaces the backend's
  // structured error message (e.g. wrong current password) to the caller.
  async changePassword(
    currentPassword: string,
    newPassword: string,
  ): Promise<void> {
    const response = await fetchWithSession(
      fetchWithTimeout,
      `${API_BASE_URL}/auth/account/change-password`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      },
    );

    if (!response.ok) {
      let message = "Could not change your password. Please try again.";
      try {
        const body = await response.json();
        if (body?.message) message = body.message;
      } catch {
        // Non-JSON error body — keep the generic message.
      }
      throw new ApiError(message, response.status);
    }
  }

  // Fleet Management
  async getFleetSummary(): Promise<ApiResponse<AssetSummary>> {
    return this.request<AssetSummary>("/fleet/summary");
  }

  async getTrucks(filters?: FleetFilters): Promise<ApiResponse<Truck[]>> {
    const queryParams = filters
      ? `?${new URLSearchParams(filters as any).toString()}`
      : "";
    return this.request<Truck[]>(`/fleet/trucks${queryParams}`);
  }

  async getTruckById(id: string): Promise<ApiResponse<Truck>> {
    return this.request<Truck>(`/fleet/trucks/${id}`);
  }

  async updateTruckStatus(
    id: string,
    status: string,
  ): Promise<ApiResponse<Truck>> {
    return this.request<Truck>(`/fleet/trucks/${id}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
  }

  // Multi-Asset Management
  async getAssets(filters?: AssetFilters): Promise<ApiResponse<Asset[]>> {
    const params = new URLSearchParams();
    if (filters?.asset_type) params.set("asset_type", filters.asset_type);
    if (filters?.asset_subtype)
      params.set("asset_subtype", filters.asset_subtype);
    if (filters?.status) params.set("status", filters.status);
    const query = params.toString();
    return this.request<Asset[]>(`/fleet/assets${query ? `?${query}` : ""}`);
  }

  async getAsset(id: string): Promise<ApiResponse<Asset>> {
    return this.request<Asset>(`/fleet/assets/${id}`);
  }

  async createAsset(data: CreateAssetPayload): Promise<ApiResponse<Asset>> {
    return this.request<Asset>("/fleet/assets", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async updateAsset(
    id: string,
    data: Partial<Asset>,
  ): Promise<ApiResponse<Asset>> {
    return this.request<Asset>(`/fleet/assets/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  }

  // Inventory Management
  async getInventory(): Promise<ApiResponse<InventoryItem[]>> {
    const response = await this.request<any[]>("/inventory/items");
    // Map backend item_id to frontend id
    response.data = (response.data || []).map((item: any) => ({
      ...item,
      id: item.id || item.item_id,
      lastUpdated:
        item.lastUpdated ||
        item.last_restocked ||
        item.updated_at ||
        new Date().toISOString(),
    }));
    return response as ApiResponse<InventoryItem[]>;
  }

  async getInventoryById(id: string): Promise<ApiResponse<InventoryItem>> {
    const response = await this.request<any>(`/inventory/items/${id}`);
    if (response.data) {
      response.data.id = response.data.id || response.data.item_id;
      response.data.lastUpdated =
        response.data.lastUpdated ||
        response.data.last_restocked ||
        response.data.updated_at ||
        new Date().toISOString();
    }
    return response as ApiResponse<InventoryItem>;
  }

  async updateInventoryItem(
    id: string,
    data: Partial<InventoryItem>,
  ): Promise<ApiResponse<InventoryItem>> {
    return this.request<InventoryItem>(`/inventory/items/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  }

  // Support Management
  async getSupportTickets(): Promise<ApiResponse<SupportTicket[]>> {
    return this.request<SupportTicket[]>("/support/tickets");
  }

  async getSupportTicketById(id: string): Promise<ApiResponse<SupportTicket>> {
    return this.request<SupportTicket>(`/support/tickets/${id}`);
  }

  async createSupportTicket(
    ticket: Omit<SupportTicket, "id" | "createdAt">,
  ): Promise<ApiResponse<SupportTicket>> {
    return this.request<SupportTicket>("/support/tickets", {
      method: "POST",
      body: JSON.stringify(ticket),
    });
  }

  async updateSupportTicket(
    id: string,
    data: Partial<SupportTicket>,
  ): Promise<ApiResponse<SupportTicket>> {
    return this.request<SupportTicket>(`/support/tickets/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  }

  // Analytics
  async getAnalyticsMetrics(
    timeRange: string = "7d",
  ): Promise<ApiResponse<AnalyticsMetrics>> {
    return this.request<AnalyticsMetrics>(
      `/analytics/metrics?timeRange=${timeRange}`,
    );
  }

  async getAnalyticsRoutePerformance(): Promise<ApiResponse<any[]>> {
    return this.request<any[]>("/analytics/routes");
  }

  // Data Upload - Legacy methods (keeping for compatibility)
  async uploadFromSheets(
    url: string,
    dataType: string,
  ): Promise<ApiResponse<{ recordCount: number }>> {
    return this.request<{ recordCount: number }>("/data/upload/sheets", {
      method: "POST",
      body: JSON.stringify({ url, dataType }),
    });
  }

  async uploadCSV(
    file: File,
    dataType: string,
  ): Promise<ApiResponse<{ recordCount: number }>> {
    // Validate file size (max 50MB)
    const MAX_FILE_SIZE = 50 * 1024 * 1024;
    if (file.size > MAX_FILE_SIZE) {
      throw new ApiError(
        `File too large: ${(file.size / 1024 / 1024).toFixed(1)}MB exceeds 50MB limit`,
        413,
      );
    }

    // Validate file type
    const allowedTypes = [".csv", ".xlsx", ".xls"];
    const fileName = file.name.toLowerCase();
    if (!allowedTypes.some((ext) => fileName.endsWith(ext))) {
      throw new ApiError(
        `Invalid file type: ${file.name}. Allowed: ${allowedTypes.join(", ")}`,
        415,
      );
    }

    const formData = new FormData();
    formData.append("file", file);
    formData.append("dataType", dataType);

    return this.request<{ recordCount: number }>("/data/upload/csv", {
      method: "POST",
      body: formData,
      headers: {}, // Let browser set Content-Type for FormData
    });
  }

  // Temporal Data Upload - New methods for demo
  async uploadTemporalCSV(
    file: File,
    dataType: string,
    batchId: string,
    operationalTime: string,
  ): Promise<
    ApiResponse<{
      recordCount: number;
      batch_id: string;
      operational_time: string;
    }>
  > {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("data_type", dataType);
    formData.append("batch_id", batchId);
    formData.append("operational_time", operationalTime);

    return this.request<{
      recordCount: number;
      batch_id: string;
      operational_time: string;
    }>("/upload/csv", {
      method: "POST",
      body: formData,
      headers: {}, // Let browser set Content-Type for FormData
    });
  }

  async uploadTemporalSheets(
    url: string,
    dataType: string,
    batchId: string,
    operationalTime: string,
  ): Promise<
    ApiResponse<{
      recordCount: number;
      batch_id: string;
      operational_time: string;
    }>
  > {
    return this.request<{
      recordCount: number;
      batch_id: string;
      operational_time: string;
    }>("/upload/sheets", {
      method: "POST",
      body: JSON.stringify({
        data_type: dataType,
        batch_id: batchId,
        operational_time: operationalTime,
        sheets_url: url,
      }),
    });
  }

  async uploadBatchTemporal(
    batchId: string,
    operationalTime: string,
  ): Promise<
    ApiResponse<{
      recordCount: number;
      batch_id: string;
      operational_time: string;
      breakdown: Record<string, number>;
    }>
  > {
    return this.request<{
      recordCount: number;
      batch_id: string;
      operational_time: string;
      breakdown: Record<string, number>;
    }>("/upload/batch", {
      method: "POST",
      body: JSON.stringify({
        batch_id: batchId,
        operational_time: operationalTime,
      }),
    });
  }

  async uploadSelectiveTemporal(
    dataTypes: string[],
    batchId: string,
    operationalTime: string,
  ): Promise<
    ApiResponse<{
      recordCount: number;
      batch_id: string;
      operational_time: string;
      breakdown: Record<string, number>;
    }>
  > {
    return this.request<{
      recordCount: number;
      batch_id: string;
      operational_time: string;
      breakdown: Record<string, number>;
    }>("/upload/selective", {
      method: "POST",
      body: JSON.stringify({
        data_types: dataTypes,
        batch_id: batchId,
        operational_time: operationalTime,
      }),
    });
  }

  // Real-time updates
  // Note: For React components, use the useFleetWebSocket hook instead
  // This method is kept for backward compatibility
  async subscribeToFleetUpdates(
    callback: (data: Truck[]) => void,
  ): Promise<() => void> {
    // WebSocket connection for real-time updates with reconnection
    // For better reconnection handling, use the useFleetWebSocket hook in React components

    // Get auth token for WebSocket connection
    const token = await getAuthToken();
    const baseWsUrl = `${API_BASE_URL.replace("http", "ws")}/fleet/live`;
    const wsUrl = token
      ? `${baseWsUrl}?token=${encodeURIComponent(token)}`
      : baseWsUrl;

    let ws: WebSocket | null = null;
    let reconnectAttempt = 0;
    let reconnectTimeout: NodeJS.Timeout | null = null;
    let shouldReconnect = true;

    const INITIAL_RECONNECT_DELAY = 1000; // 1 second
    const MAX_RECONNECT_DELAY = 30000; // 30 seconds
    const BACKOFF_MULTIPLIER = 2;

    /**
     * Calculate exponential backoff delay with jitter
     * Validates: Requirement 9.5 - exponential backoff
     */
    const calculateBackoffDelay = (attempt: number): number => {
      const exponentialDelay =
        INITIAL_RECONNECT_DELAY * BACKOFF_MULTIPLIER ** (attempt - 1);
      const cappedDelay = Math.min(exponentialDelay, MAX_RECONNECT_DELAY);
      // Add jitter (±25%) to prevent thundering herd
      const jitter = cappedDelay * 0.25 * (Math.random() * 2 - 1);
      return Math.floor(cappedDelay + jitter);
    };

    /**
     * Connect to WebSocket with reconnection support
     */
    const connect = async () => {
      if (!shouldReconnect) return;

      try {
        // Get fresh token on each reconnect attempt
        const freshToken = await getAuthToken();
        const reconnectWsUrl = freshToken
          ? `${API_BASE_URL.replace("http", "ws")}/fleet/live?token=${encodeURIComponent(freshToken)}`
          : `${API_BASE_URL.replace("http", "ws")}/fleet/live`;

        ws = new WebSocket(reconnectWsUrl);

        ws.onopen = () => {
          console.log("Fleet WebSocket connected");
          reconnectAttempt = 0;
        };

        ws.onmessage = (event) => {
          try {
            const message = JSON.parse(event.data);

            // Handle different message types
            if (message.type === "location_update" && message.data) {
              // Convert single update to array format for callback
              callback([message.data as Truck]);
            } else if (
              message.type === "batch_location_update" &&
              message.data?.updates
            ) {
              callback(message.data.updates as Truck[]);
            }
          } catch (error) {
            console.error("Failed to parse WebSocket message:", error);
          }
        };

        ws.onclose = (event) => {
          console.log("Fleet WebSocket disconnected", event.code, event.reason);

          // Reconnect if not a clean close and we should reconnect
          if (shouldReconnect && !event.wasClean) {
            reconnectAttempt++;
            const delay = calculateBackoffDelay(reconnectAttempt);
            console.log(
              `Reconnecting in ${delay}ms (attempt ${reconnectAttempt})`,
            );

            reconnectTimeout = setTimeout(connect, delay);
          }
        };

        ws.onerror = (error) => {
          console.error("Fleet WebSocket error:", error);
          // Error is usually followed by close event, which handles reconnection
        };
      } catch (error) {
        console.error("Failed to create WebSocket:", error);

        // Schedule reconnection
        if (shouldReconnect) {
          reconnectAttempt++;
          const delay = calculateBackoffDelay(reconnectAttempt);
          reconnectTimeout = setTimeout(connect, delay);
        }
      }
    };

    // Initial connection
    connect();

    // Return cleanup function
    return () => {
      shouldReconnect = false;

      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }

      if (ws) {
        ws.onclose = null; // Prevent reconnection on intentional close
        ws.close(1000, "Client unsubscribed");
        ws = null;
      }
    };
  }
}

export const apiService = new ApiService();
