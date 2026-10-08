/**
 * Accessibility (R12.1): axe-core with the WCAG 2.0/2.1/2.2 A and AA tags.
 *
 * - The shell (top bar, sidebar, title row) must have zero critical or
 *   serious violations on every page now.
 * - The whole page must reach zero in the task named in `pages.ts`; until
 *   then it is marked `test.fail()` and its findings are attached.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import { SHELL_PAGES } from "./pages";
import { installShellFake, signIn } from "./shellFake";

const TAGS = ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"];
const SHELL = [
  '[data-chrome="topbar"]',
  'aside[aria-label="Sidebar navigation"]',
  '[data-chrome="titlerow"]',
];

async function scan(page: Page, include?: string[]) {
  let b = new AxeBuilder({ page }).withTags(TAGS);
  for (const sel of include ?? []) b = b.include(sel);
  const r = await b.analyze();
  return r.violations
    .filter((v) => v.impact === "critical" || v.impact === "serious")
    .map((v) => ({
      id: v.id,
      impact: v.impact,
      nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
    }));
}

for (const p of SHELL_PAGES) {
  test.describe(p.id, () => {
    test.beforeEach(async ({ page, context }) => {
      await signIn(context);
      await installShellFake(page);
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(p.path);
      await expect(page.locator("h1")).toHaveText(p.h1);
      if (p.ready) await page.locator(p.ready).first().waitFor();
      await page.waitForLoadState("networkidle").catch(() => {});
    });

    test("shell chrome: 0 critical/serious", async ({ page }) => {
      const found = await scan(page, SHELL);
      expect(found, JSON.stringify(found, null, 2)).toEqual([]);
    });

    test("whole page: 0 critical/serious", async ({ page }) => {
      test.fail(p.axeTask !== null, `page migrates in task ${p.axeTask}`);
      const found = await scan(page);
      await test.info().attach("violations", {
        body: JSON.stringify(found, null, 2),
        contentType: "application/json",
      });
      expect(found).toEqual([]);
    });
  });
}
