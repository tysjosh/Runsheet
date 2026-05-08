/**
 * Driver-facing API client.
 *
 * Covers the presigned-upload and POD submission endpoints added by the
 * Fuel Ops Hardening spec (Capability 4 — POD + Reconciliation):
 *
 *  • `POST /api/driver/pod/uploads/presign` — request a short-lived
 *    presigned PUT URL for a single POD artifact (signature, photo,
 *    meter-ticket, or BOL). See {@link presignPodUpload}.
 *  • `PUT <upload_url>` — upload the file bytes directly to S3 using the
 *    presigned URL; see {@link putPresignedFile}.
 *  • `POST /api/driver/jobs/{job_id}/pod` — submit the POD once every
 *    artifact has been uploaded; see {@link submitPOD}.
 *
 * Kept in a separate module from `fuelApi.ts` because it targets the
 * driver-facing `/api/driver` router rather than the dispatcher-facing
 * `/api/fuel` router, and because it needs to send raw binary bodies to
 * externally-hosted presigned URLs (S3) rather than only the tenant's
 * backend. Style matches `fuelApi.ts` — same `fetchWithTimeout`,
 * `ApiError`, and `buildQueryString` helpers so the two clients feel
 * consistent.
 *
 * Validates: Requirements 4.1.3, 4.1.4, 4.1.5, 4.1.6, 4.2.4, 4.2.5, 4.2.6.
 */

import { API_TIMEOUTS, ApiError, ApiTimeoutError } from "./api";

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

/** WGS 84 coordinate pair — matches the backend `GeoPoint` (`lat`, `lng`). */
export interface GeoPoint {
  lat: number;
  lng: number;
}

/**
 * Body for `POST /api/driver/jobs/{job_id}/pod`.
 *
 * Mirrors the backend `PODRequest` model. The `*_ref` fields are the
 * preferred way to attach artifacts; they are `file_ref`s returned by
 * {@link presignPodUpload}. The legacy `*_url` fields are intentionally
 * omitted from this client — new driver UIs MUST upload via presigned
 * URLs so the server can enforce tenant isolation.
 */
export interface DriverPODRequest {
  recipient_name: string;
  /** `file_ref` from the signature presigned upload. */
  signature_ref: string;
  /** `file_ref`s from the photo presigned uploads (one or more). */
  photo_refs: string[];
  /** Optional `file_ref` from the meter-ticket presigned upload. */
  meter_ticket_ref?: string;
  /**
   * Driver-entered gallon count. When omitted and `meter_ticket_ref` is
   * supplied, the server runs OCR. When provided, the value is treated
   * as authoritative (`delivered_gallons_source = "manual"`).
   */
  delivered_gallons?: number;
  geotag: GeoPoint;
  /** ISO 8601 timestamp of the delivery. */
  timestamp: string;
  /** Optional one-time-password supplied by the customer. */
  otp?: string;
}

/** Subset of the persisted POD document returned in the submit response. */
export interface DriverPODResult {
  pod_id: string;
  job_id: string;
  recipient_name: string;
  signature_ref: string | null;
  photo_refs: string[];
  meter_ticket_ref: string | null;
  delivered_gallons: number | null;
  /** `"manual"` when the driver typed the value or OCR fell back. */
  delivered_gallons_source: "manual" | "ocr";
  /** Set by the server when the meter-ticket OCR produced a result. */
  ocr_result_id?: string | null;
  /** Confidence score [0.0, 1.0] from the OCR provider, if attempted. */
  ocr_confidence?: number | null;
  /**
   * `true` when the OCR pipeline needs driver confirmation (low
   * confidence or ambiguous extraction).
   */
  ocr_requires_manual_review?: boolean | null;
  /** Short diagnostic string when OCR failed/timed out. */
  ocr_error?: string | null;
  geotag: { lat: number; lon: number };
  timestamp: string;
  otp_verified: boolean;
  location_mismatch: boolean;
  status: string;
  tenant_id: string;
  pod_hash?: string;
  previous_pod_hash?: string;
  chain_sequence?: number;
}

/** Envelope for the POD submission response. */
export interface PODSubmissionResponse {
  data: DriverPODResult;
  request_id: string;
}

// ─── HTTP helpers (kept local so the driver client is self-contained) ────────

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
  params: Record<string, string | number | boolean | undefined | null> | object,
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

async function driverRequest<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
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

// ─── Endpoints ───────────────────────────────────────────────────────────────

/**
 * Request a presigned PUT URL for a POD artifact.
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

/**
 * Submit a POD for a job.
 *
 * All artifact `file_ref`s must already be uploaded via
 * {@link putPresignedFile}; this call only persists the POD record and
 * triggers downstream reconciliation / BOL generation on the server.
 *
 * Validates: Requirements 4.1.4, 4.2.4, 4.2.5.
 */
export async function submitPOD(
  jobId: string,
  payload: DriverPODRequest,
  idempotencyKey?: string,
): Promise<PODSubmissionResponse> {
  const headers: Record<string, string> = {};
  if (idempotencyKey) {
    headers["Idempotency-Key"] = idempotencyKey;
  }
  return driverRequest<PODSubmissionResponse>(
    `/driver/jobs/${encodeURIComponent(jobId)}/pod`,
    {
      method: "POST",
      body: JSON.stringify(payload),
      headers,
    },
  );
}

// ─── Utilities re-exported for consumers (mirrors fuelApi.ts style) ──────────

export { buildQueryString };
