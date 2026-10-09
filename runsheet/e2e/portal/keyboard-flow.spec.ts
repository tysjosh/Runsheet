/**
 * R10.4: keyboard-only sign-in → new delivery request → cancel it (R4.10)
 * → invoice PDF download.
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

/**
 * R4.10: cancel the request this flow just sent (newest first, so the first
 * Cancel button), by keyboard, through the confirm dialog. This also removes
 * the QA request again. The
 * outcome is announced politely and focus stays in the list.
 */
async function cancelNewestRequest(page: Page): Promise<void> {
  await activateWithKeyboard(
    page,
    page.getByRole("link", { name: "Orders", exact: true }),
  );
  await page.waitForURL(/\/portal\/orders$/);
  await waitForPortal(page);
  // D29: named by tank and date, never the order id.
  const cancel = page.getByRole("button", { name: /^Cancel request for .+$/ });
  await expect(cancel.first()).toBeVisible();
  const before = await cancel.count();
  await activateWithKeyboard(page, cancel.first());
  // Owner decision 2026-10-09: a confirm step, "Keep request" focused first.
  const confirm = page.getByRole("dialog", {
    name: "Cancel this delivery request?",
  });
  await expect(confirm).toBeVisible();
  await expect(
    confirm.getByRole("button", { name: "Keep request" }),
  ).toBeFocused();
  await activateWithKeyboard(
    page,
    confirm.getByRole("button", { name: "Cancel request" }),
  );
  await expect(confirm).toBeHidden();
  await expect(
    page.getByRole("status").filter({
      hasText: /^Request for .+ cancelled\.$|^This request changed\./,
    }),
  ).toBeVisible({ timeout: 30_000 });
  await expect(cancel).toHaveCount(before - 1);
  // Focus is back on the row (a table row from 1024 px, a list item below).
  await expect(page.locator("main tr:focus, main li:focus")).toHaveCount(1);
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

  test("sign in, request a delivery, cancel it and download an invoice PDF", async ({
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
    // D24: Request delivery opens a dialog over the list.
    const trigger = page
      .getByRole("main")
      .getByRole("button", { name: "Request delivery", exact: true });
    await activateWithKeyboard(page, trigger);
    const dialog = page.getByRole("dialog", { name: "Request a delivery" });
    await expect(dialog).toBeVisible();

    const noTanks = dialog.getByText("No tanks are set up yet.");
    if ((await noTanks.count()) === 0) {
      await expect(
        dialog.getByRole("radiogroup", { name: "Tank" }),
      ).toBeVisible();
      const status = dialog.getByRole("status").filter({ hasText: PD10 });
      const orderingOff = (await status.count()) > 0;
      const submit = dialog.getByRole("button", { name: /send request/i });

      if (orderingOff) {
        // R14.12: the PD10 banner first, every field disabled, aria-disabled
        // submit, nothing sent.
        await expect(status).toBeVisible();
        await expect(dialog.getByLabel("Delivery date")).toBeDisabled();
        await expect(dialog.getByLabel("PO number (optional)")).toBeDisabled();
        await expect(submit).toHaveAttribute("aria-disabled", "true");
        await page.keyboard.press("Escape");
        await expect(dialog).toBeHidden();
        await expect(trigger).toBeFocused();
      } else {
        await dialog.getByLabel("Delivery date").focus();
        await page.keyboard.type(dateKeystrokesInDays(3));
        await dialog.getByLabel("PO number (optional)").focus();
        await page.keyboard.type("QA-E2E-PORTAL");
        await activateWithKeyboard(page, submit);
        const sent = page.getByRole("heading", { name: "Request sent" });
        await expect(
          sent.or(page.getByRole("status").filter({ hasText: PD10 })),
        ).toBeVisible({ timeout: 30_000 });
        if ((await sent.count()) > 0) {
          await activateWithKeyboard(
            page,
            dialog.getByRole("button", { name: "Close" }).last(),
          );
          await expect(dialog).toBeHidden();
          await cancelNewestRequest(page);
        } else {
          await page.keyboard.press("Escape");
        }
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
      .getByRole("table", { name: "Your invoices" })
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
