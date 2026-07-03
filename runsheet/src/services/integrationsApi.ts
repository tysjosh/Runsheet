/**
 * Typed HTTP client for the Integration Marketplace.
 *
 * Mirrors the backend contract defined in
 * :mod:`Runsheet-backend/integrations/api/integrations_endpoints.py` and
 * :mod:`Runsheet-backend/integrations/api/stripe_endpoints.py` for the
 * Integration Marketplace UI (Fuel Ops Hardening, Capability 5,
 * Requirement 5.6). The Marketplace page consumes these helpers to:
 *
 *   • List the catalog of available providers (``GET /api/integrations/providers``)
 *   • List, create, update, and delete the tenant's configured
 *     IntegrationInstances (``/api/integrations`` + ``/{id}``)
 *   • Enable, disable, sync-now, and inspect sync-run history
 *   • Read Stripe's public config (publishable_key only — Req 5.5.2)
 *
 * Credentials: the create / update endpoints accept a ``credentials``
 * payload that the backend immediately unwraps into the
 * Tenant_Credentials_Vault. Plaintext is discarded on the server side
 * and NEVER returned — the response only surfaces the opaque
 * ``credentials_ref`` pointer and a derived ``credentials_status``
 * flag. Callers on this side must treat the ``credentials`` argument
 * the same way (never persist it anywhere except in transit).
 *
 * Validates: Requirements 5.6.1, 5.6.2, 5.6.3, 5.6.4, 5.6.5.
 */

import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { buildQueryString, fetchWithTimeout } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";

// ─── Shared Types ────────────────────────────────────────────────────────────

/**
 * Coarse grouping used by the Marketplace UI to organize the provider
 * list. Matches :data:`IntegrationCategory` on the backend.
 */
export type IntegrationCategory =
  | "accounting"
  | "tank_monitor"
  | "gps_eld"
  | "payment"
  | "tms"
  | "terminal_pricing";

/** Rolling health status of a configured :class:`IntegrationInstance`. */
export type IntegrationStatus =
  | "pending"
  | "connected"
  | "disconnected"
  | "error";

/** Hint to the UI on which connect flow to render. */
export type IntegrationAuthMode = "oauth2" | "api_key" | "basic" | "custom";

/** Opaque flag derived from ``credentials_ref`` presence. NOT a validity probe. */
export type CredentialsStatus = "valid" | "missing";

/** Direction of a Sync_Run — matches Req 5.1.4. */
export type SyncOperation = "pull" | "push";

/** Terminal status for a completed Sync_Run. */
export type SyncStatus = "running" | "success" | "partial" | "error";

// ─── Provider Catalog (Req 5.6.2) ────────────────────────────────────────────

/**
 * Catalog metadata for a single registered provider. Consumed by the
 * Marketplace to render the "Connect" card header.
 *
 * The ``required_credential_fields`` list is a SCHEMA — values are
 * never transported through this model (Req 5.1.8). The Marketplace UI
 * builds the connect form from this list.
 */
export interface ProviderCatalogEntry {
  provider_name: string;
  category: IntegrationCategory | string;
  description: string;
  required_credential_fields: string[];
  doc_url?: string | null;
  auth_mode: IntegrationAuthMode;
  feature_flag_key?: string | null;
  /**
   * Effective feature-flag key the Marketplace should check.
   * Defaults to ``overlay.integration.{provider_name}`` when the
   * adapter does not supply an override (Req 5.6.6).
   */
  effective_feature_flag_key: string;
}

export interface ProviderCatalogResponse {
  items: ProviderCatalogEntry[];
  total: number;
}

// ─── Integration Instance (Req 5.6.1, 5.6.3) ─────────────────────────────────

/**
 * An :class:`IntegrationInstance` as returned by ``GET /api/integrations``
 * and related endpoints. The backend never returns plaintext
 * credentials — ``credentials_ref`` is opaque, ``credentials_status``
 * is derived.
 */
export interface IntegrationInstance {
  instance_id: string;
  tenant_id: string;
  provider_name: string;
  category: IntegrationCategory | string;
  status: IntegrationStatus;
  enabled: boolean;
  credentials_ref?: string | null;
  credentials_status: CredentialsStatus;
  schedule_cron?: string | null;
  config: Record<string, unknown>;
  last_sync_at?: string | null;
  last_error?: string | null;
  retry_count: number;
  updated_at?: string | null;
  created_at?: string | null;
}

