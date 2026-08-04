/**
 * Mobile_Session lifecycle — written fresh for this repository.
 *
 * Owns four things and nothing else:
 *
 *  1. **Credential storage.** Both the access token and the refresh token live
 *     in `expo-secure-store` (iOS Keychain / Android Keystore) and never in
 *     `react-native-mmkv`, which is unencrypted and carries only the query cache
 *     and the queue's own bookkeeping (Requirement 15.3).
 *  2. **The one in-flight refresh.** `refreshSession()` coalesces concurrent
 *     callers onto a single `POST /auth/driver/session/refresh`, so N
 *     simultaneous 401s produce one refresh and one replay each
 *     (Requirements 1.8, 1.9).
 *  3. **Sign-out.** Revokes the session at the server, then deletes the
 *     credential, the cached work list, cached customer names and phone
 *     numbers, and every queued POD artifact (Requirements 1.10, 15.5).
 *  4. **Observability for the UI** — a minimal subscription so the auth gate can
 *     react without a global store.
 *
 * No token value, no PIN, and no `Authorization` value is passed to any log
 * statement in any build configuration (Requirement 15.1).
 *
 * Requirements: 15.1, 15.3, 15.5, 1.1, 1.9, 1.10
 */

import * as SecureStore from 'expo-secure-store';

import {
  ApiError,
  NetworkError,
  TOKEN_EXPIRY_SKEW_MS,
  apiRequest,
  setApiClientSession,
  type SessionBridge,
} from './api-client';
import { demoPreviewEnabled } from './demo-preview';

const ACCESS_TOKEN_KEY = 'runsheet_driver_access_token';
const REFRESH_TOKEN_KEY = 'runsheet_driver_refresh_token';
const IDENTITY_KEY = 'runsheet_driver_session_identity';

const SIGN_IN_PATH = '/auth/driver/session';
const REFRESH_PATH = '/auth/driver/session/refresh';

/** Wire shape of `DriverSessionResponse` (`driver/api/session_endpoints.py`). */
interface DriverSessionResponseBody {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  driver_id: string;
  tenant_id: string;
}

/** The credential plus the identity it is scoped to. */
export interface DriverSession {
  accessToken: string;
  refreshToken: string;
  driverId: string;
  tenantId: string;
  /** Epoch milliseconds at which the access token stops being accepted. */
  expiresAt: number;
}

/** Identity half of the session, safe to render. */
export interface SessionIdentity {
  driverId: string;
  tenantId: string;
  expiresAt: number;
}

/**
 * On-device data that Requirement 15.5 requires sign-out to delete. Each owner
 * module registers its own eraser, so this module stays free of the queue, the
 * artifact store, and the query cache.
 */
export type SessionDataDomain =
  | 'work-cache'
  | 'customer-cache'
  | 'pod-artifacts'
  | 'mutation-queue'
  | 'pod-drafts';

const REQUIRED_PURGE_DOMAINS: SessionDataDomain[] = [
  'work-cache',
  'customer-cache',
  'pod-artifacts',
  'mutation-queue',
  'pod-drafts',
];

export type SessionPurgeHandler = () => void | Promise<void>;

export interface SignOutResult {
  /** The server confirmed revocation. `false` means offline or already invalid. */
  revoked: boolean;
  /** Domains whose eraser threw. The credential is deleted regardless. */
  failedDomains: SessionDataDomain[];
  /** Domains with no registered eraser — nothing of that kind is cached yet. */
  unhandledDomains: SessionDataDomain[];
}

export class SecureStoreUnavailableError extends Error {
  constructor() {
    super(
      'The operating system secure keystore is unavailable, so the driver ' +
        'session cannot be stored. Sign-in is refused rather than falling back ' +
        'to unencrypted storage.',
    );
    this.name = 'SecureStoreUnavailableError';
  }
}

// ---------------------------------------------------------------------------
// In-memory state
// ---------------------------------------------------------------------------

let current: DriverSession | null = null;
let hydrated = false;
let inFlightRefresh: Promise<string | null> | null = null;

const purgeHandlers = new Map<SessionDataDomain, SessionPurgeHandler>();
const listeners = new Set<(identity: SessionIdentity | null) => void>();

function identityOf(session: DriverSession | null): SessionIdentity | null {
  return session
    ? {
        driverId: session.driverId,
        tenantId: session.tenantId,
        expiresAt: session.expiresAt,
      }
    : null;
}

function publish(): void {
  const identity = identityOf(current);
  listeners.forEach((listener) => listener(identity));
}

