/**
 * Agent API service for the Agentic AI Transformation layer.
 *
 * Provides typed functions for interacting with the agent REST endpoints:
 * - Approval queue (list, approve, reject)
 * - Activity log (list, stats)
 * - Agent health (status, pause, resume)
 *
 * Follows the same pattern as opsApi.ts and schedulingApi.ts.
 *
 * Requirements: 2.3, 2.4, 2.5, 8.4, 8.5, 9.5, 9.6
 */

import { API_TIMEOUTS, ApiError, ApiTimeoutError } from "./api";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Types ───────────────────────────────────────────────────────────────────

export type ApprovalStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "expired"
  | "executed";

export type RiskLevel = "low" | "medium" | "high";

export type AgentStatus = "running" | "stopped" | "error";

export interface ApprovalEntry {
  action_id: string;
  action_type: string;
  tool_name: string;
  parameters: Record<string, unknown>;
  risk_level: RiskLevel;
  proposed_by: string;
  proposed_at: string;
  status: ApprovalStatus;
  reviewed_by: string | null;
  reviewed_at: string | null;
  expiry_time: string;
  impact_summary: string;
  execution_result?: Record<string, unknown>;
  tenant_id: string;
}

export interface ActivityLogEntry {
  log_id: string;
  agent_id: string;
  action_type: string;
  tool_name: string | null;
  parameters: Record<string, unknown> | null;
  risk_level: string | null;
  outcome: string;
  duration_ms: number;
  tenant_id: string;
  user_id: string | null;
  session_id: string | null;
  timestamp: string;
  details: Record<string, unknown> | null;
}

export interface AgentHealthEntry {
  agent_id: string;
  status: AgentStatus;
}

export interface AgentHealthResponse {
  agents: Record<string, AgentHealthEntry>;
}

export interface PaginatedApprovals {
  entries: ApprovalEntry[];
  total: number;
  page: number;
  size: number;
}

