"use client";

import { useSyncExternalStore } from "react";

/** Portal breakpoints (design §11.1). */
export const PORTAL_TABS_IN_TOP_BAR = "(min-width: 768px)";
export const PORTAL_TABLES = "(min-width: 1024px)";
export const PORTAL_PHONE = "(max-width: 639.98px)";

/**
 * True while `query` matches. Without `matchMedia` (tests, old browsers) it
 * reads false, which is the phone layout: the portal is mobile first.
 */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === "undefined" || !window.matchMedia) return () => {};
      const mql = window.matchMedia(query);
      mql.addEventListener?.("change", onChange);
      return () => mql.removeEventListener?.("change", onChange);
    },
    () =>
      typeof window !== "undefined" && window.matchMedia
        ? window.matchMedia(query).matches
        : false,
    () => false,
  );
}
