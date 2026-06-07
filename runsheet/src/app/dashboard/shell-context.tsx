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
  orders: "/dashboard/orders",
  fleet: "/dashboard/fleet",
  dispatch: "/dashboard/dispatch",
  drivers: "/dashboard/drivers",
  "fuel-ops": "/dashboard/fuel-ops",
  compliance: "/dashboard/compliance",
  control: "/dashboard/control",
  customers: "/dashboard/customers",
  billing: "/dashboard/billing",
  analytics: "/dashboard/analytics",
  setup: "/dashboard/setup",
  admin: "/dashboard/admin",
  settings: "/dashboard/settings",
  profile: "/dashboard/profile",
  // Reconciliation folded into Billing; keep the alias so stale links resolve.
  reconciliation: "/dashboard/billing",
};

/** Resolve the destination path for a sidebar/module id (falls back to today). */
export function dashboardPathForItem(item: string): string {
  return DASHBOARD_ITEM_PATH[item] ?? "/dashboard";
}

/** Derive the active sidebar id from the current pathname. */
export function dashboardActiveItem(pathname: string): string {
  // /dashboard            → today
  // /dashboard/orders/123 → orders   (second path segment is the module id)
  const seg = pathname.split("/")[2];
  return seg ? seg : "today";
}