/** Subscribe to sign-in / sign-out / refresh. Returns the unsubscribe function. */
export function subscribeToSession(
  listener: (identity: SessionIdentity | null) => void,
): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Identity of the session held in memory, without touching the keystore. */
export function currentSessionIdentity(): SessionIdentity | null {
  return identityOf(current);
}

/** Register the eraser for one on-device data domain (Requirement 15.5). */
export function registerSessionPurgeHandler(
  domain: SessionDataDomain,
  handler: SessionPurgeHandler,
): void {
  purgeHandlers.set(domain, handler);
}

/** Drop all registered erasers and the in-memory session. Tests only. */
export function resetSessionState(): void {
  current = null;
  hydrated = false;
  inFlightRefresh = null;
  purgeHandlers.clear();
  listeners.clear();
}

// ---------------------------------------------------------------------------
// Secure storage
// ---------------------------------------------------------------------------

async function assertSecureStoreAvailable(): Promise<void> {
  if (demoPreviewEnabled) {
    return;
  }
  const available = await SecureStore.isAvailableAsync();
  if (!available) {
    throw new SecureStoreUnavailableError();
  }
}

async function writeStoredSession(session: DriverSession): Promise<void> {
  if (demoPreviewEnabled) {
    return;
  }
  await assertSecureStoreAvailable();
  await Promise.all([
    SecureStore.setItemAsync(ACCESS_TOKEN_KEY, session.accessToken),
    SecureStore.setItemAsync(REFRESH_TOKEN_KEY, session.refreshToken),
    SecureStore.setItemAsync(
      IDENTITY_KEY,
      JSON.stringify({
        driverId: session.driverId,
        tenantId: session.tenantId,
        expiresAt: session.expiresAt,
      }),
    ),
  ]);
}

async function deleteStoredSession(): Promise<void> {
  if (demoPreviewEnabled) {
    return;
  }
  await Promise.all(
    [ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, IDENTITY_KEY].map((key) =>
      SecureStore.deleteItemAsync(key).catch(() => undefined),
    ),
  );
}

async function readStoredSession(): Promise<DriverSession | null> {
  if (demoPreviewEnabled) {
    return null;
  }
  const [accessToken, refreshToken, rawIdentity] = await Promise.all([
    SecureStore.getItemAsync(ACCESS_TOKEN_KEY),
    SecureStore.getItemAsync(REFRESH_TOKEN_KEY),
    SecureStore.getItemAsync(IDENTITY_KEY),
  ]);
  if (!accessToken || !refreshToken || !rawIdentity) {
    return null;
  }
  let identity: Partial<SessionIdentity>;
  try {
    identity = JSON.parse(rawIdentity) as Partial<SessionIdentity>;
  } catch {
    return null;
  }
  if (!identity.driverId || !identity.tenantId) {
    return null;
  }
  return {
    accessToken,
    refreshToken,
    driverId: identity.driverId,
    tenantId: identity.tenantId,
    expiresAt: typeof identity.expiresAt === 'number' ? identity.expiresAt : 0,
  };
}

function sessionFromResponse(body: DriverSessionResponseBody): DriverSession {
  return {
    accessToken: body.access_token,
    refreshToken: body.refresh_token,
    driverId: body.driver_id,
    tenantId: body.tenant_id,
    expiresAt: Date.now() + Math.max(0, body.expires_in) * 1000,
  };
}

