/**
 * In-process fake for the UI revamp specs (task 1.11). Signs the browser in
 * with a synthetic SuperTokens front token (no backend, no staging), answers
 * every API call with fixtures, and serves the Dispatch Board through the
 * board suite's `FakeBoard`. Sockets are accepted and stay quiet.
 */
import type { BrowserContext, Page, Route } from "@playwright/test";
import { FakeBoard } from "../dispatchBoardFake";
import {
  APPROVALS,
  ASSETS,
  CUSTOMERS,
  FUEL_ALERTS,
  INVOICES,
  JOBS,
  ORDERS,
  PLANS,
  PROFILE,
  TENANT,
} from "./fixtures";
import { phase3Response } from "./phase3Fake";

export const ORIGIN = "http://localhost:8080";
/** Id the redirect spec uses for detail routes (answered 404). */
export const MISSING_ID = "QA-E2E-404";
export const API = `${ORIGIN}/api`;

/**
 * The cookies `supertokens-website` reads to decide a session exists
 * (`sFrontToken` + `st-last-access-token-update`); the front token's `up` is
 * the access-token payload `getCurrentUserRoles` reads.
 */
export async function signIn(
  context: BrowserContext,
  roles: string[] = ["admin", "dispatcher"],
  baseURL = "http://localhost:3125",
): Promise<void> {
  const front = Buffer.from(
    JSON.stringify({
      uid: PROFILE.user_id,
      ate: Date.now() + 6 * 3_600_000,
      up: { roles, tId: TENANT, sub: PROFILE.user_id },
    }),
  ).toString("base64");
  const url = new URL(baseURL);
  await context.addCookies([
    { name: "sFrontToken", value: front, domain: url.hostname, path: "/" },
    {
      name: "st-last-access-token-update",
      value: String(Date.now()),
      domain: url.hostname,
      path: "/",
    },
  ]);
  await context.addInitScript((tenant) => {
    window.localStorage.setItem("tenant_id", tenant);
  }, TENANT);
}

const EMPTY_LIST = {
  data: [],
  items: [],
  entries: [],
  total: 0,
  page: 1,
  size: 20,
  has_more: false,
  cursor: null,
  pagination: { page: 1, size: 20, total: 0, total_pages: 0 },
  request_id: "e2e",
};

function paginated<T>(rows: T[]) {
  return {
    data: rows,
    items: rows,
    entries: rows,
    total: rows.length,
    page: 1,
    size: Math.max(20, rows.length),
    has_more: false,
    cursor: null,
    pagination: { page: 1, size: 50, total: rows.length, total_pages: 1 },
    request_id: "e2e",
  };
}

/** Requests the fake answered with the generic empty list (for debugging). */
export const unmatched = new Set<string>();

export async function installShellFake(
  page: Page,
  {
    board = new FakeBoard({
      lanes: 12,
      stopsPerLane: 3,
      trayOrders: 20,
      drivers: 12,
    }),
  } = {},
): Promise<FakeBoard> {
  // Board routes first: later routes win in Playwright, so the generic
  // handler below falls back to the board for /fuel/board/*.
  await board.install(page, { userId: PROFILE.user_id, name: "QA Dispatcher" });

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

    if (path.startsWith("/fuel/board")) return route.fallback();
    // SuperTokens SDK routes (never expected with a fresh token).
    if (url.pathname.startsWith("/auth/session/refresh")) return json(200, {});
    if (path === "/auth/account/me") return json(200, PROFILE);

    if (req.method() !== "GET")
      return json(200, { data: {}, request_id: "e2e" });

    // Detail reads for the redirect spec's synthetic id answer 404, so each
    // destination renders its own "… not found" state (proves the route).
    if (path.includes(`/${MISSING_ID}`))
      return json(404, {
        error_code: "NOT_FOUND",
        message: "Not found (e2e fixture)",
        details: {},
      });

    if (path === "/orders") {
      const status = url.searchParams.get("status");
      const rows = status ? ORDERS.filter((o) => o.status === status) : ORDERS;
      // The list's own page size (chip counts ask for size=1 and read total).
      const size = Number(url.searchParams.get("size") ?? rows.length);
      return json(200, {
        ...paginated(rows.slice(0, size)),
        total: rows.length,
      });
    }
    if (path === "/fuel/mvp/plans") return json(200, paginated(PLANS));
    if (path === "/fleet/assets")
      return json(200, { data: ASSETS, request_id: "e2e" });
    if (path === "/fuel/alerts")
      return json(200, { data: FUEL_ALERTS, request_id: "e2e" });
    if (path === "/agent/approvals") return json(200, paginated(APPROVALS));
    if (path === "/agent/config/autonomy")
      return json(200, { level: "auto-low", request_id: "e2e" });
    if (path === "/scheduling/jobs/delayed")
      return json(200, {
        data: JOBS.filter((j) => j.delayed),
        request_id: "e2e",
      });
    if (path === "/scheduling/jobs/active")
      return json(200, {
        data: JOBS.filter((j) => j.status === "in_progress"),
        request_id: "e2e",
      });
    if (path === "/scheduling/jobs") {
      // Server-side status and paging, as the backend does (chip counts ask
      // for size=1 and read pagination.total).
      const status = url.searchParams.get("status");
      const rows = status ? JOBS.filter((j) => j.status === status) : JOBS;
      const size = Number(url.searchParams.get("size") ?? 20);
      const page = Number(url.searchParams.get("page") ?? 1);
      const body = paginated(rows.slice((page - 1) * size, page * size));
      return json(200, {
        ...body,
        total: rows.length,
        pagination: {
          page,
          size,
          total: rows.length,
          total_pages: Math.ceil(rows.length / size),
        },
      });
    }
    // Single-object read: the generic list body would leave `trucks` unset.
    if (path === "/compliance/ifta/report")
      return json(200, {
        data: {
          tenant_id: TENANT,
          quarter: url.searchParams.get("quarter") ?? "2026-Q4",
          trucks: [],
          fleet_mpg: null,
          incomplete_trucks: [],
          generated_at: "2026-10-08T14:00:00Z",
        },
        request_id: "e2e",
      });
    if (path === "/scheduling/jobs/summary" || path === "/scheduling/summary")
      return json(200, {
        data: {
          total: JOBS.length,
          by_status: {
            scheduled: 5,
            assigned: 5,
            in_progress: 4,
            completed: 4,
            failed: 4,
          },
          delayed: 4,
        },
        request_id: "e2e",
      });
    // Phase 3 fixtures win over the generic Phase 1 lists below.
    const phase3 = phase3Response(path, url);
    if (phase3 !== undefined) return json(200, phase3);
    if (path === "/commerce/invoices") return json(200, paginated(INVOICES));
    if (path === "/commerce/customers") return json(200, paginated(CUSTOMERS));
    if (path === "/notifications/summary")
      return json(200, {
        total: 0,
        by_status: {},
        by_channel: {},
        by_type: {},
      });
    if (path === "/search/universal")
      return json(200, { orders: [], customers: [], assets: [] });

    unmatched.add(`${req.method()} ${path}`);
    return json(200, EMPTY_LIST);
  });
  // Sockets: FakeBoard.install already accepts every /ws/ route quietly.
  return board;
}
