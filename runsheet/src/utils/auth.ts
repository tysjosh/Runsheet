/**
 * Session-based authentication utilities.
 *
 * The browser no longer mints or signs its own JWT. Authentication is owned by
 * the SuperTokens frontend SDK, which manages an HttpOnly cookie-backed session
 * and auto-refreshes it. These helpers read from the verified session rather
 * than from `sessionStorage` (SuperTokens Auth Migration Req 8.1, 8.6, 10.5).
 */

import Session from "supertokens-auth-react/recipe/session";

/**
 * Return the current SuperTokens access token, or `null` when there is no
 * active session.
 *
 * The token is issued, signed, and rotated by SuperTokens — it is never minted
 * in the browser. Callers attach it where an explicit credential is required
 * (e.g. a WebSocket handshake query param). For regular HTTP requests the SDK
 * session interceptors attach session cookies automatically, so most callers do
 * not need to read this directly.
 */
export async function getAuthToken(): Promise<string | null> {
  if (typeof window === "undefined") return null;

  try {
    if (!(await Session.doesSessionExist())) return null;
    const token = await Session.getAccessToken();
    return token ?? null;
  } catch {
    return null;
  }
}

/**
 * Return the caller's role list, read from the verified session's access-token
 * payload claims.
 *
 * Role-based UI gates use this to decide whether to surface privileged
 * controls; the backend independently re-verifies the session on every request,
 * so this is presentation-only. Returns an empty array when unauthenticated or
 * when the session carries no roles — callers should treat that as "no
 * privileged roles".
 */
export async function getCurrentUserRoles(): Promise<string[]> {
  if (typeof window === "undefined") return [];

  try {
    if (!(await Session.doesSessionExist())) return [];
    const payload = await Session.getAccessTokenPayloadSecurely();
    const roles = (payload as { roles?: unknown } | null)?.roles;
    if (Array.isArray(roles)) {
      return roles.filter((r): r is string => typeof r === "string");
    }
    return [];
  } catch {
    return [];
  }
}

/**
 * Return the caller's verified user id, read from the SuperTokens session.
 *
 * UI surfaces that stamp an actor on an action (e.g. the Storm_Mode override
 * form) use this so the displayed/submitted actor matches the verified
 * identity. The backend independently derives the audit actor from the session
 * and ignores any client-supplied actor id, so this is presentation-only.
 * Returns `null` when unauthenticated.
 */
export async function getCurrentUserId(): Promise<string | null> {
  if (typeof window === "undefined") return null;

  try {
    if (!(await Session.doesSessionExist())) return null;
    const userId = await Session.getUserId();
    return userId || null;
  } catch {
    return null;
  }
}

/**
 * Revoke the current SuperTokens session (sign-out).
 *
 * Replaces the previous `sessionStorage`-clearing helper; the SDK handles
 * cookie/token teardown server-side.
 */
export async function signOut(): Promise<void> {
  if (typeof window === "undefined") return;
  await Session.signOut();
}
