/**
 * Portal chrome budget (R14.5, R14.6; AC 4, AC 5): the first content block
 * starts ≤ 120 px from the top at 390×844 and ≤ 136 px at 1280×800 on the
 * six pages; the bottom tab bar exists on phones (items ≥ 44 px, top bar
 * 56 px) and not on desktop, where the tabs sit in the 56 px top bar.
 *
 * Fixture-backed: run with -c playwright.portal.config.ts.
 */
import { expect, test } from "@playwright/test";
import {
  BUDGET,
  DESKTOP,
  firstContentTop,
  onlyWithPortalConfig,
  openPortal,
  PHONE,
  VIEWPORTS,
} from "./fixtures/helpers";

const PAGES = [
  { id: "home", path: "/portal" },
  { id: "orders", path: "/portal/orders" },
  { id: "invoices", path: "/portal/invoices" },
  { id: "tanks", path: "/portal/tanks" },
  { id: "tank-detail", path: "/portal/tanks/QA-TANK-WARN" },
  { id: "invoice-detail", path: "/portal/invoices/QA-INV-OPEN" },
];

test.describe("portal chrome budget", () => {
  onlyWithPortalConfig();

  for (const p of PAGES) {
    for (const vp of VIEWPORTS) {
      test(`${p.id} ${vp.width}x${vp.height}: first content ≤ ${BUDGET[vp.width]} px`, async ({
        page,
      }) => {
        await openPortal(page, p.path, vp);
        await page.locator("main [data-portal-first]").first().waitFor();
        const top = await firstContentTop(page);
        test.info().annotations.push({
          type: "first-content-top",
          description: String(top),
        });
        expect(top).not.toBeNull();
        expect(top as number).toBeLessThanOrEqual(BUDGET[vp.width]);
      });
    }
  }

  test("phone: 56 px top bar and a bottom tab bar with 44 px items", async ({
    page,
  }) => {
    await openPortal(page, "/portal", PHONE);
    const bar = page.locator("header[data-portal-topbar]");
    expect((await bar.boundingBox())?.height).toBe(56);
    const tabs = page.locator("nav[data-portal-tabbar]");
    await expect(tabs).toBeVisible();
    const box = await tabs.boundingBox();
    expect(box && box.y + box.height).toBe(PHONE.height);
    const items = tabs.getByRole("link");
    await expect(items).toHaveCount(4);
    for (const item of await items.all()) {
      expect((await item.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(
        44,
      );
    }
    await expect(tabs.getByRole("link", { name: "Home" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test("phone with invoicing off: three tabs", async ({ page }) => {
    await openPortal(page, "/portal", PHONE, {
      me: { invoices_available: false },
    });
    await expect(
      page.locator("nav[data-portal-tabbar]").getByRole("link"),
    ).toHaveCount(3);
  });

  test("desktop: no bottom bar, tabs inside the 56 px top bar", async ({
    page,
  }) => {
    await openPortal(page, "/portal/orders", DESKTOP);
    await expect(page.locator("nav[data-portal-tabbar]")).toHaveCount(0);
    const header = page.locator("header[data-portal-topbar]");
    expect((await header.boundingBox())?.height).toBe(56);
    const nav = header.getByRole("navigation", { name: "Portal" });
    await expect(nav.getByRole("link")).toHaveCount(4);
    await expect(nav.getByRole("link", { name: "Orders" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test("phone: every interactive target on Home is ≥ 44 px tall", async ({
    page,
  }) => {
    await openPortal(page, "/portal", PHONE);
    await page.locator("[data-tank-row]").first().waitFor();
    const small = await page.evaluate(() =>
      Array.from(
        document.querySelectorAll<HTMLElement>(
          "main a[href], main button, header button, nav a",
        ),
      )
        .filter((el) => el.offsetParent !== null)
        .map((el) => ({
          name:
            el.getAttribute("aria-label") ||
            el.textContent?.trim() ||
            el.tagName,
          h: Math.round(el.getBoundingClientRect().height),
        }))
        .filter((t) => t.h < 44),
    );
    expect(small).toEqual([]);
  });
});
