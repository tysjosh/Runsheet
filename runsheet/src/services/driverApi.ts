/**
 * Upload helpers for driver-facing artifacts.
 *
 * Historically this module covered both POD submission and its
 * presigned-upload prerequisite, but POD submission now lives in the
 * native driver app that talks to the backend directly. The web
 * dispatcher UI only needs the presigned-upload primitives, which are
 * also used for compartment cleaning-event evidence photos
 * (`TruckCompartmentsPage`), so we keep:
 *
 *  • `POST /api/driver/pod/uploads/presign` — request a short-lived
 *    presigned PUT URL for a single artifact (signature, photo,
 *    meter-ticket, or BOL). See {@link presignPodUpload}.
 *  • `PUT <upload_url>` — upload the file bytes directly to S3 using
 *    the presigned URL; see {@link putPresignedFile}.
 *
 * These remain here rather than moving to a generic `uploadsApi.ts`
 * because the presigned-upload endpoint sits under the `/api/driver`
 * router in the backend (`Runsheet-backend/driver/api/pod_endpoints.py`)
 * and shares the POD-upload allowlist for category / content-type.
 *
 * Validates: Requirements 4.1.3, 4.1.5.
 */

import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
import { fetchWithTimeout, type PaginationMeta } from "./utils";

// Re-export the shared pagination metadata type so existing downstream imports
// keep resolving it from this module (Req 2.4/4.3).
export type { PaginationMeta } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// S3 uploads are plain HTTPS PUTs — bound the wait separately from the
// ops-API timeout because a large meter-ticket image can legitimately
// take longer than a metadata JSON exchange. 60s is a conservative
// default for a 10 MiB ceiling on residential broadband.
const UPLOAD_TIMEOUT_MS = 60_000;

// ─── Types ───────────────────────────────────────────────────────────────────

/**
 * Categories accepted by the presigned-upload endpoint.
 *
 * Mirrors `_POD_UPLOAD_CATEGORIES` in
 * `Runsheet-backend/driver/api/pod_endpoints.py`.
 */
export type PodUploadCategory = "signature" | "photo" | "meter_ticket" | "bol";

/**
 * MIME types permitted for POD uploads.
 *
 * Mirrors `_POD_UPLOAD_ALLOWED_MIME_TYPES` in
 * `Runsheet-backend/driver/api/pod_endpoints.py`.
 */
export type PodUploadContentType =
  | "image/jpeg"
  | "image/png"
  | "image/heic"
  | "application/pdf";

/**
 * Envelope returned by `POST /api/driver/pod/uploads/presign`.
 *
 * The server wraps the inner payload in a `{ data, request_id }` shape;
 * {@link presignPodUpload} returns the inner `data` object so callers do
 * not have to unwrap it repeatedly.
 */
export interface PresignResponse {
  /** Tenant-scoped S3 key — persist and pass back on POD submission. */
  file_ref: string;
  /** Presigned PUT URL; the client must target this with `fetch` directly. */
  upload_url: string;
  /** ISO 8601 UTC timestamp at which the presigned URL expires. */
  expires_at: string;
  /** MIME type that MUST be sent as the `Content-Type` header on PUT. */
  content_type: PodUploadContentType;
  /** Per-tenant upper bound on the file size (bytes). */
  max_file_bytes: number;
}

// ─── HTTP helpers (kept local so the client is self-contained) ───────────────

