/**
 * Retired routes and where their functions now live (UI revamp R3,
 * design.md §4). `next.config.ts` maps this table in `redirects()` with
 * `permanent: true` (HTTP 308); Next.js preserves dynamic segments and
 * forwards the query string. `e2e/ui-revamp/redirects.spec.ts` checks every
 * row lands on the destination's `<h1>`.
 *
 * Row 5 (`/ops/command` → Live → Approvals) is added by task 3.6, once the
 * Copilot panel has the console's inline confirmation (R3.5).
 */
export interface RedirectRow {
  /** design.md §4 row number (0 for the `/ops` stub). */
  row: number;
  source: string;
  destination: string;
  /** Where the function now lives (the "Function now lives" column). */
  lives: string;
}

export const REDIRECTS: RedirectRow[] = [
  {
    row: 3,
    source: "/ops/scheduling/:id/cargo",
    destination: "/dashboard/dispatch/jobs/:id/cargo",
    lives: "Cargo manifest in shell",
  },
  {
    row: 2,
    source: "/ops/scheduling/:id",
    destination: "/dashboard/dispatch/jobs/:id",
    lives: "Job detail in shell",
  },
  {
    row: 1,
    source: "/ops/scheduling",
    destination: "/dashboard/dispatch?tab=jobs",
    lives: "Dispatch → Jobs",
  },
  {
    row: 4,
    source: "/ops/control",
    destination: "/dashboard/control",
    lives: "Live",
  },
  {
    row: 7,
    source: "/ops/fuel/depots/:id",
    destination: "/dashboard/settings/depots/:id",
    lives: "Depot detail",
  },
  {
    row: 8,
    source: "/ops/fuel/tanks/:id",
    destination: "/dashboard/customers/tanks/:id",
    lives: "Tank detail",
  },
  {
    row: 6,
    source: "/ops/fuel",
    destination: "/dashboard/fuel-ops",
    lives: "Fuel → Stations",
  },
  {
    row: 9,
    source: "/ops/inventory",
    destination: "/dashboard/fleet?tab=inventory",
    lives: "Fleet → Inventory",
  },
  { row: 0, source: "/ops", destination: "/dashboard", lives: "Dashboard" },
  {
    row: 12,
    source: "/commerce/customers/:id",
    destination: "/dashboard/customers/:id",
    lives: "Customer detail",
  },
  {
    row: 11,
    source: "/commerce/customers",
    destination: "/dashboard/customers",
    lives: "Customers",
  },
  {
    row: 13,
    source: "/commerce/invoices/:id",
    destination: "/dashboard/billing/invoices/:id",
    lives: "Invoice detail",
  },
  {
    row: 14,
    source: "/commerce/accounts/:id",
    destination: "/dashboard/billing/accounts/:id",
    lives: "Account detail",
  },
  {
    row: 15,
    source: "/commerce/ar-aging",
    destination: "/dashboard/billing?tab=ar-aging",
    lives: "Billing → AR Aging",
  },
  {
    row: 10,
    source: "/commerce",
    destination: "/dashboard/billing",
    lives: "Billing",
  },
  {
    row: 16,
    source: "/orders/:orderId",
    destination: "/dashboard/orders/:orderId",
    lives: "Order detail",
  },
  {
    row: 17,
    source: "/compliance/terminals/:id",
    destination: "/dashboard/compliance/terminals/:id",
    lives: "Terminal detail",
  },
  {
    row: 18,
    source: "/admin/integrations",
    destination: "/dashboard/settings?tab=integrations",
    lives: "Settings → Integrations",
  },
  {
    row: 19,
    source: "/admin/weather-alerts",
    destination: "/dashboard/settings?tab=weather-alerts",
    lives: "Settings → Company → Weather alerts",
  },
  {
    row: 20,
    source: "/dashboard/drivers",
    destination: "/dashboard/fleet?tab=drivers",
    lives: "Fleet → Drivers",
  },
  {
    row: 21,
    source: "/dashboard/notifications",
    destination: "/dashboard/customers?tab=communications",
    lives:
      "Customers → Communications (history); Settings → Notifications (rules, templates)",
  },
  {
    row: 22,
    source: "/dashboard/setup",
    destination: "/dashboard/settings?tab=company",
    lives: "Settings → Company",
  },
  {
    row: 23,
    source: "/dashboard/admin",
    destination: "/dashboard/settings",
    lives: "Settings",
  },
];

/** The shape `next.config.ts` returns from `redirects()`. */
export function nextRedirects() {
  return REDIRECTS.map(({ source, destination }) => ({
    source,
    destination,
    permanent: true,
  }));
}
