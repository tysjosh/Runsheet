/**
 * Task 3.2 acceptance in the browser: the station dialog is a FormDialog,
 * a 20,000 L station shows "5,283 gal", and Fuel type shows the cap and the
 * readable name with the code as secondary text, untruncated. Axe on the
 * open dialog: 0 critical/serious.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { firstRowTop } from "./measure";
import { VIEWPORTS } from "./pages";
import { installShellFake, signIn } from "./shellFake";

for (const vp of VIEWPORTS) {
  test(`edit station dialog at ${vp.width}x${vp.height}`, async ({
    page,
    context,
  }) => {
    await page.clock.setFixedTime(new Date("2026-10-08T14:00:00Z"));
    await signIn(context);
    await installShellFake(page);
    await page.setViewportSize(vp);
    await page.goto("/dashboard/fuel-ops?tab=stations");
    await page.locator("tbody tr").first().waitFor();
    const top = await firstRowTop(page);
    console.log(`fuel-stations firstRowTop ${vp.width}x${vp.height}: ${top}`);
    // Product names, never raw codes, in the list.
    const firstRow = page.locator("tbody tr").first();
    await expect(firstRow).not.toContainText("DIESEL_2");

    // QA-FS-100 is 20,000 L (diesel) in the fixtures.
    await page
      .getByRole("button", { name: "Actions for QA Station 100" })
      .click();
    await page.getByRole("menuitem", { name: "Edit station" }).click();
    const dialog = page.getByRole("dialog", { name: "Edit fuel station" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel(/^Capacity/)).toHaveValue("5,283");
    const fuel = dialog.getByRole("combobox", { name: /^Fuel type/ });
    await expect(fuel).toContainText("Diesel #2 (on-road)");
    // Not truncated: the label fits its control.
    const fits = await fuel.evaluate((el) => {
      const label = el.querySelector("[title]") ?? el;
      return label.scrollWidth <= label.clientWidth + 1;
    });
    expect(fits).toBe(true);
    await fuel.click();
    const option = page.getByRole("option", { name: /Regular unleaded/ });
    await expect(option).toContainText("GASOLINE_REG");
    await page.keyboard.press("Escape");
    const dir = process.env.UI_REVAMP_SHOTS_DIR;
    if (dir)
      await page.screenshot({
        path: `${dir}/fuel-station-dialog-${vp.width}x${vp.height}.png`,
      });
    const axe = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    const bad = axe.violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious",
    );
    expect(bad.map((v) => `${v.id}: ${v.nodes.length}`)).toEqual([]);
  });
}
