/**
 * Runsheet driver API client — written fresh for this repository.
 *
 * This module is deliberately **not** copied, forked, or adapted from
 * `azumi-rider/lib/api-client.ts`. That module builds `Authorization: Bearer
 * ${token}` and prints the header block plus the request and response bodies to
 * the console on every call, which Requirements 15.1 and 15.2 forbid. It is not
 * one of the six donor artifacts, so the leak never enters this tree.
 *
 * Contract implemented here:
 *   - TLS only: every request URL must be `https://` (Requirement 15.4).
 *   - No log statement in any build configuration carries a session token, a
 *     refresh token, a PIN, or an `Authorization` value (Requirement 15.1), and
 *     no request or response body is logged in a release build
 *     (Requirement 15.2) — release builds log nothing at all from here.
 *   - A 401 is intercepted *above* the offline queue's disposition matrix: the
 *     client awaits the one in-flight refresh owned by `lib/session.ts`, then
 *     replays the request once with the same idempotency key. Only a 401 that
 *     survives a successful refresh is returned to the caller as terminal
 *     (Requirements 1.8, 1.9, 11.14).
 *
 * Requirements: 15.1, 15.2, 15.4, 1.1, 1.9, 1.10
 */

/** Header the Runsheet driver surface reads the idempotency key from. */
export const IDEMPOTENCY_HEADER = 'X-Idempotency-Key';

/** Header the backend echoes the correlation id on. */
export const REQUEST_ID_HEADER = 'x-request-id';

const DEFAULT_TIMEOUT_MS = 20_000;

/** Refresh this many milliseconds before the stored expiry, to absorb clock skew. */
export const TOKEN_EXPIRY_SKEW_MS = 30_000;

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/**
 * The structured `AppException` envelope every driver-surface error carries
 * (Requirement 15.10).
 */
export interface ApiErrorEnvelope {
  error_code?: string;
  message?: string;
  details?: unknown;
  request_id?: string;
}

/** A response was received and its status was not 2xx. */
export class ApiError extends Error {
  readonly status: number;
  readonly errorCode: string;
  readonly details: unknown;
  readonly requestId?: string;

  constructor(args: {
    status: number;
    errorCode: string;
    message: string;
    details?: unknown;
    requestId?: string;
  }) {
    super(args.message);
    this.name = 'ApiError';
    this.status = args.status;
    this.errorCode = args.errorCode;
    this.details = args.details;
    this.requestId = args.requestId;
  }
}

/** No response was received at all — offline, DNS failure, or a timeout. */
export class NetworkError extends Error {
  constructor(message = 'The request did not reach the Runsheet backend') {
    super(message);
    this.name = 'NetworkError';
  }
}

/** The configured base URL is missing or is not TLS. */
export class InsecureBaseUrlError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'InsecureBaseUrlError';
  }
}

/**
 * The seam `lib/session.ts` fills in. The client never reads the secure store
 * itself and never owns the refresh promise, so there is exactly one in-flight
 * refresh for the whole app.
 */
export interface SessionBridge {
  /** Current access token, or `null` when there is no usable session. */
  getAccessToken(): Promise<string | null>;
  /**
   * Refresh once. Concurrent callers receive the *same* promise, so N
   * simultaneous 401s produce one `POST /auth/driver/session/refresh`.
   * Resolves to the replacement access token, or `null` when the session is
   * unrecoverable.
   */
  refresh(): Promise<string | null>;
  /** Called when a 401 survives a refresh attempt. */
  onSessionInvalid(): void | Promise<void>;
}

export interface ApiClientConfig {
  /** Absolute `https://` origin, no trailing slash required. */
  baseUrl?: string;
  session?: SessionBridge | null;
  /** Injectable for tests; defaults to the platform `fetch`. */
  fetchImpl?: typeof fetch;
  defaultTimeoutMs?: number;
}

export interface ApiRequestOptions {
  method: HttpMethod;
  /** Path relative to the base URL, e.g. `/api/driver/orders`. */
  path?: string;
  /** Absolute `https://` URL, for presigned artifact uploads. Wins over `path`. */
  absoluteUrl?: string;
  /** JSON-serialized request body. */
  body?: unknown;
  /** Pre-encoded body (artifact bytes). Wins over `body`. */
  rawBody?: BodyInit;
  contentType?: string;
  /** Reused verbatim on the post-refresh replay (Requirement 11.6). */
  idempotencyKey?: string;
  headers?: Record<string, string>;
  /** Attach the Bearer session. Defaults to `true`. */
  auth?: boolean;
  signal?: AbortSignal;
  timeoutMs?: number;
}

/** Outcome shape the offline queue's disposition matrix consumes. */
export type ApiSendResult =
  | {
      kind: 'response';
      status: number;
      ok: boolean;
      errorCode: string | null;
      data: unknown;
      requestId?: string;
    }
  | { kind: 'no_response' };

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

interface ResolvedConfig {
  baseUrl: string | null;
  session: SessionBridge | null;
  fetchImpl: typeof fetch | null;
  defaultTimeoutMs: number;
}

