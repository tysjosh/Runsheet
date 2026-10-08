/**
 * Per-route tab titles (UI finding U2).
 *
 * Almost every page is a client component and cannot export `metadata`, so each
 * route gets a tiny server `layout.tsx` that sets `title`. The root layout's
 * `"%s · Runsheet"` template turns that into the tab text. `/` keeps the root
 * `title.default` and `/privacy` sets its own title in its server page.
 *
 * This guards against a route silently losing its title (falling back to the
 * root default) and against two routes ending up with the same tab text.
 * Dynamic `[id]` routes use static generic titles; there is no data fetching.
 */
const ROUTE_TITLES: Array<[route: string, load: () => unknown, title: string]> =
  [
    [
      "/auth/forgot-password",
      () => require("./auth/forgot-password/layout"),
      "Forgot password",
    ],
    [
      "/auth/reset-password",
      () => require("./auth/reset-password/layout"),
      "Reset password",
    ],
    ["/dashboard", () => require("./dashboard/(today)/layout"), "Dashboard"],
    [
      "/dashboard/analytics",
      () => require("./dashboard/analytics/layout"),
      "Analytics",
    ],
    [
      "/dashboard/billing",
      () => require("./dashboard/billing/layout"),
      "Billing",
    ],
    [
      "/dashboard/billing/accounts/[id]",
      () => require("./dashboard/billing/accounts/[id]/layout"),
      "Account",
    ],
    [
      "/dashboard/billing/invoices/[id]",
      () => require("./dashboard/billing/invoices/[id]/layout"),
      "Invoice",
    ],
    [
      "/dashboard/compliance",
      () => require("./dashboard/compliance/layout"),
      "Compliance",
    ],
    [
      "/dashboard/compliance/terminals/[id]",
      () => require("./dashboard/compliance/terminals/[id]/layout"),
      "Terminal",
    ],
    ["/dashboard/control", () => require("./dashboard/control/layout"), "Live"],
    [
      "/dashboard/customers",
      () => require("./dashboard/customers/layout"),
      "Customers",
    ],
    [
      "/dashboard/customers/[id]",
      () => require("./dashboard/customers/[id]/layout"),
      "Customer",
    ],
    [
      "/dashboard/customers/tanks/[id]",
      () => require("./dashboard/customers/tanks/[id]/layout"),
      "Tank",
    ],
    [
      "/dashboard/dispatch",
      () => require("./dashboard/dispatch/layout"),
      "Dispatch",
    ],
    [
      "/dashboard/dispatch/jobs/[id]",
      () => require("./dashboard/dispatch/jobs/[id]/layout"),
      "Job",
    ],
    [
      "/dashboard/dispatch/jobs/[id]/cargo",
      () => require("./dashboard/dispatch/jobs/[id]/cargo/layout"),
      "Cargo Manifest",
    ],
    ["/dashboard/fleet", () => require("./dashboard/fleet/layout"), "Fleet"],
    [
      "/dashboard/fuel-ops",
      () => require("./dashboard/fuel-ops/layout"),
      "Fuel",
    ],
    ["/dashboard/orders", () => require("./dashboard/orders/layout"), "Orders"],
    [
      "/dashboard/orders/[orderId]",
      () => require("./dashboard/orders/[orderId]/layout"),
      "Order",
    ],
    [
      "/dashboard/profile",
      () => require("./dashboard/profile/layout"),
      "Profile",
    ],
    [
      "/dashboard/settings",
      () => require("./dashboard/settings/layout"),
      "Settings",
    ],
    [
      "/dashboard/settings/depots/[id]",
      () => require("./dashboard/settings/depots/[id]/layout"),
      "Depot",
    ],
    [
      "/ops/command",
      () => require("./ops/command/(console)/layout"),
      "Command Interface",
    ],
    ["/privacy", () => require("./privacy/page"), "Privacy"],
    [
      "/request-pilot",
      () => require("./request-pilot/layout"),
      "Request a Pilot",
    ],
    ["/signin", () => require("./signin/layout"), "Sign in"],
  ];

type Title = string | { default?: string; template?: string } | undefined;

function rawTitle(load: () => unknown): Title {
  return (load() as { metadata?: { title?: Title } }).metadata?.title;
}

function titleOf(load: () => unknown): string | undefined {
  const title = rawTitle(load);
  return typeof title === "string" ? title : title?.default;
}

// Layouts that have titled child routes beneath them. Next.js only passes a
// `title.template` down from an object title; a plain string here would make
// every child render without the " · Runsheet" suffix.
const PARENT_LAYOUTS: Array<[segment: string, load: () => unknown]> = [
  ["dashboard/billing", () => require("./dashboard/billing/layout")],
  ["dashboard/compliance", () => require("./dashboard/compliance/layout")],
  ["dashboard/customers", () => require("./dashboard/customers/layout")],
  ["dashboard/dispatch", () => require("./dashboard/dispatch/layout")],
  [
    "dashboard/dispatch/jobs/[id]",
    () => require("./dashboard/dispatch/jobs/[id]/layout"),
  ],
  ["dashboard/orders", () => require("./dashboard/orders/layout")],
  ["dashboard/settings", () => require("./dashboard/settings/layout")],
];

describe("per-route page titles", () => {
  it("covers every route except the landing page", () => {
    // Staff routes after the UI revamp consolidation (design.md §4); `/`
    // deliberately keeps the root `title.default`.
    expect(ROUTE_TITLES).toHaveLength(27);
  });

  it.each(ROUTE_TITLES)("%s has its own title", (_route, load, expected) => {
    expect(titleOf(load)).toBe(expected);
  });

  it("gives every route a distinct title", () => {
    const titles = ROUTE_TITLES.map(([, load]) => titleOf(load));
    expect(new Set(titles).size).toBe(titles.length);
  });

  it.each(PARENT_LAYOUTS)(
    "%s keeps the Runsheet template for its child routes",
    (_segment, load) => {
      expect(rawTitle(load)).toMatchObject({ template: "%s · Runsheet" });
    },
  );
});
