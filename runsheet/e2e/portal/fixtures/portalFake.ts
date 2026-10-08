/**
 * Customer-portal fixtures for the fixture-backed specs (design §11.8):
 * a synthetic customer session (no backend, no staging) and `page.route`
 * answers for `GET /api/portal/**`. Every other method is answered 418, so
 * no test can write. QA- ids, generic names, no real PII.
 */
import type { BrowserContext, Page, Route } from "@playwright/test";

export const ORIGIN = "http://localhost:8080";
export const PORT = 3126;
export const BASE_URL = `http://localhost:${PORT}`;

/** Fixed clock for stable dates (Thu 8 Oct 2026, 9:00 AM CDT). */
export const NOW = new Date("2026-10-08T14:00:00Z");
const at = (hoursFromNow: number) =>
  new Date(NOW.getTime() + hoursFromNow * 3_600_000).toISOString();

export const ME = {
  email: "qa-portal-ap@example.test",
  customer_display_name: "QA Lone Star Construction",
  supplier_name: "QA Demo Fuels",
  ordering_available: true,
  invoices_available: true,
  payments_available: true,
  measurement_units: { volume: "gal", distance: "mi" },
  open_balance_cents: 526_600,
  open_invoice_count: 3,
  overdue_count: 1,
};

const forecast = (days: number) => ({
  runout_at: at(days * 24),
  days_to_runout: days,
  generated_at: at(-1),
});

export const TANKS = [
  {
    customer_tank_id: "QA-TANK-OK",
    label: "Tank …TANK-2",
    display_name: null,
    service_address: null,
    product_code: "HEATING_OIL",
    capacity_gallons: 1000,
    current_level_gallons: 820,
    percent_full: 82,
    last_reading_at: at(-24 * 9),
    reading_stale: true,
    forecast: null,
    next_delivery: null,
  },
  {
    customer_tank_id: "QA-TANK-WARN",
    label: "QA North yard diesel",
    display_name: "North yard diesel",
    service_address: "12 QA Yard Road, Springfield",
    product_code: "DIESEL_2",
    capacity_gallons: 2000,
    current_level_gallons: 640,
    percent_full: 32,
    last_reading_at: at(-2),
    reading_stale: false,
    forecast: forecast(4),
    next_delivery: {
      order_id: "QA-ORD-CONF",
      status_code: "confirmed",
      status_label: "Confirmed",
      window_start: at(24 + 0.33),
      window_end: at(28.4),
    },
  },
  {
    customer_tank_id: "QA-TANK-CRIT",
    label: "Farm off-road",
    display_name: "Farm off-road",
    service_address: null,
    product_code: "OFF_ROAD_DIESEL",
    capacity_gallons: 1500,
    current_level_gallons: 180,
    percent_full: 12,
    last_reading_at: at(-1),
    reading_stale: false,
    forecast: forecast(1),
    next_delivery: {
      order_id: "QA-ORD-AWAIT",
      status_code: "awaiting_confirmation",
      status_label: "Awaiting confirmation",
      window_start: at(24 + 0.33),
      window_end: at(30.33),
    },
  },
];

const tankRef = (id: string) => {
  const t = TANKS.find((x) => x.customer_tank_id === id);
  return { customer_tank_id: id, label: t?.label ?? "Tank" };
};

function order(
  id: string,
  status_code: string,
  status_label: string,
  tank: string,
  product: string,
  o: Record<string, unknown> = {},
) {
  return {
    order_id: id,
    status_code,
    status_label,
    product_code: product,
    gallons_requested: 1200,
    fill_to_full: false,
    window_start: at(-24),
    window_end: at(-20),
    po_number: null,
    tank: tankRef(tank),
    created_at: at(-48),
    delivered_at: null,
    delivered_gallons: null,
    ticket_number: null,
    cancellable: false,
    ...o,
  };
}