async function adopt(session: DriverSession): Promise<DriverSession> {
  current = session;
  hydrated = true;
  await writeStoredSession(session);
  publish();
  return session;
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

/**
 * Read the stored credential into memory and install the API-client seam.
 *
 * Safe to call more than once; the keystore is read only on the first call.
 */
export async function initializeSession(): Promise<SessionIdentity | null> {
  setApiClientSession(sessionBridge);
  if (!hydrated) {
    current = await readStoredSession().catch(() => null);
    hydrated = true;
    publish();
  }
  return identityOf(current);
}

/**
 * Sign in with email and password.
 *
 * `POST /auth/driver/session` returns both tokens in the body, so nothing here
 * depends on reading `st-*` response headers (Requirement 1.1).
 */
export async function signIn(args: {
  email: string;
  password: string;
  deviceId?: string;
}): Promise<SessionIdentity> {
  setApiClientSession(sessionBridge);
  await assertSecureStoreAvailable();

  const body = await apiRequest<DriverSessionResponseBody>({
    method: 'POST',
    path: SIGN_IN_PATH,
    auth: false,
    body: {
      email: args.email.trim(),
      password: args.password,
      ...(args.deviceId ? { device_id: args.deviceId } : {}),
    },
  });

  const session = await adopt(sessionFromResponse(body));
  return identityOf(session) as SessionIdentity;
}

/**
 * Rotate the credential, at most once concurrently.
 *
 * Every caller within one refresh window receives the same promise, which is
 * what makes "concurrent 401s await the one refresh" true rather than aspirational.
 * Resolves to the replacement access token, or `null` when the session is
 * unrecoverable — in which case the stored credential is already deleted.
 */
export function refreshSession(): Promise<string | null> {
  if (inFlightRefresh) {
    return inFlightRefresh;
  }
  const attempt = performRefresh().finally(() => {
    if (inFlightRefresh === attempt) {
      inFlightRefresh = null;
    }
  });
  inFlightRefresh = attempt;
  return attempt;
}

async function performRefresh(): Promise<string | null> {
  if (!hydrated) {
    current = await readStoredSession().catch(() => null);
    hydrated = true;
  }
  const refreshToken = current?.refreshToken;
  if (!refreshToken) {
    return null;
  }

  try {
    const body = await apiRequest<DriverSessionResponseBody>({
      method: 'POST',
      path: REFRESH_PATH,
      auth: false,
      body: { refresh_token: refreshToken },
    });
    // SuperTokens rotates the refresh token, so both halves are replaced.
    const session = await adopt(sessionFromResponse(body));
    return session.accessToken;
  } catch (error) {
    if (error instanceof NetworkError) {
      // Offline: the credential may still be good once connectivity returns.
      return null;
    }
    if (error instanceof ApiError && error.status >= 500) {
      return null;
    }
    // The refresh token itself was rejected — the session is over.
    await forgetSessionLocally();
    return null;
  }
}

/** Erase the local credential without calling the server. */
async function forgetSessionLocally(): Promise<void> {
  current = null;
  hydrated = true;
  await deleteStoredSession();
  publish();
}

/**
 * Delete every on-device artifact of the session other than the credential
 * (Requirement 15.5). Used by sign-out and by the session-invalid path.
 */
async function purgeDeviceData(): Promise<{
  failedDomains: SessionDataDomain[];
  unhandledDomains: SessionDataDomain[];
}> {
  const failedDomains: SessionDataDomain[] = [];
  const unhandledDomains: SessionDataDomain[] = [];

  for (const domain of REQUIRED_PURGE_DOMAINS) {
    const handler = purgeHandlers.get(domain);
    if (!handler) {
      unhandledDomains.push(domain);
      continue;
    }
    try {
      await handler();
    } catch {
      // The specific failure is the owning module's to report; swallowing it
      // here guarantees the remaining domains and the credential are still
      // erased. Nothing about the failure can carry a token value.
      failedDomains.push(domain);
    }
  }
  return { failedDomains, unhandledDomains };
}

/**
 * Sign out.
 *
 * Order matters: revoke at the server first, because that call needs the
 * credential, then erase the cached data, then erase the credential. A failed
 * revocation (offline, or an already-invalid token) does not stop the local
 * erasure — the phone must not keep the data.
 */
export async function signOut(): Promise<SignOutResult> {
  let revoked = false;
  if (current) {
    try {
      await apiRequest({ method: 'DELETE', path: SIGN_IN_PATH });
      revoked = true;
    } catch {
      revoked = false;
    }
  }

  const { failedDomains, unhandledDomains } = await purgeDeviceData();
  await forgetSessionLocally();
  inFlightRefresh = null;

  return { revoked, failedDomains, unhandledDomains };
}

// ---------------------------------------------------------------------------
// The API-client seam
// ---------------------------------------------------------------------------

const sessionBridge: SessionBridge = {
  async getAccessToken(): Promise<string | null> {
    if (!hydrated) {
      current = await readStoredSession().catch(() => null);
      hydrated = true;
    }
    if (!current) {
      return null;
    }
    const expired =
      current.expiresAt > 0 && current.expiresAt - TOKEN_EXPIRY_SKEW_MS <= Date.now();
    if (expired) {
      // Pre-emptive rotation, through the same single in-flight promise.
      const replacement = await refreshSession();
      return replacement ?? current?.accessToken ?? null;
    }
    return current.accessToken;
  },

  refresh(): Promise<string | null> {
    return refreshSession();
  },

  async onSessionInvalid(): Promise<void> {
    await purgeDeviceData();
    await forgetSessionLocally();
  },
};

/** Exposed for tests that need to drive the seam directly. */
export function apiClientSessionBridge(): SessionBridge {
  return sessionBridge;
}
