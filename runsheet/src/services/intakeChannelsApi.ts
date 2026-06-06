/**
 * Typed HTTP client for the Intake Channel Admin surface.
 *
 * Mirrors the backend contract defined in
 * :mod:`Runsheet-backend/integrations/api/intake_channel_endpoints.py`
 * for the IntakeChannelsAdminPanel. Follows the same pattern as
 * {@link fuelApi.ts} — local request helper with timeout + typed
 * generics, no runtime fetch changes.
 *
 * Security note: the HMAC secret is returned in plaintext EXACTLY ONCE
 * on create and rotate. The UI must surface it to the admin immediately
 * and never persist it. Subsequent list/get responses carry only the
 * opaque `hmac_secret_ref`.
 *
 * Validates: Requirements 2.1.1, 2.1.4, 2.1.6.
 */

import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { fetchWithTimeout } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── Types ───────────────────────────────────────────────────────────────────

export type IntakeChannelType =
  | "voice"
  | "web_portal"
  | "dispatcher"
  | "csv"
  | "edi"
  | "api_partner"
  | "legacy";

/**
 * An intake channel as returned by list/get endpoints. The HMAC secret
 * is NEVER included — only the opaque `hmac_secret_ref`.
 */
export interface IntakeChannel {
  channel_id: string;
  tenant_id: string;
  channel_type: IntakeChannelType;
  display_name: string;
  hmac_secret_ref: string;
  supported_schema_versions: string[];
  rate_limit_per_minute?: number | null;
  secret_version: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

/**
 * Response from create and rotate-secret — includes the plaintext
 * HMAC secret exactly once.
 */
export interface IntakeChannelWithSecret {
  channel_id: string;
  tenant_id: string;
  channel_type: IntakeChannelType;
  display_name: string;
  hmac_secret: string;
  hmac_secret_ref: string;
  supported_schema_versions: string[];
  rate_limit_per_minute?: number | null;
  secret_version: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

/**
 * Envelope for ``GET /api/integrations/intake-channels``.
 *
 * Mirrors the backend ``IntakeChannelListResponse`` — the list endpoint
 * returns ``{ items, total }`` (NOT the ``{ data, request_id }`` envelope
 * used by other surfaces).
 */
export interface IntakeChannelListResponse {
  items: IntakeChannel[];
  total: number;
}

export interface CreateIntakeChannelPayload {
  channel_id: string;
  channel_type: IntakeChannelType;
  display_name: string;
  supported_schema_versions: string[];
  rate_limit_per_minute?: number;
  enabled?: boolean;
}

export interface UpdateIntakeChannelPayload {
  display_name?: string;
  supported_schema_versions?: string[];
  rate_limit_per_minute?: number;
  enabled?: boolean;
}

// ─── HTTP Helpers ────────────────────────────────────────────────────────────

async function intakeChannelsRequest<T>(
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

    // 204 No Content has no body
    if (response.status === 204) return undefined as unknown as T;

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

// ─── Intake Channel Admin Endpoints ──────────────────────────────────────────

/**
 * POST /api/integrations/intake-channels — register a new intake channel.
 *
 * Returns the freshly minted HMAC secret EXACTLY ONCE. The admin must
 * copy it immediately; it will never be returned again.
 */
export async function createIntakeChannel(
  payload: CreateIntakeChannelPayload,
): Promise<IntakeChannelWithSecret> {
  return intakeChannelsRequest<IntakeChannelWithSecret>(
    "/integrations/intake-channels",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/**
 * GET /api/integrations/intake-channels — list all intake channels for
 * the caller's tenant. HMAC secrets are never included.
 */
export async function listIntakeChannels(): Promise<IntakeChannelListResponse> {
  return intakeChannelsRequest<IntakeChannelListResponse>(
    "/integrations/intake-channels",
  );
}

/**
 * PATCH /api/integrations/intake-channels/:channel_id — update an
 * existing intake channel's configuration.
 */
export async function updateIntakeChannel(
  channelId: string,
  payload: UpdateIntakeChannelPayload,
): Promise<IntakeChannel> {
  return intakeChannelsRequest<IntakeChannel>(
    `/integrations/intake-channels/${encodeURIComponent(channelId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

/**
 * DELETE /api/integrations/intake-channels/:channel_id — remove an
 * intake channel. Existing orders referencing this channel are not
 * affected; only future webhook deliveries will be rejected.
 */
export async function deleteIntakeChannel(channelId: string): Promise<void> {
  await intakeChannelsRequest<void>(
    `/integrations/intake-channels/${encodeURIComponent(channelId)}`,
    { method: "DELETE" },
  );
}

/**
 * POST /api/integrations/intake-channels/:channel_id/rotate-secret —
 * generate a new HMAC secret, invalidating the previous one within
 * 60 seconds.
 *
 * Returns the new plaintext secret EXACTLY ONCE.
 */
export async function rotateIntakeChannelSecret(
  channelId: string,
): Promise<IntakeChannelWithSecret> {
  return intakeChannelsRequest<IntakeChannelWithSecret>(
    `/integrations/intake-channels/${encodeURIComponent(channelId)}/rotate-secret`,
    { method: "POST" },
  );
}