async function driverRequest<T>(
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

// ─── Endpoints ───────────────────────────────────────────────────────────────

/**
 * Request a presigned PUT URL for a POD-category artifact.
 *
 * Validates: Requirement 4.1.3.
 */
export async function presignPodUpload(
  category: PodUploadCategory,
  contentType: PodUploadContentType,
): Promise<PresignResponse> {
  const envelope = await driverRequest<{
    data: PresignResponse;
    request_id: string;
  }>("/driver/pod/uploads/presign", {
    method: "POST",
    body: JSON.stringify({ category, content_type: contentType }),
  });
  return envelope.data;
}

/**
 * Upload a file directly to a presigned URL.
 *
 * The server returns a short-lived presigned PUT URL that the client
 * uses to send the raw file bytes to S3 (or S3-compatible storage).
 * The `Content-Type` header MUST match the value passed to
 * {@link presignPodUpload}; the presigned URL is signed against that
 * header and S3 will reject any mismatch.
 *
 * Throws {@link ApiError} on non-2xx responses and
 * {@link ApiTimeoutError} on aborted requests.
 *
 * Validates: Requirements 4.1.3, 4.1.5.
 */
export async function putPresignedFile(
  uploadUrl: string,
  file: Blob,
  contentType: PodUploadContentType,
): Promise<void> {
  let response: Response;
  try {
    response = await fetchWithTimeout(
      uploadUrl,
      {
        method: "PUT",
        body: file,
        headers: {
          "Content-Type": contentType,
        },
      },
      UPLOAD_TIMEOUT_MS,
    );
  } catch (error) {
    if (error instanceof ApiTimeoutError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Upload failed",
      0,
    );
  }

  if (!response.ok) {
    // S3 returns XML error bodies; surface a short summary rather than
    // the full document so downstream toasts stay readable.
    throw new ApiError(
      `Upload failed (HTTP ${response.status})`,
      response.status,
    );
  }
}

// ─── Driver Communication Types ───────────────────────────────────────────────

/**
 * Field-exception categories a driver can report.
 *
 * Mirrors ``ExceptionType`` in :mod:`Runsheet-backend/driver/models.py`.
 */
export type ExceptionType =
  | "road_closure"
  | "vehicle_breakdown"
  | "customer_unavailable"
  | "access_denied"
  | "weather"
  | "cargo_damage"
  | "other";

/** Severity scale shared with the agent risk-signal pipeline. */
export type ExceptionSeverity = "low" | "medium" | "high" | "critical";

/** WGS-84 coordinate pair (note: backend uses ``lng``, not ``lon``). */
export interface DriverGeoPoint {
  lat: number;
  lng: number;
}

export interface ReportExceptionPayload {
  exception_type: ExceptionType;
  severity: ExceptionSeverity;
  note: string;
  location?: DriverGeoPoint;
  media_refs?: string[];
}

/** Stored exception document echoed back on report. */
export interface DriverException {
  exception_id: string;
  job_id: string;
  exception_type: ExceptionType;
  severity: ExceptionSeverity;
  note: string;
  location: DriverGeoPoint | null;
  media_refs: string[];
  tenant_id: string;
  timestamp: string;
}

export type MessageSenderRole = "driver" | "dispatcher";

export interface SendMessagePayload {
  body: string;
  sender_id: string;
  sender_role: MessageSenderRole;
}

export interface JobMessage {
  message_id: string;
  job_id: string;
  sender_id: string;
  sender_role: MessageSenderRole;
  body: string;
  timestamp: string;
  tenant_id: string;
}

/**
 * Proof-of-delivery submission body.
 *
 * Mirrors ``PODRequest`` in
 * :mod:`Runsheet-backend/driver/api/pod_endpoints.py`. Prefer the
 * ``*_ref`` fields (file_refs from {@link presignPodUpload}); the
 * ``*_url`` variants are deprecated. ``geotag`` and ``timestamp`` are
 * required. For a refused delivery, set ``refused_delivery: true`` and
 * supply ``refusal_reason_code``.
 */
export interface SubmitPODPayload {
  recipient_name: string;
  customer_id?: string;
  signature_ref?: string;
  photo_refs?: string[];
  meter_ticket_ref?: string;
  /** @deprecated use signature_ref */
  signature_url?: string;
  /** @deprecated use photo_refs */
  photo_urls?: string[];
  delivered_gallons?: number;
  geotag: DriverGeoPoint;
  /** ISO 8601 timestamp. */
  timestamp: string;
  otp?: string;
  refused_delivery?: boolean;
  refusal_reason_code?:
    | "customer_refused"
    | "customer_unavailable"
    | "access_denied"
    | "unsafe_site"
    | "wrong_product"
    | "insufficient_capacity"
    | "payment_hold"
    | "other";
  refusal_note?: string;
}

/** Stored POD document echoed back on submission (subset of fields). */
export interface ProofOfDelivery {
  pod_id: string;
  job_id: string;
  order_id?: string | null;
  recipient_name: string;
  customer_id?: string | null;
  signature_ref?: string | null;
  photo_refs?: string[];
  meter_ticket_ref?: string | null;
  delivered_gallons?: number | null;
  delivered_gallons_source?: string | null;
  geotag: { lat: number; lon: number };
  timestamp: string;
  otp_verified?: boolean;
  location_mismatch?: boolean;
  status: string;
  refused_delivery: boolean;
  refusal_reason_code?: string | null;
  refusal_note?: string | null;
  tenant_id: string;
  [key: string]: unknown;
}

// ─── Driver Communication Endpoints ────────────────────────────────────────────

/**
 * POST /api/driver/jobs/:jobId/exceptions — report a field exception.
 *
 * Persists the exception, appends an ``exception_reported`` event to the
 * job timeline, and (for high/critical severity) broadcasts an escalation
 * over the dispatcher and driver WebSocket channels. The submitting driver
 * must be the one assigned to the job (else 403).
 */
export async function reportException(
  jobId: string,
  payload: ReportExceptionPayload,
): Promise<{ data: DriverException; request_id: string }> {
  return driverRequest<{ data: DriverException; request_id: string }>(
    `/driver/jobs/${encodeURIComponent(jobId)}/exceptions`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/**
 * POST /api/driver/jobs/:jobId/messages — post a message to the job thread.
 *
 * The sender must be the assigned driver or a dispatcher for the tenant.
 */
export async function sendJobMessage(
  jobId: string,
  payload: SendMessagePayload,
): Promise<{ data: JobMessage; request_id: string }> {
  return driverRequest<{ data: JobMessage; request_id: string }>(
    `/driver/jobs/${encodeURIComponent(jobId)}/messages`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/**
 * GET /api/driver/jobs/:jobId/messages — list the job thread (ascending by
 * timestamp), paginated via ``page``/``size``.
 */
export async function listJobMessages(
  jobId: string,
  params: { page?: number; size?: number } = {},
): Promise<{
  data: JobMessage[];
  pagination: PaginationMeta;
  request_id: string;
}> {
  const search = new URLSearchParams();
  if (params.page != null) search.set("page", String(params.page));
  if (params.size != null) search.set("size", String(params.size));
  const qs = search.toString() ? `?${search.toString()}` : "";
  return driverRequest<{
    data: JobMessage[];
    pagination: PaginationMeta;
    request_id: string;
  }>(`/driver/jobs/${encodeURIComponent(jobId)}/messages${qs}`);
}

/**
 * POST /api/driver/jobs/:jobId/pod — submit proof of delivery.
 *
 * Validates geotag distance against the job destination and, when the tenant
 * has OTP enabled, the supplied OTP. When a ``meter_ticket_ref`` is provided
 * without ``delivered_gallons``, the server runs OCR to extract the gallon
 * count. Use {@link presignPodUpload} + {@link putPresignedFile} first to
 * obtain the ``*_ref`` values.
 */
export async function submitPOD(
  jobId: string,
  payload: SubmitPODPayload,
): Promise<{ data: ProofOfDelivery; request_id: string }> {
  return driverRequest<{ data: ProofOfDelivery; request_id: string }>(
    `/driver/jobs/${encodeURIComponent(jobId)}/pod`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}
