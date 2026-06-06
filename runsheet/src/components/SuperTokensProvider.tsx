"use client";

/**
 * Client-side SuperTokens SDK initializer/provider.
 *
 * The SuperTokens React SDK must be initialized in the browser. This component
 * calls `SuperTokens.init` once (guarded so React strict-mode double-renders do
 * not re-initialize) and wraps the app in `SuperTokensWrapper` so session state
 * is available to descendants (SuperTokens Auth Migration Req 8.3).
 */

import type React from "react";
import SuperTokens, { SuperTokensWrapper } from "supertokens-auth-react";
import { frontendConfig } from "../config/supertokens";

let initialized = false;

function ensureInitialized(): void {
  if (initialized) return;
  // `SuperTokens.init` is a no-op target on the server; only run in the browser.
  if (typeof window === "undefined") return;
  SuperTokens.init(frontendConfig());
  initialized = true;
}

// Initialize as early as module evaluation on the client so SDK helpers (e.g.
// session interceptors) are ready before the first render.
ensureInitialized();

export function SuperTokensProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  ensureInitialized();
  return <SuperTokensWrapper>{children}</SuperTokensWrapper>;
}