function envBaseUrl(): string | null {
  // `EXPO_PUBLIC_*` values are inlined at build time by the Expo bundler.
  const raw = process.env.EXPO_PUBLIC_API_BASE_URL;
  return raw && raw.trim().length > 0 ? raw.trim() : null;
}

const config: ResolvedConfig = {
  baseUrl: envBaseUrl(),
  session: null,
  fetchImpl: null,
  defaultTimeoutMs: DEFAULT_TIMEOUT_MS,
};

/** Merge configuration. Only the supplied keys change. */
export function configureApiClient(next: ApiClientConfig): void {
  if (next.baseUrl !== undefined) {
    config.baseUrl = next.baseUrl ? next.baseUrl.trim().replace(/\/+$/, '') : null;
  }
  if (next.session !== undefined) {
    config.session = next.session ?? null;
  }
  if (next.fetchImpl !== undefined) {
    config.fetchImpl = next.fetchImpl ?? null;
  }
  if (next.defaultTimeoutMs !== undefined) {
    config.defaultTimeoutMs = next.defaultTimeoutMs;
  }
}

/** Install the session seam. Called by `lib/session.ts`, never by a screen. */
export function setApiClientSession(session: SessionBridge | null): void {
  config.session = session;
}

/** Restore module defaults. Tests only. */
export function resetApiClient(): void {
  config.baseUrl = envBaseUrl();
  config.session = null;
  config.fetchImpl = null;
  config.defaultTimeoutMs = DEFAULT_TIMEOUT_MS;
}

/**
 * The configured origin, validated as TLS.
 *
 * @throws {InsecureBaseUrlError} when unset or not `https://` (Requirement 15.4).
 */
export function apiBaseUrl(): string {
  if (!config.baseUrl) {
    throw new InsecureBaseUrlError(
      'EXPO_PUBLIC_API_BASE_URL is not set. The driver app has no backend origin to call.',
    );
  }
  return assertTls(config.baseUrl);
}

/**
 * Requirement 15.4 — the app talks to the backend exclusively over TLS. There
 * is no `__DEV__` escape hatch: a local backend is reached over an HTTPS tunnel.
 */
export function assertTls(url: string): string {
  if (!/^https:\/\//i.test(url)) {
    throw new InsecureBaseUrlError(
      'The Runsheet driver app communicates over TLS only; a non-https URL was supplied.',
    );
  }
  return url;
}

function resolveFetch(): typeof fetch {
  if (config.fetchImpl) {
    return config.fetchImpl;
  }
  if (typeof fetch === 'function') {
    return fetch;
  }
  throw new Error('No fetch implementation is available in this environment');
}

// ---------------------------------------------------------------------------
// Logging — redacted by construction
// ---------------------------------------------------------------------------

/**
 * The only log statement in this module.
 *
 * It carries the method, the path, the status, and the elapsed milliseconds and
 * nothing else: no headers (so no `Authorization` value, no token), no request
 * body, no response body (Requirements 15.1, 15.2). It is a no-op outside a
 * development build, so a release build logs nothing from the transport at all.
 */
function logRequestOutcome(
  method: HttpMethod,
  path: string,
  status: number | 'no-response',
  elapsedMs: number,
): void {
  if (!__DEV__) {
    return;
  }
  // eslint-disable-next-line no-console
  console.log(`[api] ${method} ${path} -> ${status} (${Math.round(elapsedMs)}ms)`);
}

// ---------------------------------------------------------------------------
// Request execution
// ---------------------------------------------------------------------------

function buildUrl(options: ApiRequestOptions): string {
  if (options.absoluteUrl) {
    return assertTls(options.absoluteUrl);
  }
  const path = options.path ?? '/';
  return `${apiBaseUrl()}${path.startsWith('/') ? path : `/${path}`}`;
}

async function buildHeaders(
  options: ApiRequestOptions,
  accessTokenOverride: string | null,
): Promise<{ headers: Record<string, string>; authorized: boolean }> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(options.headers ?? {}),
  };

  const hasJsonBody = options.rawBody === undefined && options.body !== undefined;
  if (hasJsonBody) {
    headers['Content-Type'] = options.contentType ?? 'application/json';
  } else if (options.rawBody !== undefined && options.contentType) {
    headers['Content-Type'] = options.contentType;
  }

  if (options.idempotencyKey) {
    headers[IDEMPOTENCY_HEADER] = options.idempotencyKey;
  }

  const wantsAuth = options.auth !== false;
  let authorized = false;
  if (wantsAuth) {
    const token = accessTokenOverride ?? (await config.session?.getAccessToken()) ?? null;
    if (token) {
      // Built here and never logged anywhere (Requirement 15.1).
      headers.Authorization = `Bearer ${token}`;
      authorized = true;
    }
  }
  return { headers, authorized };
}

