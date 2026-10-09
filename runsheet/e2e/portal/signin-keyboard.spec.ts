/**
 * Sign-in keyboard order (V1, R14.17; AC 13): from Email, one Tab reaches
 * Password, then Show password, Sign in, Forgot password?. Enter submits the
 * form. Fixture-backed: run with -c playwright.portal.config.ts.
 */
import { expect, test } from "@playwright/test";
import { onlyWithPortalConfig } from "./fixtures/helpers";
import { installPortalFake } from "./fixtures/portalFake";
import { tabToPassword } from "./portal-env";

test.describe("sign-in keyboard order", () => {
  onlyWithPortalConfig();

  test("Email → Password → Show password → Sign in → Forgot password?", async ({
    page,
  }) => {
    await installPortalFake(page);
    await page.goto("/signin");
    await page.getByLabel("Email address").focus();
    // The helper the staging specs use reaches the field in one press now.
    expect(await tabToPassword(page)).toBe(1);
    await expect(page.locator("#password")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(
      page.getByRole("button", { name: "Show password" }),
    ).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "Sign in" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(
      page.getByRole("link", { name: "Forgot password?" }),
    ).toBeFocused();
  });

  test("Enter in the password field submits the form", async ({ page }) => {
    await installPortalFake(page);
    await page.goto("/signin");
    await page.getByLabel("Email address").fill("qa-portal@example.test");
    await page.locator("#password").fill("not-the-password");
    await page.locator("#password").press("Enter");
    // The fixture refuses the sign-in POST, so the form shows its error.
    await expect(page.locator("main [role=alert]")).toBeVisible();
  });

  test("every sign-in target is at least 24 px", async ({ page }) => {
    await installPortalFake(page);
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/signin");
    const small = await page.evaluate(() =>
      Array.from(
        document.querySelectorAll<HTMLElement>("a[href], button, input"),
      )
        .filter((el) => el.offsetParent !== null)
        .map((el) => {
          const r = el.getBoundingClientRect();
          return {
            name:
              el.getAttribute("aria-label") || el.textContent?.trim() || el.id,
            w: Math.round(r.width),
            h: Math.round(r.height),
          };
        })
        .filter((t) => t.w < 24 || t.h < 24),
    );
    expect(small).toEqual([]);
  });
});
