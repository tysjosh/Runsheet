/**
 * The Runsheet **web app** origin, and the pages on it the driver app links out
 * to.
 *
 * The origin is not configured in this app. It is fetched from the backend's
 * unauthenticated `GET /api/auth/public-config`, which publishes
 * `settings.supertokens_website_domain` — the value
 * `InputAppInfo(website_domain=...)` is built from, and therefore the origin
 * SuperTokens mints password-reset links against. Asking the backend is the
 * whole point: a build-time copy in this app (there used to be one,
 * `EXPO_PUBLIC_WEB_BASE_URL`) is a second source of truth for one value and can
 * drift from the authoritative one, sending the driver to a host that cannot
 * service the reset token.
 *
 * This is deliberately separate from `EXPO_PUBLIC_API_BASE_URL` in
 * `lib/api-client.ts`: that value is the *backend* origin, and the web app is a
 * different host in general, so the web origin cannot be derived from it.
 *
 * The origin is TLS-only, matching the transport rule the API client enforces
 * (Requirement 15.4). A value that is absent, blank, or not `https://` — and a
 * config call that fails or never answers — yields `null` rather than throwing:
 * this is read by the pre-auth sign-in screen, which hides the affordance
 * instead of rendering a dead link, and must never block signing in.
 */

import { InsecureBaseUrlError, apiRequest, assertTls } from './api-client';

/** Path of the web app's self-service password reset page. */
export const FORGOT_PASSWORD_PATH = '/auth/forgot-password';

/** Backend route publishing the authoritative web app origin. */
export const PUBLIC_CONFIG_PATH = '/api/auth/public-config';

/** Shape of the `GET /api/auth/public-config` body. One field, by design. */
interface PublicConfigBody {
  website_domain?: string | null;
}

/**
 * Resolved once per app session. The origin is deployment configuration, so it
 * does not change under a running app; caching the promise means N renders of
 * the sign-in screen share one request rather than issuing one each.
 */
let resolved: Promise<string | null> | null = null;

/** Drop the cached origin so the next read re-fetches. Tests only. */
export function resetWebAppConfig(): void {
  resolved = null;
}

/** Normalize the origin the backend reported, or `null` if it is unusable. */
function normalizeOrigin(raw: unknown): string | null {
  const trimmed = typeof raw === 'string' ? raw.trim().replace(/\/+$/, '') : '';
  if (trimmed.length === 0) {
    return null;
  }
  try {
    return assertTls(trimmed);
  } catch (error) {
    if (error instanceof InsecureBaseUrlError) {
      return null;
    }
    throw error;
  }
}

async function fetchWebBaseUrl(): Promise<string | null> {
  // `auth: false` — the endpoint is on the backend's Public_Route_Allowlist and
  // is read before there is any session to attach.
  const body = await apiRequest<PublicConfigBody>({
    method: 'GET',
    path: PUBLIC_CONFIG_PATH,
    auth: false,
  });
  return normalizeOrigin(body?.website_domain);
}

/**
 * The web app origin the backend reports, or `null` when it is unset, not TLS,
 * or could not be fetched.
 *
 * A failed call resolves to `null` and is **not** cached, so a transient network
 * failure hides the link for this attempt rather than for the whole session. A
 * successful answer — including a `null` origin, which is a definitive answer —
 * is cached.
 */
export function webBaseUrl(): Promise<string | null> {
  if (resolved === null) {
    const inFlight: Promise<string | null> = fetchWebBaseUrl().catch(() => {
      if (resolved === inFlight) {
        resolved = null;
      }
      return null;
    });
    resolved = inFlight;
  }
  return resolved;
}

/**
 * Absolute URL of the web app's password reset page, or `null` when the web
 * origin is unknown — in which case the caller renders no affordance at all.
 *
 * Asynchronous because the origin comes from the backend. Callers must start
 * with the affordance hidden and reveal it only once this resolves to a URL, so
 * the link never flashes visible and then disappears.
 */
export async function forgotPasswordUrl(): Promise<string | null> {
  const base = await webBaseUrl();
  return base === null ? null : `${base}${FORGOT_PASSWORD_PATH}`;
}
