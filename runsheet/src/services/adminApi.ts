import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { buildQueryString, fetchWithTimeout } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";

// ─── Communication Metrics Types ─────────────────────────────────────────────

export interface MetricDataPoint {
  timestamp: string;
  value: number;
}

export interface CommunicationMetrics {
  ack_latency: MetricDataPoint[];
  notification_send_latency: MetricDataPoint[];
  driver_response_latency: MetricDataPoint[];
  failed_notification_rate: MetricDataPoint[];
  request_id: string;
}

export interface MetricsFilters {
  start_date?: string;
  end_date?: string;
  interval?: string; // '1h', '1d', etc.
}

// ─── Feature Flag Types ──────────────────────────────────────────────────────

export type FeatureFlagState =
  | "disabled"
  | "shadow"
  | "active_gated"
  | "active_auto";

export interface FeatureFlagResponse {
  tenant_id: string;
  flag_key: string;
  previous_state: FeatureFlagState;
  new_state: FeatureFlagState;
  ws_broadcast: boolean;
}

export interface FeatureFlagStateResponse {
  tenant_id: string;
  flag_key: string;
  state: FeatureFlagState;
}

// ─── Agent Monitoring Types ──────────────────────────────────────────────────

export interface AgentHealth {
  agent_id: string;
  status: string;
  type: "autonomous" | "overlay";
}

export interface AgentHealthResponse {
  agents: Record<string, AgentHealth>;
}

export interface ActivityLogEntry {
  agent_id: string;
  action_type: string;
  tool_name?: string | null;
  parameters?: any;
  risk_level?: string | null;
  outcome: string;
  duration_ms: number;
  tenant_id: string;
  user_id?: string | null;
  session_id?: string | null;
  details?: any;
  timestamp?: string;
}

export interface ActivityLogResponse {
  items: ActivityLogEntry[];
  total: number;
  page: number;
  page_size: number;
}

export interface ActivityStats {
  total_actions: number;
  success_rate: number;
  average_duration_ms: number;
  actions_by_agent: Record<string, number>;
  actions_by_type: Record<string, number>;
}

export interface ApprovalEntry {
  action_id: string;
  agent_id: string;
  action_type: string;
  tool_name?: string;
  parameters?: any;
  risk_level?: string;
  proposed_at: string;
  status: string;
}

export interface ApprovalsResponse {
  items: ApprovalEntry[];
  total: number;
  page: number;
  page_size: number;
}

// ─── Stripe Integration Types ────────────────────────────────────────────────

export interface StripePublicConfig {
  publishable_key: string;
}

export interface StripePaymentItem {
  id: string;
  status?: string | null;
  amount?: number | null;
  currency?: string | null;
  created?: number | null;
  customer?: string | null;
  description?: string | null;
  metadata: Record<string, string>;
  // Canonical commerce mapping (cross-module-entity-linkage Req 12.3): an
  // external Stripe charge is either "mapped" to a canonical commerce payment
  // (carrying the canonical payment/invoice/account ids) or "unmapped".
  mapping_status?: "mapped" | "unmapped";
  canonical_payment_id?: string | null;
  invoice_id?: string | null;
  account_id?: string | null;
}

export interface StripePaymentsResponse {
  items: StripePaymentItem[];
  has_more: boolean;
  next_starting_after?: string | null;
}

// ─── HTTP Helper ─────────────────────────────────────────────────────────────

