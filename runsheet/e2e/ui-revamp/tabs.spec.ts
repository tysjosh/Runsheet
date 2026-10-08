/**
 * URL-synced tabs (R2.4, task 1.9): opening each hub's `?tab=` URL selects
 * that tab, and a reload keeps it.
 */
import { expect, test } from "@playwright/test";
import { installShellFake, signIn } from "./shellFake";

const CASES: [string, string, string][] = [
  ["/dashboard/dispatch?tab=board", "Dispatch", "Board"],
  ["/dashboard/dispatch?tab=jobs", "Dispatch", "Jobs"],
  ["/dashboard/dispatch?tab=plans", "Dispatch", "Plans"],
  ["/dashboard/dispatch?tab=scheduling", "Dispatch", "Jobs"],
  ["/dashboard/dispatch?tab=distribution", "Dispatch", "Plans"],
  ["/dashboard/fleet?tab=drivers", "Fleet", "Drivers"],
  ["/dashboard/fleet?tab=inventory", "Fleet", "Inventory"],
  ["/dashboard/customers?tab=communications", "Customers", "Communications"],
  ["/dashboard/fuel-ops?tab=kfactor", "Fuel", "K-Factor"],
  ["/dashboard/compliance?tab=ifta", "Compliance", "IFTA"],
  ["/dashboard/billing?tab=payments", "Billing", "Payments"],
  ["/dashboard/analytics?tab=scheduling", "Analytics", "Scheduling metrics"],
];

for (const [url, h1, tab] of CASES) {
  test(`${url} selects ${tab} and survives a reload`, async ({
    page,
    context,
  }) => {
    // Staff shape: platform_admin alongside admin (sees the Tier 4 tabs).
    await signIn(context, ["admin", "dispatcher", "platform_admin"]);
    await installShellFake(page);
    await page.goto(url);
    await expect(page.locator("h1")).toHaveText(h1);
    const t = page.getByRole("tab", { name: tab, exact: true });
    await expect(t).toHaveAttribute("aria-selected", "true");
    await page.reload();
    await expect(t).toHaveAttribute("aria-selected", "true");
  });
}

test("Settings sections are URLs", async ({ page, context }) => {
  // Staff shape: platform_admin alongside admin (sees the Tier 4 tabs).
  await signIn(context, ["admin", "dispatcher", "platform_admin"]);
  await installShellFake(page);
  await page.goto("/dashboard/settings?tab=company");
  await page.getByRole("button", { name: "Feature flags" }).click();
  await expect(page).toHaveURL(/\/dashboard\/settings\?tab=flags/);
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Feature flags" }),
  ).toHaveAttribute("aria-current", "page");
});