export const ORDERS = [
  order(
    "QA-ORD-AWAIT",
    "awaiting_confirmation",
    "Awaiting confirmation",
    "QA-TANK-CRIT",
    "OFF_ROAD_DIESEL",
    {
      fill_to_full: true,
      gallons_requested: null,
      window_start: at(24 + 0.33),
      window_end: at(30.33),
      po_number: "PO-55812",
      cancellable: true,
      created_at: at(-1),
    },
  ),
  order("QA-ORD-HOLD", "on_hold", "On hold", "QA-TANK-WARN", "DIESEL_2", {
    window_start: at(48),
    window_end: at(52),
    created_at: at(-2),
  }),
  order("QA-ORD-CONF", "confirmed", "Confirmed", "QA-TANK-WARN", "DIESEL_2", {
    window_start: at(24 + 0.33),
    window_end: at(28.4),
    po_number: "PO-55790",
    created_at: at(-3),
  }),
  order(
    "QA-ORD-OUT",
    "out_for_delivery",
    "Out for delivery",
    "QA-TANK-OK",
    "HEATING_OIL",
    {
      gallons_requested: 400,
      window_start: at(0.33),
      window_end: at(5.13),
      created_at: at(-4),
    },
  ),
  order("QA-ORD-DEL", "delivered", "Delivered", "QA-TANK-WARN", "DIESEL_2", {
    delivered_at: at(-24 * 6 + 2.7),
    delivered_gallons: 1187.4,
    ticket_number: "TKT-20817",
    created_at: at(-24 * 7),
  }),
  order(
    "QA-ORD-FAIL",
    "not_delivered",
    "Not delivered",
    "QA-TANK-WARN",
    "DIESEL_2",
    {
      window_start: at(-24 * 12),
      window_end: at(-24 * 11),
      created_at: at(-24 * 13),
    },
  ),
  order("QA-ORD-CANC", "cancelled", "Cancelled", "QA-TANK-WARN", "DIESEL_2", {
    window_start: at(-24 * 20),
    window_end: at(-24 * 19),
    created_at: at(-24 * 21),
  }),
];

function invoice(
  id: string,
  number: string,
  status_code: string,
  status_label: string,
  o: Record<string, unknown> = {},
) {
  return {
    invoice_id: id,
    invoice_number: number,
    status_code,
    status_label,
    issued_at: at(-24 * 10),
    due_date: "2026-10-31",
    created_at: at(-24 * 10),
    account_display_name: "Main account",
    subtotal_cents: 346_648,
    tax_cents: 20_799,
    total_cents: 367_447,
    amount_paid_cents: 0,
    remaining_cents: 367_447,
    line_items: [
      {
        product_code: "DIESEL_2",
        quantity_gallons: 1187.4,
        unit_price_cents: 292,
        unit_price_dollars: "2.9194",
        subtotal_cents: 346_648,
      },
    ],
    delivery: {
      delivered_at: at(-24 * 6 + 2.7),
      actual_gallons: 1187.4,
      ticket_number: "TKT-20817",
    },
    payment_attempt: null,
    payable: true,
    ...o,
  };
}

export const INVOICES = [
  invoice("QA-INV-OPEN", "INV-001021", "open", "Open"),
  invoice("QA-INV-PART", "INV-001020", "partial", "Partially paid", {
    amount_paid_cents: 100_000,
    remaining_cents: 267_447,
    payment_attempt: {
      payment_attempt_id: "QA-PA-1",
      status_code: "pending",
      status_label: "Payment processing",
      amount_cents: 50_000,
      created_at: at(-3),
    },
  }),
  invoice("QA-INV-OVER", "INV-001019", "overdue", "Overdue", {
    due_date: "2026-10-01",
    remaining_cents: 152_210,
    total_cents: 152_210,
  }),
  invoice("QA-INV-PAID", "INV-001018", "paid", "Paid", {
    amount_paid_cents: 367_447,
    remaining_cents: 0,
    payable: false,
  }),
  invoice("QA-INV-VOID", "INV-001017", "void", "Void", {
    remaining_cents: 0,
    payable: false,
  }),
];

export const DELIVERIES = [
  {
    order_id: "QA-ORD-DEL",
    delivered_at: at(-24 * 6 + 2.7),
    delivered_gallons: 1187.4,
    product_code: "DIESEL_2",
    ticket_number: "TKT-20817",
  },
  {
    order_id: "QA-ORD-DEL2",
    delivered_at: at(-24 * 34),
    delivered_gallons: 1402,
    product_code: "DIESEL_2",
    ticket_number: "TKT-20112",
  },
];

/** What a portal route answers: data, an error status, or never (loading). */
export type Answer = unknown | { status: number } | "pending";

