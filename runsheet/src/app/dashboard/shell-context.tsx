"use client";

/**
 * Dashboard shell context.
 *
 * The dashboard is now a real nested-route tree: `app/dashboard/layout.tsx`
 * renders the persistent chrome (sidebar, header, global overlays) and each
 * view is its own route segment under `/dashboard/*`. Child route pages still
 * need to trigger the two global overlays the layout owns — the Create Order
 * modal and the AI Copilot panel — so the layout exposes them through this
 * context instead of prop-drilling.
 */

import { createContext, useContext } from "react";

export interface DashboardChrome {
  /** Open the global "Create Order" modal. */
  openCreateOrder: () => void;
  /** Open the AI Copilot side panel. */
  openAIChat: () => void;
}

const DashboardChromeContext = createContext<DashboardChrome | null>(null);

export function DashboardChromeProvider({
  value,
  children,
}: {
  value: DashboardChrome;
  children: React.ReactNode;
}) {
  return (
    <DashboardChromeContext.Provider value={value}>
      {children}
    </DashboardChromeContext.Provider>
  );
}

/** Access the dashboard chrome actions. No-ops when rendered outside the shell. */
export function useDashboardChrome(): DashboardChrome {
  return (
    useContext(DashboardChromeContext) ?? {
      openCreateOrder: () => {},
      openAIChat: () => {},
    }
  );
}

// ─── Sidebar item ↔ route mapping ────────────────────────────────────────────

/**
 * Maps a sidebar nav id (and the in-shell `openModule` item key) to its
 * `/dashboard/*` route. The route tree is the single source of truth for where
 * a module lives; navigation and active-state derivation both go through here.
 */
export const DASHBOARD_ITEM_PATH: Record<string, string> = {
  today: "/dashboard",
  dispatch: "/dashboard/dispatch",
  orders: "/dashboard/orders",
  live: "/dashboard/control",
  control: "/dashboard/control",
  fleet: "/dashboard/fleet",
  customers: "/dashboard/customers",
  "fuel-ops": "/dashboard/fuel-ops",
  billing: "/dashboard/billing",
  compliance: "/dashboard/compliance",
  analytics: "/dashboard/analytics",
  settings: "/dashboard/settings",
  profile: "/dashboard/profile",
  // Retired modules (UI revamp §4) resolve to their new homes, so stale
  // `openModule` callers and bookmarks still land on the right tab.
  drivers: "/dashboard/fleet?tab=drivers",
  notifications: "/dashboard/customers?tab=communications",
  setup: "/dashboard/settings?tab=company",
  admin: "/dashboard/settings",
  reconciliation: "/dashboard/billing?tab=reconciliation",
};

/** Resolve the destination path for a sidebar/module id (falls back to today). */
export function dashboardPathForItem(item: string): string {
  return DASHBOARD_ITEM_PATH[item] ?? "/dashboard";
}

/**
 * The URL for a module and optional tab. An explicit tab replaces any tab the
 * module's default path already names (e.g. `setup` + `depots`).
 */
export function dashboardHref(item: string, tab?: string): string {
  const base = dashboardPathForItem(item);
  if (!tab) return base;
  const [path, query = ""] = base.split("?");
  const params = new URLSearchParams(query);
  params.set("tab", tab);
  return `${path}?${params.toString()}`;
}

/** Derive the active sidebar id from the current pathname. */
export function dashboardActiveItem(pathname: string): string {
  // /dashboard            → today
  // /dashboard/orders/123 → orders   (second path segment is the module id)
  const seg = pathname.split("/")[2];
  return seg ? seg : "today";
}