async function adminRequest<T>(
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

// ─── Communication Metrics Endpoints ─────────────────────────────────────────

/** GET /api/metrics/communications — get all communication SLA metrics */
export async function getCommunicationMetrics(
  filters: MetricsFilters = {},
): Promise<CommunicationMetrics> {
  const qs = buildQueryString(
    filters as Record<string, string | number | boolean | undefined | null>,
  );
  return adminRequest<CommunicationMetrics>(`/metrics/communications${qs}`);
}

// ─── Feature Flag Endpoints ──────────────────────────────────────────────────

/** GET /api/ops/admin/feature-flags/{tenant_id}/order-intake-pipeline */
export async function getOrderIntakePipelineState(
  tenantId: string,
): Promise<{ data: FeatureFlagStateResponse; request_id: string }> {
  return adminRequest<{ data: FeatureFlagStateResponse; request_id: string }>(
    `/ops/admin/feature-flags/${encodeURIComponent(tenantId)}/order-intake-pipeline`,
  );
}

/** POST /api/ops/admin/feature-flags/{tenant_id}/order-intake-pipeline/{new_state} */
export async function setOrderIntakePipelineState(
  tenantId: string,
  newState: FeatureFlagState,
): Promise<{ data: FeatureFlagResponse; request_id: string }> {
  return adminRequest<{ data: FeatureFlagResponse; request_id: string }>(
    `/ops/admin/feature-flags/${encodeURIComponent(tenantId)}/order-intake-pipeline/${newState}`,
    { method: "POST" },
  );
}

// ─── Agent Monitoring Endpoints ──────────────────────────────────────────────

/** GET /api/agent/health — get agent health status */
export async function getAgentHealth(): Promise<AgentHealthResponse> {
  return adminRequest<AgentHealthResponse>("/agent/health");
}

/** POST /api/agent/{agent_id}/pause — pause an agent */
export async function pauseAgent(
  agentId: string,
): Promise<{ agent_id: string; status: string }> {
  return adminRequest<{ agent_id: string; status: string }>(
    `/agent/${encodeURIComponent(agentId)}/pause`,
    { method: "POST" },
  );
}

/** POST /api/agent/{agent_id}/resume — resume an agent */
export async function resumeAgent(
  agentId: string,
): Promise<{ agent_id: string; status: string }> {
  return adminRequest<{ agent_id: string; status: string }>(
    `/agent/${encodeURIComponent(agentId)}/resume`,
    { method: "POST" },
  );
}

/** GET /api/agent/activity — get activity log */
export async function getActivityLog(params: {
  agent_id?: string;
  action_type?: string;
  outcome?: string;
  time_from?: string;
  time_to?: string;
  page?: number;
  size?: number;
}): Promise<ActivityLogResponse> {
  const qs = buildQueryString(
    params as Record<string, string | number | boolean | undefined | null>,
  );
  return adminRequest<ActivityLogResponse>(`/agent/activity${qs}`);
}

/** GET /api/agent/activity/stats — get activity statistics */
export async function getActivityStats(): Promise<ActivityStats> {
  return adminRequest<ActivityStats>("/agent/activity/stats");
}

/** GET /api/agent/approvals — get pending approvals */
export async function getApprovals(params: {
  page?: number;
  size?: number;
}): Promise<ApprovalsResponse> {
  const qs = buildQueryString(
    params as Record<string, string | number | boolean | undefined | null>,
  );
  return adminRequest<ApprovalsResponse>(`/agent/approvals${qs}`);
}

/** POST /api/agent/approvals/{action_id}/approve — approve an action.
 * The reviewer is derived server-side from the authenticated session. */
export async function approveAction(actionId: string): Promise<any> {
  return adminRequest<any>(
    `/agent/approvals/${encodeURIComponent(actionId)}/approve`,
    { method: "POST" },
  );
}

/** POST /api/agent/approvals/{action_id}/reject — reject an action.
 * The reviewer is derived server-side from the authenticated session. */
export async function rejectAction(
  actionId: string,
  reason: string = "",
): Promise<any> {
  return adminRequest<any>(
    `/agent/approvals/${encodeURIComponent(actionId)}/reject`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}

// ─── Stripe Integration Endpoints ────────────────────────────────────────────

/** GET /api/integrations/stripe/public-config — get Stripe publishable key */
export async function getStripePublicConfig(): Promise<StripePublicConfig> {
  return adminRequest<StripePublicConfig>("/integrations/stripe/public-config");
}

/** GET /api/integrations/stripe/payments — list Stripe payments */
export async function getStripePayments(params: {
  limit?: number;
  starting_after?: string;
  created_gte?: string;
  created_lte?: string;
}): Promise<StripePaymentsResponse> {
  const qs = buildQueryString(
    params as Record<string, string | number | boolean | undefined | null>,
  );
  return adminRequest<StripePaymentsResponse>(
    `/integrations/stripe/payments${qs}`,
  );
}
