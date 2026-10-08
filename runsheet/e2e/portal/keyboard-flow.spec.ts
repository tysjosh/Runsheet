/**
 * R10.4: keyboard-only sign-in → new delivery request → invoice PDF download.
 * Runs on staging only (F5); skips without the portal env.
 *
 * Staging may have online ordering off (`order_intake_pipeline` disabled,
 * owned by another agent); the request step then expects the PD10 message
 * instead of a created request, and submits nothing.
 */
import { expect, type Page, test } from "@playwright/test";
import {
  signInWithKeyboard,
  skipWithoutPortalEnv,
  waitForPortal,
} from "./portal-env";

const PD10 =
  "Online ordering is unavailable right now. Please contact your supplier to place this order.";

/** Move focus to `target` and activate it with Enter (no pointer events). */
async function activateWithKeyboard(
  page: Page,
  target: ReturnType<Page["getByRole"]>,
): Promise<void> {
  await target.focus();
  await expect(target).toBeFocused();
  await page.keyboard.press("Enter");
}

/** Keystrokes for a native date input in an en-US browser (MM DD YYYY). */
function dateKeystrokesInDays(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${mm}${dd}${d.getFullYear()}`;
}

test.describe("customer portal keyboard-only flow", () => {
  skipWithoutPortalEnv();

  test("sign in, request a delivery and download an invoice PDF", async ({
    page,
  }) => {
    await signInWithKeyboard(page);

    // The skip link is the first stop and moves focus to main content.
    await page.keyboard.press("Tab");
    await expect(
      page.getByRole("link", { name: "Skip to content" }),
    ).toBeFocused();

    // New request.
    await activateWithKeyboard(
      page,
      page.getByRole("link", { name: "Orders", exact: true }),
    );
    await page.waitForURL(/\/portal\/orders$/);
    await waitForPortal(page);
    await activateWithKeyboard(
      page,
      page.getByRole("link", { name: "Request delivery" }),
    );
    await page.waitForURL(/\/portal\/orders\/new$/);
    await waitForPortal(page);

    const noTanks = page.getByText("No tanks are set up for online ordering");
    if ((await noTanks.count()) === 0) {
      const status = page.getByRole("status").filter({ hasText: PD10 });
      const orderingOff = (await status.count()) > 0;

      await page.getByLabel("Delivery date").focus();
      await page.keyboard.type(dateKeystrokesInDays(3));
      await page.getByLabel("PO number (optional)").focus();
      await page.keyboard.type("QA-E2E-PORTAL");
      const submit = page.getByRole("button", { name: /send request/i });
      await activateWithKeyboard(page, submit);

      if (orderingOff) {
        await expect(status).toBeVisible();
        await expect(submit).toHaveAttribute("aria-disabled", "true");
        await expect(page.getByLabel("PO number (optional)")).toHaveValue(
          "QA-E2E-PORTAL",
        );
      } else {
        await expect(
          page
            .getByRole("heading", { name: "Request sent" })
            .or(page.getByRole("status").filter({ hasText: PD10 })),
        ).toBeVisible({ timeout: 30_000 });
      }
    }

    // Invoice PDF.
    await activateWithKeyboard(
      page,
      page.getByRole("link", { name: "Invoices", exact: true }),
    );
    await page.waitForURL(/\/portal\/invoices$/);
    await waitForPortal(page);
    const invoiceLinks = page
      .getByRole("table", { name: "Invoices" })
      .getByRole("link");
    test.skip(
      (await invoiceLinks.count()) === 0,
      "The QA customer has no invoices on staging.",
    );
    await activateWithKeyboard(page, invoiceLinks.first());
    await page.waitForURL(/\/portal\/invoices\/[^/]+$/);
    await waitForPortal(page);

    const downloadPromise = page.waitForEvent("download");
    await activateWithKeyboard(
      page,
      page.getByRole("button", { name: "Download PDF" }),
    );
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(
      /^invoice_[A-Za-z0-9_-]+\.pdf$/,
    );
  });
});
