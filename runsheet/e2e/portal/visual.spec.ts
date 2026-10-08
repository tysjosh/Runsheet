/**
 * Portal visual baselines and axe (design §11.8, R14.20; AC 14, AC 15).
 *
 * Every page and state in the §11.8 matrix, at 390×844 and 1280×800: axe
 * (WCAG 2.0/2.1/2.2 A and AA) must report 0 critical and 0 serious, and the
 * page must match its `toHaveScreenshot` baseline in
 * `e2e/portal/__screenshots__/` (per platform; a platform without baselines
 * skips the pixel compare, as in Phase 1, until recorded with
 * `PORTAL_RECORD=1 … --update-snapshots`).
 *
 * Writes are refused (418) by the fixture, so the request-dialog submit
 * errors with server codes (409, 429, 422) are covered by the RTL suite;
 * here the dialog's own states (populated, empty, loading, error,
 * unavailable) are captured.
 *
 * Set PORTAL_SHOTS_DIR to also write plain PNG copies (evidence folder).
 * Fixture-backed: run with -c playwright.portal.config.ts.
 */
import { existsSync } from "node:fs";
import path from "node:path";
import { expect, type Page, test } from "@playwright/test";
import {
  onlyWithPortalConfig,
  openPortal,
  seriousAxe,
  VIEWPORTS,
} from "./fixtures/helpers";
import { installPortalFake, NOW, type Scenario } from "./fixtures/portalFake";

interface State {
  id: string;
  path: string;
  scenario?: Scenario;
  /** What to wait for before scanning. */
  ready: string;
  /** Signed-out pages (sign-in, set password). */
  public?: boolean;
  /** Steps after load (open the dialog, submit a form). */
  act?: (page: Page) => Promise<void>;
}

const FIRST = "main [data-portal-first]";
const LOADING = '[role="status"][aria-label^="Loading"]';
const ERROR = "main [role=alert]";
const PENDING = "pending" as const;
const E500 = { status: 500 };

