/**
 * R10.5: every portal page reflows at 320 CSS px without horizontal scrolling.
 * Runs on staging only (F5); skips without the portal env.
 */
import { expect, test } from "@playwright/test";
import {
  discoverDetailPages,
  PORTAL_PAGES,
  signInWithKeyboard,
  skipWithoutPortalEnv,
  waitForPortal,
} from "./portal-env";

test.describe("customer portal 320 px reflow", () => {
  skipWithoutPortalEnv();

  test("no portal page scrolls sideways at 320 px", async ({ page }) => {
    await signInWithKeyboard(page);
    const pages = [...PORTAL_PAGES, ...(await discoverDetailPages(page))];
    await page.setViewportSize({ width: 320, height: 800 });
    const overflowing: Record<
      string,
      { scrollWidth: number; clientWidth: number }
    > = {};
    for (const path of pages) {
      await page.goto(path);
      await waitForPortal(page);
      const size = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      if (size.scrollWidth > size.clientWidth) overflowing[path] = size;
    }
    expect(overflowing).toEqual({});
  });
});
