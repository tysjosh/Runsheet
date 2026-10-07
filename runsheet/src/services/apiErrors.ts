/**
 * Shared API error helpers.
 *
 * Every service client turns a non-OK response into an `ApiError` through
 * `apiErrorFromResponse`, so error text is extracted the same way everywhere
 * and can never degrade to "[object Object]". Detail pages use
 * `classifyLoadError` to decide between a not-found, module-disabled,
 * forbidden or generic error state.
 *
 * This module may import from `./api`, but `./api` must never import from
 * here (that would create a cycle).
 */

import { ApiError } from "./api";

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Returns the trimmed string, or undefined for non-strings / empty strings. */
function str(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

function codeOf(obj: JsonObject): string | undefined {
  return str(obj.error_code) ?? str(obj.code);
}

/** Joins FastAPI validation errors: `[{loc, msg, type}]` → "field: msg; …". */
function validationMessage(items: unknown[]): string | undefined {
  const parts: string[] = [];
  for (const item of items) {
    if (!isObject(item)) continue;
    const msg = str(item.msg);
    if (!msg) continue;
    const loc = Array.isArray(item.loc) ? item.loc : [];
    const last = loc.length > 0 ? loc[loc.length - 1] : undefined;
    const field =
      typeof last === "number" ? String(last) : str(last as unknown);
    parts.push(field ? `${field}: ${msg}` : msg);
  }
  return parts.length > 0 ? parts.join("; ") : undefined;
}

/**
 * Extracts a human-readable message from an API error body.
 *
 * Handles `{detail: string}`, `{detail: {message, error_code}}`, FastAPI
 * validation arrays, the AppException envelope `{error_code, message, …}`,
 * `{error: string | {message}}` and plain string bodies. Only string-typed
 * fields are ever returned, so the result is never "[object Object]".
 */
export function extractApiErrorMessage(
  body: unknown,
  fallback: string,
): string {
  const asString = str(body);
  if (asString) return asString;
  if (!isObject(body)) return fallback;

  const { detail } = body;
  const detailString = str(detail);
  if (detailString) return detailString;

  if (Array.isArray(detail)) {
    const joined = validationMessage(detail);
    if (joined) return joined;
  } else if (isObject(detail)) {
    const fromDetail =
      str(detail.message) ?? str(detail.msg) ?? str(detail.detail);
    if (fromDetail) return fromDetail;
    const detailCode = codeOf(detail);
    if (detailCode) return `API error: ${detailCode}`;
  }

  const message = str(body.message);
  if (message) return message;

  const { error } = body;
  const errorString = str(error);
  if (errorString) return errorString;
  if (isObject(error)) {
    const errorMessage = str(error.message);
    if (errorMessage) return errorMessage;
  }

  const topCode = codeOf(body);
  if (topCode) return `API error: ${topCode}`;

  return fallback;
}

/** Extracts a machine-readable error code (nested `detail` first, then top-level). */
export function extractApiErrorCode(body: unknown): string | undefined {
  if (!isObject(body)) return undefined;
  const nested = isObject(body.detail) ? codeOf(body.detail) : undefined;
  return nested ?? codeOf(body);
}

/**
 * The envelope's `details` object: top-level `details` (the AppException
 * envelope), else `detail.details`.
 */
export function extractApiErrorDetails(
  body: unknown,
): Record<string, unknown> | undefined {
  if (!isObject(body)) return undefined;
  if (isObject(body.details)) return body.details;
  if (isObject(body.detail) && isObject(body.detail.details)) {
    return body.detail.details;
  }
  return undefined;
}

/** Builds an `ApiError` from a non-OK response, tolerating non-JSON bodies. */
export async function apiErrorFromResponse(
  response: Response,
): Promise<ApiError> {
  let body: unknown = {};
  try {
    body = await response.json();
  } catch {
    // Non-JSON error body: fall through to the generic message.
  }
  return new ApiError(
    extractApiErrorMessage(body, `HTTP error! status: ${response.status}`),
    response.status,
    extractApiErrorCode(body),
    extractApiErrorDetails(body),
  );
}

// ─── Load-failure classification ─────────────────────────────────────────────

/** Display names for the module-disabled error codes the backend returns. */
export const MODULE_DISABLED_NAMES: Record<string, string> = {
  CUSTOMERS_DISABLED: "Customers",
  INVOICING_DISABLED: "Invoicing",
};

export type LoadErrorKind =
  | "not_found"
  | "module_disabled"
  | "forbidden"
  | "error";

export interface LoadFailure {
  kind: LoadErrorKind;
  message: string;
  status?: number;
  code?: string;
  moduleName?: string;
}

function moduleNameFor(code: string | undefined): string | undefined {
  if (!code) return undefined;
  if (MODULE_DISABLED_NAMES[code]) return MODULE_DISABLED_NAMES[code];
  const match = /^(.+)_DISABLED$/.exec(code);
  if (!match) return undefined;
  const words = match[1].split("_").filter(Boolean);
  if (words.length === 0) return undefined;
  return words
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(" ");
}

function usableMessage(err: unknown): string | undefined {
  if (!(err instanceof Error)) return undefined;
  const message = str(err.message);
  if (!message || message.includes("[object Object]")) return undefined;
  return message;
}

/**
 * Classifies a failure from a page's initial fetch.
 *
 * Uses duck typing on `status`/`code` rather than `instanceof ApiError` so it
 * still works in tests that replace `./api` with a partial mock.
 */
export function classifyLoadError(err: unknown, fallback: string): LoadFailure {
  const message = usableMessage(err) ?? fallback;
  const status =
    err instanceof Error && typeof (err as ApiError).status === "number"
      ? (err as ApiError).status
      : undefined;
  const code =
    err instanceof Error && typeof (err as ApiError).code === "string"
      ? (err as ApiError).code
      : undefined;

  if (status === 404) {
    const moduleName = moduleNameFor(code);
    if (moduleName) {
      return { kind: "module_disabled", message, status, code, moduleName };
    }
    return { kind: "not_found", message, status, code };
  }
  if (status === 403) {
    return { kind: "forbidden", message, status, code };
  }
  return { kind: "error", message, status, code };
}