const STATES: State[] = [
  // Home
  { id: "home", path: "/portal", ready: "[data-tank-row]" },
  {
    id: "home-empty",
    path: "/portal",
    scenario: {
      tanks: { data: [], next_cursor: null, limit: 25, request_id: "e" },
      orders: { data: [], next_cursor: null, limit: 25, request_id: "e" },
      invoices: { data: [], next_cursor: null, limit: 25, request_id: "e" },
      me: { open_balance_cents: 0, open_invoice_count: 0, overdue_count: 0 },
    },
    ready: "text=No tanks are set up yet.",
  },
  {
    id: "home-loading",
    path: "/portal",
    scenario: { tanks: PENDING, orders: PENDING, invoices: PENDING },
    ready: LOADING,
  },
  {
    id: "home-error",
    path: "/portal",
    scenario: { tanks: E500, orders: E500, invoices: E500 },
    ready: ERROR,
  },
  {
    id: "home-ordering-off",
    path: "/portal",
    scenario: { me: { ordering_available: false } },
    ready: "[data-tank-row]",
  },
  {
    id: "home-invoices-off",
    path: "/portal",
    scenario: { me: { invoices_available: false, payments_available: false } },
    ready: "[data-tank-row]",
  },
  {
    id: "home-payments-off",
    path: "/portal",
    scenario: { me: { payments_available: false } },
    ready: "[data-tank-row]",
  },
  // Orders
  { id: "orders", path: "/portal/orders", ready: FIRST },
  {
    id: "orders-empty",
    path: "/portal/orders",
    scenario: {
      orders: { data: [], next_cursor: null, limit: 25, request_id: "e" },
    },
    ready: "text=No orders yet.",
  },
  {
    id: "orders-loading",
    path: "/portal/orders",
    scenario: { orders: PENDING },
    ready: LOADING,
  },
  {
    id: "orders-error",
    path: "/portal/orders",
    scenario: { orders: E500 },
    ready: ERROR,
  },
  // Request dialog
  {
    id: "request",
    path: "/portal/orders/new?tank=QA-TANK-CRIT",
    ready: '[role="dialog"] [role="radiogroup"]',
  },
  {
    id: "request-gallons",
    path: "/portal/orders/new?tank=QA-TANK-WARN",
    ready: '[role="dialog"] [role="radiogroup"]',
    act: async (page) => {
      await page.getByLabel("Gallons", { exact: true }).check();
      await page.getByLabel("Gallons requested").fill("900");
    },
  },
  {
    id: "request-validation",
    path: "/portal/orders/new",
    ready: '[role="dialog"] [role="radiogroup"]',
    act: async (page) => {
      await page.getByRole("button", { name: "Send request" }).click();
      await page.locator('[role="dialog"] [role="alert"]').waitFor();
    },
  },
  {
    id: "request-empty",
    path: "/portal/orders/new",
    scenario: {
      tanks: { data: [], next_cursor: null, limit: 25, request_id: "e" },
    },
    ready: "text=No tanks are set up yet.",
  },
  {
    id: "request-loading",
    path: "/portal/orders/new",
    scenario: { tanks: PENDING },
    ready: `[role="dialog"] ${LOADING}`,
  },
  {
    id: "request-error",
    path: "/portal/orders/new",
    scenario: { tanks: E500 },
    ready: '[role="dialog"] [role="alert"]',
  },
  {
    id: "request-unavailable",
    path: "/portal/orders/new",
    scenario: { me: { ordering_available: false } },
    ready: '[role="dialog"] [role="radiogroup"]',
  },
  // Invoices
  {
    id: "invoices",
    path: "/portal/invoices",
    ready: "[data-invoice-row], main tbody tr",
  },
  {
    id: "invoices-empty",
    path: "/portal/invoices",
    scenario: {
      invoices: { data: [], next_cursor: null, limit: 25, request_id: "e" },
    },
    ready: "text=No invoices to show.",
  },
  {
    id: "invoices-loading",
    path: "/portal/invoices",
    scenario: { invoices: PENDING },
    ready: LOADING,
  },
  {
    id: "invoices-error",
    path: "/portal/invoices",
    scenario: { invoices: E500 },
    ready: ERROR,
  },
  {
    id: "invoices-off",
    path: "/portal/invoices",
    scenario: { me: { invoices_available: false } },
    ready: FIRST,
  },
  // Invoice detail
  { id: "invoice-open", path: "/portal/invoices/QA-INV-OPEN", ready: FIRST },
  {
    id: "invoice-partial-pending",
    path: "/portal/invoices/QA-INV-PART",
    ready: FIRST,
  },
  { id: "invoice-paid", path: "/portal/invoices/QA-INV-PAID", ready: FIRST },
  { id: "invoice-void", path: "/portal/invoices/QA-INV-VOID", ready: FIRST },
  {
    id: "invoice-loading",
    path: "/portal/invoices/QA-INV-OPEN",
    scenario: { invoice: PENDING },
    ready: LOADING,
  },
  {
    id: "invoice-404",
    path: "/portal/invoices/QA-INV-MISSING",
    ready: ERROR,
  },
  {
    id: "invoice-500",
    path: "/portal/invoices/QA-INV-OPEN",
    scenario: { invoice: E500 },
    ready: ERROR,
  },
  {
    id: "invoice-payments-off",
    path: "/portal/invoices/QA-INV-OPEN",
    scenario: { me: { payments_available: false } },
    ready: FIRST,
  },
  // Pay (render only; Stripe is blocked)
  {
    id: "pay",
    path: "/portal/invoices/QA-INV-OPEN/pay",
    ready: "text=Payment amount (USD)",
  },
  {
    id: "pay-loading",
    path: "/portal/invoices/QA-INV-OPEN/pay",
    scenario: { invoice: PENDING },
    ready: LOADING,
  },
  {
    id: "pay-error",
    path: "/portal/invoices/QA-INV-OPEN/pay",
    scenario: { invoice: E500 },
    ready: ERROR,
  },
  {
    id: "pay-payments-off",
    path: "/portal/invoices/QA-INV-OPEN/pay",
    scenario: { me: { payments_available: false } },
    ready: FIRST,
  },
  // Tanks and tank detail
  { id: "tanks", path: "/portal/tanks", ready: "[data-tank-row]" },
  {
    id: "tanks-empty",
    path: "/portal/tanks",
    scenario: {
      tanks: { data: [], next_cursor: null, limit: 25, request_id: "e" },
    },
    ready: "text=No tanks are set up yet.",
  },
  {
    id: "tanks-loading",
    path: "/portal/tanks",
    scenario: { tanks: PENDING },
    ready: LOADING,
  },
  {
    id: "tanks-error",
    path: "/portal/tanks",
    scenario: { tanks: E500 },
    ready: ERROR,
  },
  { id: "tank-warning", path: "/portal/tanks/QA-TANK-WARN", ready: FIRST },
  { id: "tank-critical", path: "/portal/tanks/QA-TANK-CRIT", ready: FIRST },
  { id: "tank-stale-fallback", path: "/portal/tanks/QA-TANK-OK", ready: FIRST },
  {
    id: "tank-loading",
    path: "/portal/tanks/QA-TANK-WARN",
    scenario: { tank: PENDING },
    ready: LOADING,
  },
  { id: "tank-404", path: "/portal/tanks/QA-TANK-MISSING", ready: ERROR },
  {
    id: "tank-500",
    path: "/portal/tanks/QA-TANK-WARN",
    scenario: { tank: E500 },
    ready: ERROR,
  },
  // Account (PE6)
  { id: "account", path: "/portal/account", ready: FIRST },
  // Shared auth pages
  { id: "signin", path: "/signin", public: true, ready: "form" },
  {
    id: "signin-error",
    path: "/signin",
    public: true,
    ready: "form",
    act: async (page) => {
      await page.getByLabel("Email address").fill("qa-portal@example.test");
      await page.getByLabel(/^password/i).fill("wrong-password");
      await page.getByRole("button", { name: "Sign in" }).click();
      await page.locator("main [role=alert]").waitFor();
    },
  },
  {
    id: "set-password",
    path: "/auth/reset-password?token=x",
    public: true,
    ready: "form",
  },
  {
    id: "set-password-invite",
    path: "/auth/reset-password?token=x&invite=1",
    public: true,
    ready: "form",
  },
  {
    id: "set-password-mismatch",
    path: "/auth/reset-password?token=x",
    public: true,
    ready: "form",
    act: async (page) => {
      await page.getByLabel("New password").fill("QaPass-1234");
      await page.getByLabel("Confirm password").fill("QaPass-9999");
      await page.getByRole("button", { name: "Set password" }).click();
      await page.locator("main [role=alert]").waitFor();
    },
  },
  {
    id: "set-password-no-token",
    path: "/auth/reset-password",
    public: true,
    ready: "h1",
  },
  {
    id: "forgot-password",
    path: "/auth/forgot-password",
    public: true,
    ready: "form",
  },
];