export interface IntegrationInstanceListResponse {
  items: IntegrationInstance[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

export interface IntegrationInstanceListFilters {
  provider_name?: string;
  category?: IntegrationCategory;
  enabled?: boolean;
  status?: IntegrationStatus;
  page?: number;
  page_size?: number;
}

/**
 * Body for ``POST /api/integrations``. The optional ``credentials``
 * dict is forwarded to the TenantCredentialsVault server-side and
 * discarded after wrapping — never persisted in plaintext, never
 * returned.
 */
export interface IntegrationInstanceCreatePayload {
  instance_id?: string;
  provider_name: string;
  category: IntegrationCategory | string;
  schedule_cron?: string;
  enabled?: boolean;
  config?: Record<string, unknown>;
  credentials?: Record<string, unknown>;
}

/**
 * Body for ``PATCH /api/integrations/{instance_id}``. Every field is
 * optional so the UI can send just the delta.
 */
export interface IntegrationInstanceUpdatePayload {
  schedule_cron?: string;
  enabled?: boolean;
  status?: IntegrationStatus;
  config?: Record<string, unknown>;
  credentials?: Record<string, unknown>;
}

// ─── Sync Runs (Req 5.6.4) ───────────────────────────────────────────────────

export interface SyncRun {
  run_id: string;
  tenant_id: string;
  instance_id: string;
  provider_name: string;
  operation: SyncOperation;
  started_at: string;
  finished_at?: string | null;
  status: SyncStatus;
  record_counts: Record<string, number>;
  error_details?: string | null;
  duration_ms?: number | null;
}

export interface SyncRunListResponse {
  items: SyncRun[];
  total: number;
}

// ─── Stripe public config (Req 5.5.2) ────────────────────────────────────────

export interface StripePublicConfigResponse {
  publishable_key: string;
}

// ─── HTTP Helpers ────────────────────────────────────────────────────────────

/**
 * Extract a human-readable error message from a FastAPI error envelope.
 *
 * FastAPI returns ``{detail: string}`` for most errors, and the
 * integrations router returns ``{detail: {error_code, message, ...}}``
 * structured error bodies. This helper normalizes both shapes so the
 * UI can render a single error string.
 */
function extractErrorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      const { message, error_code } = detail as {
        message?: unknown;
        error_code?: unknown;
      };
      if (typeof message === "string" && message.trim()) return message;
      if (typeof error_code === "string" && error_code.trim()) {
        return `API error: ${error_code}`;
      }
    }
  }
  return fallback;
}

async function integrationsRequest<T>(
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
      let body: unknown = {};
      try {
        body = await response.json();
      } catch {
        // Non-JSON error body — ignore and fall through to default message.
      }
      throw new ApiError(
        extractErrorMessage(body, `HTTP error! status: ${response.status}`),
        response.status,
      );
    }

    // 204 No Content has no body — return undefined as T.
    if (response.status === 204) return undefined as unknown as T;

    return (await response.json()) as T;
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

// ─── Provider Catalog Endpoint ───────────────────────────────────────────────

/**
 * GET ``/api/integrations/providers`` — return the platform's catalog
 * of available integration providers (Req 5.6.2).
 *
 * The Marketplace layers its own per-tenant ``overlay.integration.*``
 * feature-flag filter on top of the response so disabled providers are
 * hidden (Req 5.6.6).
 */
export async function listIntegrationProviders(): Promise<ProviderCatalogResponse> {
  return integrationsRequest<ProviderCatalogResponse>(
    "/integrations/providers",
  );
}

// ─── Instance CRUD Endpoints ─────────────────────────────────────────────────

/**
 * GET ``/api/integrations`` — paginated list of the caller's
 * :class:`IntegrationInstance` records (Req 5.1.7).
 *
 * The backend uses ``status`` as the query-parameter name; this client
 * serializes the corresponding TypeScript ``status`` field the same
 * way. Credentials are NEVER returned (Req 5.1.8).
 */
export async function listIntegrationInstances(
  filters: IntegrationInstanceListFilters = {},
): Promise<IntegrationInstanceListResponse> {
  const qs = buildQueryString({
    provider_name: filters.provider_name,
    category: filters.category,
    enabled: filters.enabled,
    status: filters.status,
    page: filters.page,
    page_size: filters.page_size,
  });
  return integrationsRequest<IntegrationInstanceListResponse>(
    `/integrations${qs}`,
  );
}

/**
 * POST ``/api/integrations`` — create a new :class:`IntegrationInstance`
 * for the caller's tenant (Req 5.1.7, 5.1.8).
 *
 * When ``credentials`` is supplied the server forwards it to the
 * TenantCredentialsVault, persists only the ``credentials_ref``, and
 * discards the plaintext. The response never echoes credential values.
 */
