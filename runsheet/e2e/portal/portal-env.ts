/**
 * Shared setup for the customer-portal Playwright specs (FREEZE F5).
 *
 * These specs run against staging in the verify step, never in CI. They read
 * the target and a QA portal login from the environment and skip cleanly when
 * any of them is missing:
 *
 *   PLAYWRIGHT_BASE_URL   the staging UI origin
 *   PORTAL_QA_EMAIL       a QA- customer portal user
 *   PORTAL_QA_PASSWORD
 *
 * Example: PLAYWRIGHT_BASE_URL=... PORTAL_QA_EMAIL=... PORTAL_QA_PASSWORD=...
 *          npx playwright test e2e/portal --project=chromium
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

export const PORTAL_ENV = {
  baseUrl: process.env.PLAYWRIGHT_BASE_URL ?? "",
  email: process.env.PORTAL_QA_EMAIL ?? "",
  password: process.env.PORTAL_QA_PASSWORD ?? "",
};

export const PORTAL_ENV_READY = Boolean(
  PORTAL_ENV.baseUrl && PORTAL_ENV.email && PORTAL_ENV.password,
);

/** Call at the top of every portal describe block. */
export function skipWithoutPortalEnv(): void {
  test.skip(
    !PORTAL_ENV_READY,
    "Set PLAYWRIGHT_BASE_URL, PORTAL_QA_EMAIL and PORTAL_QA_PASSWORD to run the portal specs against staging.",
  );
}

/** The static portal pages; detail pages are discovered from the lists. */
export const PORTAL_PAGES = [
  "/portal",
  "/portal/orders",
  "/portal/orders/new",
  "/portal/invoices",
  "/portal/tanks",
  "/portal/account",
] as const;

/**
 * Tab until `#password` has focus, at most 5 presses (V1 fix: the helper no
 * longer assumes the field is exactly one Tab away). Fails with the name of
 * whatever has focus instead.
 */
export async function tabToPassword(page: Page): Promise<number> {
  for (let presses = 1; presses <= 5; presses += 1) {
    await page.keyboard.press("Tab");
    const onPassword = await page.evaluate(
      () => document.activeElement?.id === "password",
    );
    if (onPassword) return presses;
  }
  const focused = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el) return "nothing";
    return (
      el.getAttribute("aria-label") ||
      el.id ||
      el.textContent?.trim() ||
      el.tagName
    );
  });
  throw new Error(
    `Password field not reached after 5 Tab presses; focus is on "${focused}".`,
  );
}

/** Sign in with the keyboard only: type, Tab between fields, Enter to submit. */
export async function signInWithKeyboard(page: Page): Promise<void> {
  await page.goto("/signin");
  await page.getByLabel("Email address").focus();
  await page.keyboard.type(PORTAL_ENV.email);
  await tabToPassword(page);
  await expect(page.getByLabel(/^password/i)).toBeFocused();
  await page.keyboard.type(PORTAL_ENV.password);
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/portal(\/|$)/, { timeout: 30_000 });
  await waitForPortal(page);
}

/** Wait until the portal shell and the page's own loading text are done. */
export async function waitForPortal(page: Page): Promise<void> {
  await expect(page.getByRole("navigation", { name: "Portal" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText(/^Loading/)).toHaveCount(0, { timeout: 30_000 });
}

async function firstHref(page: Page, selector: string): Promise<string | null> {
  const links = page.locator(selector);
  if ((await links.count()) === 0) return null;
  return links.first().getAttribute("href");
}

/** Detail pages reachable from the lists (first invoice, its pay page, first tank). */
export async function discoverDetailPages(page: Page): Promise<string[]> {
  const found: string[] = [];
  await page.goto("/portal/invoices");
  await waitForPortal(page);
  const invoiceHref = await firstHref(page, 'a[href^="/portal/invoices/"]');
  if (invoiceHref) {
    found.push(invoiceHref);
    found.push(`${invoiceHref}/pay`);
  }
  await page.goto("/portal/tanks");
  await waitForPortal(page);
  const tankHref = await firstHref(page, 'a[href^="/portal/tanks/"]');
  if (tankHref) found.push(tankHref);
  return found;
}

/** axe violations with serious or critical impact (R10.6). */
export async function seriousViolations(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    // Stripe's Payment Element iframe has its own accessibility (§10.2).
    .exclude('iframe[name^="__privateStripe"]')
    .analyze();
  return results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => ({
      id: v.id,
      impact: v.impact,
      targets: v.nodes.map((n) => n.target.join(" ")),
    }));
}
