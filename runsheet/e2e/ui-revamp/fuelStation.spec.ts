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

// Review finding 5 (iteration 1): Fuel Stations sits at exactly 172 px, so
// nothing may grow the chrome. The toolbar is a fixed 44 px row (chips that
// don't fit collapse into "More"), and a partial load failure shows as a chip
// inside it, not a banner above the table.
test("fuel stations: toolbar stays one 44 px row with a load-failure notice at 1280", async ({
  page,
  context,
}) => {
  await signIn(context);
  await installShellFake(page);
  await page.route("**/api/fuel/metrics/summary**", (r) =>
    r.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error_code: "INTERNAL_ERROR", message: "boom" }),
    }),
  );
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/dashboard/fuel-ops?tab=stations");
  await page.locator("tbody tr").first().waitFor();
  await expect(page.getByText("Some data didn't load")).toBeVisible();
  const bar = page.locator('[data-chrome="toolbar"]').first();
  expect(Math.round((await bar.boundingBox())?.height ?? 0)).toBe(44);
  const top = await firstRowTop(page);
  expect(top as number).toBeLessThanOrEqual(172);
});