export async function createIntegrationInstance(
  payload: IntegrationInstanceCreatePayload,
): Promise<IntegrationInstance> {
  return integrationsRequest<IntegrationInstance>("/integrations", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/**
 * PATCH ``/api/integrations/{instance_id}`` — partial update of a
 * single :class:`IntegrationInstance` (Req 5.1.7). ``credentials`` is
 * handled the same way as on create.
 */
export async function updateIntegrationInstance(
  instanceId: string,
  payload: IntegrationInstanceUpdatePayload,
): Promise<IntegrationInstance> {
  return integrationsRequest<IntegrationInstance>(
    `/integrations/${encodeURIComponent(instanceId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

/**
 * DELETE ``/api/integrations/{instance_id}`` — disconnect the
 * integration and delete the instance record (Req 5.6.5 — the
 * Marketplace "disconnect" control wires through here).
 */
export async function deleteIntegrationInstance(
  instanceId: string,
): Promise<void> {
  await integrationsRequest<void>(
    `/integrations/${encodeURIComponent(instanceId)}`,
    { method: "DELETE" },
  );
}

/**
 * POST ``/api/integrations/{instance_id}/enable`` — flip ``enabled=true``
 * and schedule the cron job when a scheduler is wired (Req 5.6.5).
 */
export async function enableIntegrationInstance(
  instanceId: string,
): Promise<IntegrationInstance> {
  return integrationsRequest<IntegrationInstance>(
    `/integrations/${encodeURIComponent(instanceId)}/enable`,
    { method: "POST" },
  );
}

/**
 * POST ``/api/integrations/{instance_id}/disable`` — flip
 * ``enabled=false`` and unschedule (Req 5.6.5).
 */
export async function disableIntegrationInstance(
  instanceId: string,
): Promise<IntegrationInstance> {
  return integrationsRequest<IntegrationInstance>(
    `/integrations/${encodeURIComponent(instanceId)}/disable`,
    { method: "POST" },
  );
}

/**
 * POST ``/api/integrations/{instance_id}/sync-now`` — trigger an
 * immediate :class:`SyncRun` outside the cron schedule (Req 5.6.5).
 * Returns the terminal :class:`SyncRun`.
 */
export async function syncIntegrationNow(instanceId: string): Promise<SyncRun> {
  return integrationsRequest<SyncRun>(
    `/integrations/${encodeURIComponent(instanceId)}/sync-now`,
    { method: "POST" },
  );
}

/**
 * GET ``/api/integrations/{instance_id}/sync-runs`` — list the most-
 * recent :class:`SyncRun` records for the instance (Req 5.6.4).
 *
 * The backend caps the limit at 50 server-side; this helper clamps the
 * argument to that range on the client side as well so the UI never
 * asks for more than the API can return.
 */
export async function listSyncRuns(
  instanceId: string,
  limit = 10,
): Promise<SyncRunListResponse> {
  const clampedLimit = Math.max(1, Math.min(50, Math.floor(limit)));
  const qs = buildQueryString({ limit: clampedLimit });
  return integrationsRequest<SyncRunListResponse>(
    `/integrations/${encodeURIComponent(instanceId)}/sync-runs${qs}`,
  );
}

// ─── Stripe public config (Req 5.5.2) ────────────────────────────────────────

/**
 * GET ``/api/integrations/stripe/public-config`` — return the tenant's
 * Stripe ``publishable_key``. Never exposes the secret_key or
 * webhook_secret.
 *
 * Returns ``null`` when no Stripe integration is configured (HTTP 404)
 * so the Marketplace UI can surface a neutral "not configured" state
 * instead of an error banner.
 */
export async function getStripePublicConfig(): Promise<StripePublicConfigResponse | null> {
  try {
    return await integrationsRequest<StripePublicConfigResponse>(
      "/integrations/stripe/public-config",
    );
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

// ─── Helpers for UI assembly ─────────────────────────────────────────────────

/**
 * Human-readable label for an :class:`IntegrationCategory`. Kept in
 * the service layer (rather than the component) so both the
 * Marketplace page and any future card/pill renderers reach for the
 * same source of truth.
 */
export const INTEGRATION_CATEGORY_LABELS: Record<IntegrationCategory, string> =
  {
    accounting: "Accounting",
    tank_monitor: "Tank Monitoring",
    gps_eld: "GPS & ELD",
    payment: "Payments",
    tms: "TMS",
    terminal_pricing: "Terminal Pricing",
  };

/**
 * Stable ordering of categories for the Marketplace grouped view.
 * Unknown categories fall through to the end of the list.
 */
export const INTEGRATION_CATEGORY_ORDER: IntegrationCategory[] = [
  "accounting",
  "payment",
  "tank_monitor",
  "gps_eld",
  "tms",
  "terminal_pricing",
];

/**
 * Look up the single :class:`IntegrationInstance` for a provider in a
 * list — the backend permits at most one instance per provider per
 * tenant in practice, and the Marketplace card is built around that
 * assumption. Returns ``null`` when no matching instance exists.
 */
export function findInstanceForProvider(
  instances: IntegrationInstance[],
  providerName: string,
): IntegrationInstance | null {
  return (
    instances.find((instance) => instance.provider_name === providerName) ??
    null
  );
}

/**
 * Derive the Marketplace status badge label from a catalog entry +
 * its currently-configured instance (if any). See Req 5.6.1 for the
 * four states the page must surface: available, connected, error,
 * disabled.
 *
 * The rules match the backend contract:
 *   - No instance at all → "available"
 *   - Instance with rolling status "error"      → "error"
 *   - Instance with enabled=false               → "disabled"
 *   - Instance with status=connected / enabled  → "connected"
 *   - Anything else (pending / disconnected)    → "pending"
 */
export type MarketplaceStatus =
  | "available"
  | "connected"
  | "error"
  | "disabled"
  | "pending";

export function deriveMarketplaceStatus(
  instance: IntegrationInstance | null,
): MarketplaceStatus {
  if (!instance) return "available";
  if (instance.status === "error") return "error";
  if (!instance.enabled) return "disabled";
  if (instance.status === "connected") return "connected";
  return "pending";
}
