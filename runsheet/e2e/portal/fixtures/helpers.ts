/** Shared steps for the portal fixture specs. */
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import {
  installPortalFake,
  NOW,
  type Scenario,
  signInCustomer,
} from "./portalFake";

export const PHONE = { width: 390, height: 844 };
export const DESKTOP = { width: 1280, height: 800 };
export const VIEWPORTS = [PHONE, DESKTOP] as const;

/** R14.6 budgets: the first content block's top edge. */
export const BUDGET = { 390: 120, 1280: 136 } as const;

export function onlyWithPortalConfig(): void {
  test.skip(
    process.env.PORTAL_FIXTURES !== "1",
    "Run with -c playwright.portal.config.ts (fixture-backed portal specs).",
  );
}

/** Open a portal page signed in as the fixture customer. */
export async function openPortal(
  page: Page,
  path: string,
  viewport: { width: number; height: number },
  scenario: Scenario = {},
): Promise<void> {
  // Surface uncaught page errors in the test output.
  page.on("pageerror", (err) => {
    console.log(`[pageerror] ${path}: ${err.message}\n${err.stack ?? ""}`);
  });
  await page.clock.setFixedTime(NOW);
  await signInCustomer(page.context());
  await installPortalFake(page, scenario);
  await page.setViewportSize(viewport);
  await page.goto(path);
  await expect(page.locator("main#main h1")).toBeVisible({ timeout: 30_000 });
}

/** Top of `main [data-portal-first]`, in CSS px from the viewport top. */
export async function firstContentTop(page: Page): Promise<number | null> {
  return page.evaluate(() => {
    const el = document.querySelector<HTMLElement>("main [data-portal-first]");
    return el ? Math.round(el.getBoundingClientRect().top) : null;
  });
}

const TAGS = ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"];

/** axe critical and serious findings (R14.20). */
export async function seriousAxe(page: Page) {
  const r = await new AxeBuilder({ page })
    .withTags(TAGS)
    .exclude('iframe[name^="__privateStripe"]')
    .analyze();
  return r.violations
    .filter((v) => v.impact === "critical" || v.impact === "serious")
    .map((v) => ({
      id: v.id,
      impact: v.impact,
      nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
    }));
}
