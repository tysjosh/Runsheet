/**
 * Shared fixtures for the portal Jest suites: response-shaped fetch fakes and
 * sample projections (QA- prefixed ids, generic names, no real PII).
 */

import type {
  PortalInvoice,
  PortalMe,
  PortalTank,
} from "../../../services/portalApi";

export interface FakeResponseInit {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}

/** A fetch-Response-shaped object (jsdom has no Response global). */
export function fakeResponse({
  status = 200,
  body = {},
  headers = {},
}: FakeResponseInit = {}) {
  const lower = Object.fromEntries(
    Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => lower[name.toLowerCase()] ?? null },
    json: async () => body,
    blob: async () => new Blob(["x"]),
  };
}

/** Error envelope as the backend sends it. */
export function errorBody(code: string, message: string, details = {}) {
  return { error_code: code, message, details, request_id: "req-test" };
}

/** The slice of a Jest mock the portal suites read. */
export type FetchMock = ((
  url: string,
  init?: RequestInit,
) => Promise<unknown>) & {
  mock: { calls: Array<[string, RequestInit | undefined]> };
};

// `jest` is injected by the Jest runtime into every module it loads; this
// file is only imported by test suites, and test files are outside tsc's
// program, so the global is declared here for type-checking only.
declare const jest: {
  fn: (impl: (url: string) => Promise<unknown>) => FetchMock;
};

/**
 * Replace `global.fetch` with a mock that answers with `responses` in order
 * (the last one repeats).
 */
export function installFetch(
  ...responses: Array<FakeResponseInit | ((url: string) => FakeResponseInit)>
): FetchMock {
  const queue = [...responses];
  const mock = jest.fn(async (url: string) => {
    const next = queue.length > 1 ? queue.shift() : queue[0];
    const init = typeof next === "function" ? next(url) : next;
    return fakeResponse(init ?? {});
  });
  global.fetch = mock as unknown as typeof fetch;
  return mock;
}

export const ME: PortalMe = {
  email: "ap@example.test",
  customer_display_name: "QA-PORTAL Customer A",
  supplier_name: "demo-tenant",
  ordering_available: true,
  invoices_available: true,
  payments_available: true,
  measurement_units: { volume: "gal", distance: "mi" },
};

export function tank(overrides: Partial<PortalTank> = {}): PortalTank {
  return {
    customer_tank_id: "QA-TANK-1",
    label: "North yard",
    product_code: "ULSD",
    capacity_gallons: 2000,
    current_level_gallons: 1240,
    percent_full: 62,
    last_reading_at: "2026-10-08T14:00:00Z",
    reading_stale: false,
    forecast: {
      runout_at: "2026-10-20T12:00:00Z",
      days_to_runout: 11,
      generated_at: "2026-10-08T15:00:00Z",
    },
    next_delivery: null,
    ...overrides,
  };
}

export function invoice(overrides: Partial<PortalInvoice> = {}): PortalInvoice {
  return {
    invoice_id: "QA-INV-1",
    invoice_number: "1001",
    status_code: "open",
    status_label: "Open",
    issued_at: "2026-10-01T12:00:00Z",
    due_date: "2026-10-31",
    created_at: "2026-10-01T12:00:00Z",
    account_display_name: "Main account",
    subtotal_cents: 100000,
    tax_cents: 5000,
    total_cents: 105000,
    amount_paid_cents: 0,
    remaining_cents: 105000,
    line_items: [],
    delivery: null,
    payment_attempt: null,
    payable: true,
    ...overrides,
  };
}
