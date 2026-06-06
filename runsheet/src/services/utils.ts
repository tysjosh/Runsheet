/**
 * Shared utility functions for API services.
 *
 * Extracted to avoid duplication across opsApi, fuelApi, schedulingApi, agentApi.
 */

import { API_TIMEOUTS, ApiError, ApiTimeoutError } from "./api";

// Re-export the canonical error types so consumers can import error types and
// the shared HTTP helpers from a single module. `utils.ts` already depends on
// `./api`; the reverse import must never happen (it would create a circular
// dependency — `api.ts` owns these definitions and stays dependency-free of
// `utils.ts`).
export { ApiError, ApiTimeoutError };

// ─── Pagination / response envelope types ────────────────────────────────────
//
// Shared shapes for the paginated/single response envelopes returned by the
// typed service clients. Identical to the per-client copies they replace (see
// schedulingApi/fuelApi/inventoryApi/etc.), centralized here as the single
// source of truth.

/** Pagination metadata block returned alongside a paginated list. */
export interface PaginationMeta {
  page: number;
  size: number;
  total: number;
  total_pages: number;
}

/** A paginated list response envelope. */
export interface PaginatedResponse<T> {
  data: T[];
  pagination: PaginationMeta;
  request_id: string;
}

/** A single-entity response envelope. */
export interface SingleResponse<T> {
  data: T;
  request_id: string;
}

/**
 * Build a URL query string from a params object.
 * Filters out undefined, null, and empty string values.
 */
export function buildQueryString(
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

/**
 * Fetch with AbortController-based timeout.
 */
export async function fetchWithTimeout(
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

/**
 * Calculate exponential backoff delay with jitter.
 * Used by both WebSocket reconnection and API retry logic.
 */
export function calculateBackoffDelay(
  attempt: number,
  initialDelay: number,
  maxDelay: number,
  multiplier: number,
): number {
  const exponentialDelay = initialDelay * multiplier ** (attempt - 1);
  const cappedDelay = Math.min(exponentialDelay, maxDelay);
  const jitter = cappedDelay * 0.25 * (Math.random() * 2 - 1);
  return Math.floor(cappedDelay + jitter);
}

/**
 * User-friendly error messages mapped from HTTP status codes.
 */
export const HTTP_ERROR_MESSAGES: Record<number, string> = {
  400: "Invalid request. Please check your input.",
  401: "Session expired. Please sign in again.",
  403: "You don't have permission to perform this action.",
  404: "Resource not found.",
  408: "Request timed out. Please try again.",
  429: "Too many requests. Please wait a moment.",
  500: "Server error. Please try again later.",
  502: "Service temporarily unavailable.",
  503: "Service temporarily unavailable.",
  504: "Request timed out. Please try again.",
};

/**
 * Get a user-friendly error message for an HTTP status code.
 */
export function getUserFriendlyError(status: number): string {
  return (
    HTTP_ERROR_MESSAGES[status] ??
    `Unexpected error (${status}). Please try again.`
  );
}