export interface PaginatedActivity {
  entries: ActivityLogEntry[];
  total: number;
  page: number;
  size: number;
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

async function agentRequest<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}/agent${endpoint}`;
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

// ─── Approval Queue Endpoints ────────────────────────────────────────────────

/** GET /agent/approvals — list pending approvals for a tenant */
export async function getApprovals(
  tenantId: string = "default",
  page: number = 1,
  size: number = 20,
): Promise<PaginatedApprovals> {
  const qs = buildQueryString({ tenant_id: tenantId, page, size });
  return agentRequest<PaginatedApprovals>(`/approvals${qs}`);
}

/** POST /agent/approvals/{action_id}/approve — approve a pending action */
export async function approveAction(
  actionId: string,
  reviewerId: string = "admin",
): Promise<Record<string, unknown>> {
  const qs = buildQueryString({ reviewer_id: reviewerId });
  return agentRequest<Record<string, unknown>>(
    `/approvals/${encodeURIComponent(actionId)}/approve${qs}`,
    { method: "POST" },
  );
}

/** POST /agent/approvals/{action_id}/reject — reject a pending action */
export async function rejectAction(
  actionId: string,
  reason: string = "",
  reviewerId: string = "admin",
): Promise<Record<string, unknown>> {
  const qs = buildQueryString({ reviewer_id: reviewerId });
  return agentRequest<Record<string, unknown>>(
    `/approvals/${encodeURIComponent(actionId)}/reject${qs}`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}

// ─── Activity Log Endpoints ──────────────────────────────────────────────────

export interface ActivityFilters {
  tenant_id?: string;
  agent_id?: string;
  action_type?: string;
  outcome?: string;
  time_from?: string;
  time_to?: string;
  page?: number;
  size?: number;
}

/** GET /agent/activity — paginated activity log with filters */
export async function getActivityLog(
  filters: ActivityFilters = {},
): Promise<PaginatedActivity> {
  const qs = buildQueryString({
    tenant_id: filters.tenant_id ?? "default",
    agent_id: filters.agent_id,
    action_type: filters.action_type,
    outcome: filters.outcome,
    time_from: filters.time_from,
    time_to: filters.time_to,
    page: filters.page ?? 1,
    size: filters.size ?? 50,
  });
  return agentRequest<PaginatedActivity>(`/activity${qs}`);
}

// ─── Agent Health Endpoints ──────────────────────────────────────────────────

/** GET /agent/health — status of all autonomous agents */
export async function getAgentHealth(): Promise<AgentHealthResponse> {
  return agentRequest<AgentHealthResponse>("/health");
}

/** POST /agent/{agent_id}/pause — pause an autonomous agent */
export async function pauseAgent(
  agentId: string,
): Promise<{ agent_id: string; status: string }> {
  return agentRequest<{ agent_id: string; status: string }>(
    `/${encodeURIComponent(agentId)}/pause`,
    { method: "POST" },
  );
}

/** POST /agent/{agent_id}/resume — resume a paused autonomous agent */
export async function resumeAgent(
  agentId: string,
): Promise<{ agent_id: string; status: string }> {
  return agentRequest<{ agent_id: string; status: string }>(
    `/${encodeURIComponent(agentId)}/resume`,
    { method: "POST" },
  );
}

// ─── Autonomy & Memory Types ─────────────────────────────────────────────────

export type AutonomyLevel =
  | "suggest-only"
  | "auto-low"
  | "auto-medium"
  | "full-auto";

export interface AutonomyUpdateResponse {
  tenant_id: string;
  previous_level: string;
  new_level: string;
}

export interface MemoryEntry {
  memory_id: string;
  memory_type: "pattern" | "preference";
  content: string;
  tags: string[];
  created_at: string;
  tenant_id: string;
}

export interface MemoryFilters {
  tenant_id?: string;
  memory_type?: "pattern" | "preference";
  tags?: string;
  page?: number;
  size?: number;
}

export interface PaginatedMemories {
  entries: MemoryEntry[];
  total: number;
  page: number;
  size: number;
}

// ─── Autonomy Configuration Endpoints ────────────────────────────────────────

/** GET /agent/config/autonomy — get current autonomy level for a tenant */
export async function getAutonomyLevel(
  tenantId: string = "default",
): Promise<{ level: AutonomyLevel }> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return agentRequest<{ level: AutonomyLevel }>(`/config/autonomy${qs}`);
}

/** PATCH /agent/config/autonomy — update the autonomy level for a tenant */
export async function updateAutonomyLevel(
  level: string,
  tenantId: string = "default",
): Promise<AutonomyUpdateResponse> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return agentRequest<AutonomyUpdateResponse>(`/config/autonomy${qs}`, {
    method: "PATCH",
    body: JSON.stringify({ level }),
  });
}

// ─── Memory Management Endpoints ─────────────────────────────────────────────

/** GET /agent/memory — paginated list of agent memories with optional filters */
export async function getMemories(
  filters: MemoryFilters = {},
): Promise<PaginatedMemories> {
  const qs = buildQueryString({
    tenant_id: filters.tenant_id ?? "default",
    memory_type: filters.memory_type,
    tags: filters.tags,
    page: filters.page ?? 1,
    size: filters.size ?? 20,
  });
  return agentRequest<PaginatedMemories>(`/memory${qs}`);
}

/** DELETE /agent/memory/{id} — delete a specific memory entry */
export async function deleteMemory(
  memoryId: string,
  tenantId: string = "default",
): Promise<{ deleted: boolean }> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return agentRequest<{ deleted: boolean }>(
    `/memory/${encodeURIComponent(memoryId)}${qs}`,
    { method: "DELETE" },
  );
}

// ─── Feedback Types ──────────────────────────────────────────────────────────

export interface FeedbackEntry {
  feedback_id: string;
  action_id: string;
  feedback_type: "positive" | "negative" | "correction";
  comment?: string;
  created_at: string;
  tenant_id: string;
}

export interface FeedbackStats {
  tenant_id?: string;
  total_feedback: number;
  rejection_count: number;
  override_count: number;
  rejection_rate: number;
  rejections_per_agent?: Record<string, number>;
  common_action_types?: Record<string, number>;
  /** Derived fields for backward compat */
  total_actions?: number;
  approval_rate?: number;
}

export interface FeedbackFilters {
  tenant_id?: string;
  feedback_type?: string;
  start_date?: string;
  end_date?: string;
  page?: number;
  size?: number;
}

export interface PaginatedFeedback {
  entries: FeedbackEntry[];
  total: number;
  page: number;
  size: number;
}

// ─── Feedback Endpoints ──────────────────────────────────────────────────────

/** GET /agent/feedback — paginated feedback list with filters */
export async function getFeedback(
  filters: FeedbackFilters = {},
): Promise<PaginatedFeedback> {
  const qs = buildQueryString({
    tenant_id: filters.tenant_id ?? "default",
    feedback_type: filters.feedback_type,
    start_date: filters.start_date,
    end_date: filters.end_date,
    page: filters.page ?? 1,
    size: filters.size ?? 20,
  });
  return agentRequest<PaginatedFeedback>(`/feedback${qs}`);
}

/** GET /agent/feedback/stats — feedback statistics */
export async function getFeedbackStats(
  tenantId: string = "default",
): Promise<FeedbackStats> {
  const qs = buildQueryString({ tenant_id: tenantId });
  return agentRequest<FeedbackStats>(`/feedback/stats${qs}`);
}
