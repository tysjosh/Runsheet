/**
 * SuperTokens frontend SDK configuration.
 *
 * Wires `supertokens-auth-react` (and its `supertokens-web-js` core) using the
 * `NEXT_PUBLIC_ST_*` environment variables rather than hardcoded constants
 * (SuperTokens Auth Migration Req 8.3, 10.4). The EmailPassword + Session
 * recipes are registered so the browser establishes an SDK-managed session on
 * sign-in instead of minting its own token.
 */

import type { SuperTokensConfig } from "supertokens-auth-react/lib/build/types";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";
import Session from "supertokens-auth-react/recipe/session";

const APP_NAME = "Runsheet";

/** Backend public origin that serves the SuperTokens SDK auth routes. */
const API_DOMAIN =
  process.env.NEXT_PUBLIC_ST_API_DOMAIN || "http://localhost:8080";

/** Frontend public origin (this Next.js app). */
const WEBSITE_DOMAIN =
  process.env.NEXT_PUBLIC_ST_WEBSITE_DOMAIN || "http://localhost:3000";

/** Path prefix the SDK auth routes are mounted under on the backend. */
const API_BASE_PATH = process.env.NEXT_PUBLIC_ST_API_BASE_PATH || "/auth";

/** Window event the audience guard listens for (customer portal §10.1). */
export const SESSION_CHANGED_EVENT = "runsheet:session-changed";

/** SDK session events that can change who the signed-in user is. */
export const SESSION_CHANGE_ACTIONS: ReadonlySet<string> = new Set([
  "SESSION_CREATED",
  "SIGN_OUT",
  "ACCESS_TOKEN_PAYLOAD_UPDATED",
  "UNAUTHORISED",
]);

/**
 * Re-broadcast an SDK session event as a window `CustomEvent`, so components
 * that cache the session's audience (AudienceGuard) re-resolve it once instead
 * of reading roles on every navigation.
 */
export function handleSessionEvent(event: { action: string }): void {
  if (typeof window === "undefined") return;
  if (!SESSION_CHANGE_ACTIONS.has(event.action)) return;
  window.dispatchEvent(
    new CustomEvent(SESSION_CHANGED_EVENT, {
      detail: { action: event.action },
    }),
  );
}

/**
 * Build the SuperTokens frontend configuration from environment.
 *
 * Registers the EmailPassword recipe (email/password sign-in) and the Session
 * recipe (SDK-managed, cookie-backed sessions with automatic refresh). The
 * Session recipe's documented `onHandleEvent` option forwards session changes
 * to {@link SESSION_CHANGED_EVENT}.
 */
export function frontendConfig(): SuperTokensConfig {
  return {
    appInfo: {
      appName: APP_NAME,
      apiDomain: API_DOMAIN,
      websiteDomain: WEBSITE_DOMAIN,
      apiBasePath: API_BASE_PATH,
    },
    recipeList: [
      EmailPassword.init(),
      Session.init({ onHandleEvent: handleSessionEvent }),
    ],
  };
}
