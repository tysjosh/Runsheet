/**
 * Ops API client — platform monitoring and per-tenant rollout administration.
 *
 * Scope note: this module used to also wrap the pre-pivot Nigerian last-mile
 * read model (shipments, riders, events, failure/SLA/rider metrics, Dinee
 * replay + drift detection). Every one of those routes sits behind
 * `require_ops_enabled` in `ops/api/endpoints.py`, which raises
 * `LEGACY_NG_DELIVERY_DISABLED` while `LEGACY_NG_DELIVERY_ENABLED` is false —
 * the default in every environment. Those wrappers and the UI that called them
 * were deleted rather than left as callable dead code.
 *
 * What remains is exactly the part of `/api/ops/*` that is deliberately
 * exempt from that gate, so operators can observe and manage a disabled
 * surface (audit reference: product-owner-audit-2026-05-08 recommendation #1):
 *
 * - `GET  /ops/monitoring/{ingestion,indexing,poison-queue}`
 * - `GET  /ops/metrics/prometheus`
 * - `POST /ops/admin/feature-flags/:tenantId/{enable,disable,rollback}`
 */

import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { buildQueryString, fetchWithTimeout } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";

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
