/**
 * Authentication utilities for JWT token management
 */

import * as jose from "jose";

const JWT_SECRET = "dev-jwt-secret-change-me-in-production";

export interface TokenPayload {
  tenant_id: string;
  sub: string; // user_id
  user_id?: string;
  has_pii_access?: boolean;
  roles?: string[];
  exp?: number;
}

/**
 * Generate a properly signed JWT token
 * Uses HMAC-SHA256 signing to match backend validation
 */
export async function generateDevToken(payload: TokenPayload): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  const fullPayload = {
    ...payload,
    exp: payload.exp || now + 86400, // 24 hours from now
    iat: now,
  };

  // Create a proper JWT using jose library with HS256 signing
  const secret = new TextEncoder().encode(JWT_SECRET);

  const token = await new jose.SignJWT(fullPayload)
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setIssuedAt(now)
    .setExpirationTime(fullPayload.exp)
    .sign(secret);

  return token;
}

/**
 * Get the current auth token from session storage
 */
export async function getAuthToken(): Promise<string | null> {
  if (typeof window === "undefined") return null;

  const isAuthenticated = sessionStorage.getItem("isAuthenticated");
  if (isAuthenticated !== "true") return null;

  // Check if we have a stored token
  let token = sessionStorage.getItem("authToken");

  if (!token) {
    // Generate a development token for the demo user
    const payload: TokenPayload = {
      tenant_id: "demo-tenant",
      sub: "admin@runsheet.com",
      user_id: "admin@runsheet.com",
      has_pii_access: true,
      roles: ["admin", "ops_manager"],
    };

    token = await generateDevToken(payload);
    sessionStorage.setItem("authToken", token);
  }

  return token;
}

/**
 * Clear the auth token from session storage
 */
export function clearAuthToken(): void {
  if (typeof window === "undefined") return;
  sessionStorage.removeItem("authToken");
  sessionStorage.removeItem("isAuthenticated");
  sessionStorage.removeItem("tenant_id");
}

/**
 * Set authentication state and generate token
 */
export async function setAuthenticated(email: string): Promise<void> {
  if (typeof window === "undefined") return;

  const tenantId = "demo-tenant";

  sessionStorage.setItem("isAuthenticated", "true");
  sessionStorage.setItem("tenant_id", tenantId); // Store tenant_id for tenant service

  // Generate token for the user
  const payload: TokenPayload = {
    tenant_id: tenantId,
    sub: email,
    user_id: email,
    has_pii_access: true,
    roles: ["admin", "ops_manager"],
  };

  const token = await generateDevToken(payload);
  sessionStorage.setItem("authToken", token);
}
