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

import { API_TIMEOUTS, ApiError, ApiTimeoutError } from "./api";
import { getAuthToken } from "../utils/auth";

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

async function driverRequest<T>(
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
      headers["Authorization"] = `Bearer ${token}`;
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