function serializeBody(options: ApiRequestOptions): BodyInit | undefined {
  if (options.rawBody !== undefined) {
    return options.rawBody;
  }
  if (options.body === undefined) {
    return undefined;
  }
  return JSON.stringify(options.body);
}

interface RawOutcome {
  status: number;
  ok: boolean;
  data: unknown;
  requestId?: string;
}

function envelopeOf(data: unknown): ApiErrorEnvelope {
  return data && typeof data === 'object' ? (data as ApiErrorEnvelope) : {};
}

/** Best-effort error code, tolerating FastAPI's bare `{"detail": ...}` shape. */
export function errorCodeOf(status: number, data: unknown): string {
  const envelope = envelopeOf(data);
  if (typeof envelope.error_code === 'string' && envelope.error_code.length > 0) {
    return envelope.error_code;
  }
  return status === 401 ? 'SESSION_EXPIRED' : 'UNEXPECTED_ERROR';
}

function messageOf(status: number, data: unknown): string {
  const envelope = envelopeOf(data);
  if (typeof envelope.message === 'string' && envelope.message.length > 0) {
    return envelope.message;
  }
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string' && detail.length > 0) {
    return detail;
  }
  return `Request failed with status ${status}`;
}

async function sendOnce(
  options: ApiRequestOptions,
  accessTokenOverride: string | null,
): Promise<RawOutcome> {
  const url = buildUrl(options);
  const { headers } = await buildHeaders(options, accessTokenOverride);
  const timeoutMs = options.timeoutMs ?? config.defaultTimeoutMs;

  const controller = new AbortController();
  const abortFromCaller = () => controller.abort();
  if (options.signal) {
    if (options.signal.aborted) {
      controller.abort();
    } else {
      options.signal.addEventListener('abort', abortFromCaller);
    }
  }
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  const startedAt = Date.now();
  const logPath = options.absoluteUrl ? '<presigned>' : (options.path ?? '/');

  try {
    const response = await resolveFetch()(url, {
      method: options.method,
      headers,
      body: serializeBody(options),
      signal: controller.signal,
    });

    const text = await response.text().catch(() => '');
    let data: unknown = null;
    if (text.length > 0) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }

    logRequestOutcome(options.method, logPath, response.status, Date.now() - startedAt);

    return {
      status: response.status,
      ok: response.ok,
      data,
      requestId:
        response.headers?.get?.(REQUEST_ID_HEADER) ??
        envelopeOf(data).request_id ??
        undefined,
    };
  } catch (error) {
    logRequestOutcome(options.method, logPath, 'no-response', Date.now() - startedAt);
    if (error instanceof InsecureBaseUrlError) {
      throw error;
    }
    // The message of a fetch rejection carries the URL, never a header value.
    throw new NetworkError();
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener?.('abort', abortFromCaller);
  }
}

/**
 * Execute a request, intercepting 401 above the caller.
 *
 * A 401 on an authorized request triggers the one in-flight refresh and exactly
 * one replay under the *same* idempotency key. A second 401 is returned to the
 * caller and the session is reported invalid.
 */
async function execute(options: ApiRequestOptions): Promise<RawOutcome> {
  const first = await sendOnce(options, null);
  if (first.status !== 401 || options.auth === false) {
    return first;
  }

  const session = config.session;
  if (!session) {
    return first;
  }

  const replacementToken = await session.refresh();
  if (!replacementToken) {
    await session.onSessionInvalid();
    return first;
  }

  const replayed = await sendOnce(options, replacementToken);
  if (replayed.status === 401) {
    await session.onSessionInvalid();
  }
  return replayed;
}

/**
 * Throwing entry point for screens and queries.
 *
 * @throws {ApiError} on a non-2xx response.
 * @throws {NetworkError} when no response arrives.
 */
export async function apiRequest<T = unknown>(options: ApiRequestOptions): Promise<T> {
  const outcome = await execute(options);
  if (!outcome.ok) {
    throw new ApiError({
      status: outcome.status,
      errorCode: errorCodeOf(outcome.status, outcome.data),
      message: messageOf(outcome.status, outcome.data),
      details: envelopeOf(outcome.data).details,
      requestId: outcome.requestId,
    });
  }
  return outcome.data as T;
}

/**
 * Non-throwing entry point for the offline mutation queue.
 *
 * Every HTTP status is returned rather than raised, so the queue's disposition
 * matrix can classify it. A missing response is `{ kind: 'no_response' }`, which
 * the queue treats as "stay pending". Because `execute` already refreshed and
 * replayed, a 401 reaching here is terminal.
 */
export async function apiSend(options: ApiRequestOptions): Promise<ApiSendResult> {
  try {
    const outcome = await execute(options);
    return {
      kind: 'response',
      status: outcome.status,
      ok: outcome.ok,
      errorCode: outcome.ok ? null : errorCodeOf(outcome.status, outcome.data),
      data: outcome.data,
      requestId: outcome.requestId,
    };
  } catch (error) {
    if (error instanceof NetworkError) {
      return { kind: 'no_response' };
    }
    throw error;
  }
}
