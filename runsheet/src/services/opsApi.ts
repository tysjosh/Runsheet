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
 * What remains is the one route the UI calls:
 *
 * - `GET  /ops/monitoring/poison-queue` (`platform_admin` only), shown in
 *   Settings → System health (UI revamp task 3.9)
 *
 * `/ops/monitoring/{ingestion,indexing}` were deleted on the backend (they
 * queried Elasticsearch indices dropped by migration 0007). The
 * `/ops/metrics/prometheus` and `/ops/admin/feature-flags/*` routes still
 * exist for scrapers and operators, but no UI called their wrappers, so the
 * wrappers were removed (task 3.9).
 */

import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { apiErrorFromResponse } from "./apiErrors";
import { fetchWithTimeout } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";

// ─── Monitoring Types ────────────────────────────────────────────────────────

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
      throw await apiErrorFromResponse(response);
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

/** GET /ops/monitoring/poison-queue — poison queue stats (platform_admin) */
export async function getPoisonQueueMonitoring(): Promise<PoisonQueueMetrics> {
  return opsRequest<PoisonQueueMetrics>("/ops/monitoring/poison-queue");
}