export interface Scenario {
  me?: Partial<typeof ME>;
  tanks?: Answer;
  orders?: Answer;
  invoices?: Answer;
  invoice?: Answer;
  tank?: Answer;
  deliveries?: Answer;
}

const isStatus = (a: unknown): a is { status: number } =>
  typeof a === "object" &&
  a !== null &&
  "status" in a &&
  Object.keys(a).length === 1;

const list = (data: unknown[]) => ({
  data,
  next_cursor: null,
  limit: 25,
  request_id: "e2e",
});

/** Sign in with a synthetic SuperTokens front token holding `customer`. */
export async function signInCustomer(context: BrowserContext): Promise<void> {
  const front = Buffer.from(
    JSON.stringify({
      uid: "qa-portal-user",
      ate: Date.now() + 6 * 3_600_000,
      up: {
        roles: ["customer"],
        tId: "demo-tenant",
        sub: "qa-portal-user",
        customer_id: "QA-PORTAL-CUST",
      },
    }),
  ).toString("base64");
  const url = new URL(BASE_URL);
  await context.addCookies([
    { name: "sFrontToken", value: front, domain: url.hostname, path: "/" },
    {
      name: "st-last-access-token-update",
      value: String(Date.now()),
      domain: url.hostname,
      path: "/",
    },
  ]);
}

/** Answer the portal API from fixtures (GET only) and refuse every write. */
export async function installPortalFake(
  page: Page,
  s: Scenario = {},
): Promise<void> {
  // Stripe never loads in fixture runs (render-only pay page).
  await page.route("https://js.stripe.com/**", (r) => r.abort());
  await page.route(`${ORIGIN}/**`, async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/api/, "");
    const json = (status: number, body: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (url.pathname.startsWith("/auth/session/refresh")) return json(200, {});
    if (req.method() !== "GET") {
      return json(418, {
        error_code: "E2E_READ_ONLY",
        message: "fixture run",
        details: {},
      });
    }
    const answer = (given: Answer | undefined, fallback: unknown) => {
      const a = given === undefined ? fallback : given;
      if (a === "pending") return new Promise<void>(() => {});
      if (isStatus(a)) {
        return json(a.status, {
          error_code:
            a.status === 404 ? "RESOURCE_NOT_FOUND" : "INTERNAL_ERROR",
          message: "fixture error",
          details: {},
          request_id: "e2e",
        });
      }
      return json(200, a);
    };
    if (path === "/portal/me")
      return json(200, { data: { ...ME, ...s.me }, request_id: "e2e" });
    if (path === "/portal/tanks") return answer(s.tanks, list(TANKS));
    const tankDeliveries = /^\/portal\/tanks\/([^/]+)\/deliveries$/.exec(path);
    if (tankDeliveries) return answer(s.deliveries, list(DELIVERIES));
    const tankOne = /^\/portal\/tanks\/([^/]+)$/.exec(path);
    if (tankOne) {
      const t = TANKS.find(
        (x) => x.customer_tank_id === decodeURIComponent(tankOne[1]),
      );
      return answer(
        s.tank,
        t ? { data: t, request_id: "e2e" } : { status: 404 },
      );
    }
    if (path === "/portal/orders") {
      const group = url.searchParams.get("status_group");
      const past = new Set(["delivered", "not_delivered", "cancelled"]);
      const rows =
        group === "active"
          ? ORDERS.filter((o) => !past.has(o.status_code))
          : group === "past"
            ? ORDERS.filter((o) => past.has(o.status_code))
            : ORDERS;
      return answer(s.orders, list(rows));
    }
    if (path === "/portal/invoices") {
      const status = url.searchParams.get("status");
      const rows = status
        ? INVOICES.filter((i) => i.status_code === status)
        : INVOICES;
      return answer(s.invoices, list(rows));
    }
    const inv = /^\/portal\/invoices\/([^/]+)$/.exec(path);
    if (inv) {
      const i = INVOICES.find(
        (x) => x.invoice_id === decodeURIComponent(inv[1]),
      );
      return answer(
        s.invoice,
        i ? { data: i, request_id: "e2e" } : { status: 404 },
      );
    }
    return json(404, {
      error_code: "NOT_FOUND",
      message: "unmatched fixture",
      details: {},
    });
  });
}