test.describe("portal visual and axe", () => {
  onlyWithPortalConfig();

  for (const s of STATES) {
    for (const vp of VIEWPORTS) {
      const name = `${s.id}-${vp.width}x${vp.height}`;
      test(name, async ({ page }) => {
        if (s.public) {
          await page.clock.setFixedTime(NOW);
          await installPortalFake(page, s.scenario);
          await page.setViewportSize(vp);
          await page.goto(s.path);
        } else {
          await openPortal(page, s.path, vp, s.scenario);
        }
        await page.locator(s.ready).first().waitFor({ timeout: 30_000 });
        if (s.act) await s.act(page);
        await page.waitForTimeout(300);

        const found = await seriousAxe(page);
        await test.info().attach("axe", {
          body: JSON.stringify(found, null, 2),
          contentType: "application/json",
        });
        expect(found, JSON.stringify(found, null, 2)).toEqual([]);

        const dir = process.env.PORTAL_SHOTS_DIR;
        if (dir)
          await page.screenshot({
            path: `${dir}/portal-${name}.png`,
            fullPage: false,
          });

        const baseline = path.join(
          __dirname,
          "__screenshots__",
          `${name}-${process.platform}.png`,
        );
        if (existsSync(baseline) || process.env.PORTAL_RECORD) {
          await expect(page).toHaveScreenshot(`${name}.png`, {
            animations: "disabled",
          });
        }
      });
    }
  }
});
