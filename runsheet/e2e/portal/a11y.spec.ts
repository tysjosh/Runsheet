/**
 * R10.6: axe reports zero serious or critical violations on every portal page.
 * Runs on staging only (F5); skips without the portal env.
 */
import { expect, test } from "@playwright/test";
import {
  discoverDetailPages,
  PORTAL_PAGES,
  seriousViolations,
  signInWithKeyboard,
  skipWithoutPortalEnv,
  waitForPortal,
} from "./portal-env";

test.describe("customer portal accessibility (axe)", () => {
  skipWithoutPortalEnv();

  test("every portal page has no serious or critical axe violations", async ({
    page,
  }) => {
    await signInWithKeyboard(page);
    const pages = [...PORTAL_PAGES, ...(await discoverDetailPages(page))];
    const failures: Record<string, unknown> = {};
    for (const path of pages) {
      await page.goto(path);
      await waitForPortal(page);
      const violations = await seriousViolations(page);
      if (violations.length > 0) failures[path] = violations;
    }
    expect(failures).toEqual({});
  });
});
