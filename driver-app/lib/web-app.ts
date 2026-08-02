/**
 * The Runsheet **web app** origin, and the pages on it the driver app links out
 * to.
 *
 * This is deliberately separate from `EXPO_PUBLIC_API_BASE_URL` in
 * `lib/api-client.ts`: that value is the *backend* origin, and the web app is a
 * different host in general, so the web origin cannot be derived from it.
 *
 * `EXPO_PUBLIC_WEB_BASE_URL` MUST match the backend's
 * `SUPERTOKENS_WEBSITE_DOMAIN` (`config/settings.py` →
 * `settings.supertokens_website_domain`), because that is the origin SuperTokens
 * builds password-reset links against. It is therefore a *second* source of
 * truth for one origin, and the two can drift: point this at a host that is not
 * the one the reset token was minted for and the driver lands on a page that
 * cannot service the token. Whoever changes one must change the other.
 *
 * The origin is TLS-only, matching the transport rule the API client enforces
 * (Requirement 15.4). A value that is absent, blank, or not `https://` yields
 * `null` rather than throwing: these accessors are read by the pre-auth sign-in
 * screen, which hides the affordance instead of rendering a dead link.
 */

import { InsecureBaseUrlError, assertTls } from './api-client';

/** Path of the web app's self-service password reset page. */
export const FORGOT_PASSWORD_PATH = '/auth/forgot-password';

/**
 * The configured web app origin, or `null` when it is unset or not TLS.
 *
 * Trimmed and stripped of trailing slashes, exactly as `configureApiClient`
 * normalizes the backend origin.
 */
export function webBaseUrl(): string | null {
  // `EXPO_PUBLIC_*` values are inlined at build time by the Expo bundler.
  const raw = process.env.EXPO_PUBLIC_WEB_BASE_URL;
  const trimmed = raw?.trim().replace(/\/+$/, '') ?? '';
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

/**
 * Absolute URL of the web app's password reset page, or `null` when the web
 * origin is unknown — in which case the caller renders no affordance at all.
 */
export function forgotPasswordUrl(): string | null {
  const base = webBaseUrl();
  return base === null ? null : `${base}${FORGOT_PASSWORD_PATH}`;
}
