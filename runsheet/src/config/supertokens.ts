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

/**
 * Build the SuperTokens frontend configuration from environment.
 *
 * Registers the EmailPassword recipe (email/password sign-in) and the Session
 * recipe (SDK-managed, cookie-backed sessions with automatic refresh).
 */
export function frontendConfig(): SuperTokensConfig {
  return {
    appInfo: {
      appName: APP_NAME,
      apiDomain: API_DOMAIN,
      websiteDomain: WEBSITE_DOMAIN,
      apiBasePath: API_BASE_PATH,
    },
    recipeList: [EmailPassword.init(), Session.init()],
  };
}
