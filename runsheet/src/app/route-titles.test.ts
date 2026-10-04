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
      "/admin/integrations",
      () => require("./admin/integrations/layout"),
      "Integrations",
    ],
    [
      "/admin/weather-alerts",
      () => require("./admin/weather-alerts/layout"),
      "Weather Alerts",
    ],
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
    ["/commerce", () => require("./commerce/layout"), "Commerce"],
    [
      "/commerce/accounts/[id]",
      () => require("./commerce/accounts/[id]/layout"),
      "Account",
    ],
    [
      "/commerce/ar-aging",
      () => require("./commerce/ar-aging/layout"),
      "AR Aging",
    ],
    [
      "/commerce/customers",
      () => require("./commerce/customers/layout"),
      "Commerce Customers",
    ],
    [
      "/commerce/customers/[id]",
      () => require("./commerce/customers/[id]/layout"),
      "Commerce Customer",
    ],
    [
      "/commerce/invoices/[id]",
      () => require("./commerce/invoices/[id]/layout"),
      "Invoice",
    ],
    [
      "/compliance/terminals/[id]",
      () => require("./compliance/terminals/[id]/layout"),
      "Terminal",
    ],
    ["/dashboard", () => require("./dashboard/(today)/layout"), "Today"],
    ["/dashboard/admin", () => require("./dashboard/admin/layout"), "Admin"],
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
      "/dashboard/compliance",
      () => require("./dashboard/compliance/layout"),
      "Compliance",
    ],
    [
      "/dashboard/control",
      () => require("./dashboard/control/layout"),
      "Control Center",
    ],
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
      "/dashboard/dispatch",
      () => require("./dashboard/dispatch/layout"),
      "Dispatch",
    ],
    [
      "/dashboard/drivers",
      () => require("./dashboard/drivers/layout"),
      "Drivers",
    ],
    ["/dashboard/fleet", () => require("./dashboard/fleet/layout"), "Fleet"],
    [
      "/dashboard/fuel-ops",
      () => require("./dashboard/fuel-ops/layout"),
      "Fuel Ops",
    ],
    [
      "/dashboard/notifications",
      () => require("./dashboard/notifications/layout"),
      "Notifications",
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
    ["/dashboard/setup", () => require("./dashboard/setup/layout"), "Setup"],
    ["/ops", () => require("./ops/(overview)/layout"), "Operations"],
    [
      "/ops/command",
      () => require("./ops/command/(console)/layout"),
      "Command Interface",
    ],
    [
      "/ops/control",
      () => require("./ops/control/layout"),
      "Operations Control",
    ],
    ["/ops/fuel", () => require("./ops/fuel/layout"), "Fuel Monitoring"],
    [
      "/ops/fuel/depots/[id]",
      () => require("./ops/fuel/depots/[id]/layout"),
      "Depot",
    ],
    [
      "/ops/fuel/tanks/[id]",
      () => require("./ops/fuel/tanks/[id]/layout"),
      "Tank",
    ],
    ["/ops/inventory", () => require("./ops/inventory/layout"), "Inventory"],
    ["/ops/scheduling", () => require("./ops/scheduling/layout"), "Job Board"],
    [
      "/ops/scheduling/[id]",
      () => require("./ops/scheduling/[id]/layout"),
      "Job",
    ],
    [
      "/ops/scheduling/[id]/cargo",
      () => require("./ops/scheduling/[id]/cargo/layout"),
      "Cargo Manifest",
    ],
    [
      "/orders/[orderId]",
      () => require("./orders/[orderId]/layout"),
      "Order Details",
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
  ["commerce", () => require("./commerce/layout")],
  ["commerce/customers", () => require("./commerce/customers/layout")],
  ["dashboard/customers", () => require("./dashboard/customers/layout")],
  ["dashboard/orders", () => require("./dashboard/orders/layout")],
  ["ops/fuel", () => require("./ops/fuel/layout")],
  ["ops/scheduling", () => require("./ops/scheduling/layout")],
  ["ops/scheduling/[id]", () => require("./ops/scheduling/[id]/layout")],
];

describe("per-route page titles", () => {
  it("covers every route except the landing page", () => {
    // 43 routes in total; `/` deliberately keeps the root `title.default`.
    expect(ROUTE_TITLES).toHaveLength(42);
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
