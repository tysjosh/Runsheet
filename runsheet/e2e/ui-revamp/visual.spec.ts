/**
 * Visual baselines (design.md §10): `toHaveScreenshot` at 1280×800 and
 * 1440×900, with the clock fixed so dates and relative times are stable.
 * Baselines live in `e2e/ui-revamp/__screenshots__/`; refresh them with
 * `--update-snapshots` when a page is redesigned on purpose.
 *
 * Set UI_REVAMP_SHOTS_DIR to also write plain PNG copies (used for the
 * phase evidence folder).
 *
 * Baselines are per platform (`<name>-<platform>.png`) because font
 * rendering differs between macOS and Linux. A platform without recorded
 * baselines (Linux CI today) skips the pixel comparison instead of failing on
 * a missing file; record them there with
 * `UI_REVAMP_RECORD=1 npx playwright test -c playwright.ui-revamp.config.ts
 * visual --update-snapshots`.
 */
import { existsSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { VIEWPORTS } from "./pages";
import { installShellFake, signIn } from "./shellFake";

const PAGES: { id: string; path: string; ready: string; roles?: string[] }[] = [
  { id: "dashboard", path: "/dashboard", ready: "[data-feed-row]" },
  {
    id: "dispatch-board",
    path: "/dashboard/dispatch?tab=board",
    ready: '[role="grid"] [role="row"]',
  },
  { id: "orders", path: "/dashboard/orders", ready: "tbody tr" },
  {
    id: "dispatch-jobs",
    path: "/dashboard/dispatch?tab=jobs",
    ready: "tbody tr",
  },
  {
    id: "dispatch-plans",
    path: "/dashboard/dispatch?tab=plans",
    ready: "tbody tr",
  },
  { id: "live", path: "/dashboard/control", ready: "[data-feed-row]" },
  {
    id: "billing-invoices",
    path: "/dashboard/billing?tab=invoices",
    ready: "tbody tr",
  },
  { id: "settings", path: "/dashboard/settings?tab=company", ready: "h1" },
  {
    id: "fuel-stations",
    path: "/dashboard/fuel-ops?tab=stations",
    ready: "tbody tr",
  },
  { id: "analytics", path: "/dashboard/analytics", ready: "h1" },
  {
    id: "fleet-trucks",
    path: "/dashboard/fleet?tab=trucks",
    ready: "tbody tr",
  },
  {
    id: "fleet-drivers",
    path: "/dashboard/fleet?tab=drivers",
    ready: "tbody tr",
  },
  {
    id: "fleet-drivers-qualifications",
    path: "/dashboard/fleet?tab=drivers&view=qualifications",
    ready: "tbody tr",
  },
  {
    id: "fleet-inventory",
    path: "/dashboard/fleet?tab=inventory",
    ready: "tbody tr",
  },
  { id: "customers", path: "/dashboard/customers", ready: "tbody tr" },
  {
    id: "customers-communications",
    path: "/dashboard/customers?tab=communications",
    ready: "tbody tr",
  },
  // Phase 3 iteration 3 (Compliance, Billing, Settings).
  {
    id: "compliance-certifications",
    path: "/dashboard/compliance?tab=certifications",
    ready: "tbody tr",
  },
  {
    id: "compliance-meters",
    path: "/dashboard/compliance?tab=meters",
    ready: "tbody tr",
  },
  {
    id: "compliance-bols",
    path: "/dashboard/compliance?tab=bols",
    ready: "tbody tr",
  },
  {
    id: "compliance-ifta",
    path: "/dashboard/compliance?tab=ifta",
    ready: "tbody tr",
  },
  {
    id: "billing-accounts",
    roles: ["admin", "dispatcher", "platform_admin"],
    path: "/dashboard/billing?tab=accounts",
    ready: "tbody tr",
  },
  {
    id: "billing-payments",
    roles: ["admin", "dispatcher", "platform_admin"],
    path: "/dashboard/billing?tab=payments",
    ready: "tbody tr",
  },
  {
    id: "billing-ar-aging",
    roles: ["admin", "dispatcher", "platform_admin"],
    path: "/dashboard/billing?tab=ar-aging",
    ready: "tbody tr",
  },
  {
    id: "billing-reconciliation",
    path: "/dashboard/billing?tab=reconciliation",
    ready: "tbody tr",
  },
  {
    id: "billing-price-books",
    roles: ["admin", "dispatcher", "platform_admin"],
    path: "/dashboard/billing?tab=price-books",
    ready: "tbody tr",
  },
  {
    id: "billing-pricing-rules",
    roles: ["admin", "dispatcher", "platform_admin"],
    path: "/dashboard/billing?tab=pricing-rules",
    ready: "tbody tr",
  },
  {
    id: "billing-contracts",
    roles: ["admin", "dispatcher", "platform_admin"],
    path: "/dashboard/billing?tab=contracts",
    ready: "tbody tr",
  },
  {
    id: "billing-margin",
    path: "/dashboard/billing?tab=margin",
    ready: "tbody tr",
  },
  {
    id: "settings-flags",
    path: "/dashboard/settings?tab=flags",
    ready: "tbody tr",
  },
  {
    id: "settings-tax",
    path: "/dashboard/settings?tab=tax",
    ready: "tbody tr",
  },
  {
    id: "settings-exemptions",
    path: "/dashboard/settings?tab=exemptions",
    ready: "tbody tr",
  },
  {
    id: "settings-system",
    path: "/dashboard/settings?tab=system",
    ready: "dl",
    roles: ["admin", "platform_admin"],
  },
];

const BOARD_1024 = { width: 1024, height: 768 } as const;
for (const p of PAGES) {
  const vps =
    p.id === "dispatch-board" ? [BOARD_1024, ...VIEWPORTS] : VIEWPORTS;
  for (const vp of vps) {
    test(`${p.id} ${vp.width}x${vp.height}`, async ({ page, context }) => {
      const baseline = path.join(
        __dirname,
        "__screenshots__",
        `${p.id}-${vp.width}x${vp.height}-${process.platform}.png`,
      );
      test.skip(
        !existsSync(baseline) && !process.env.UI_REVAMP_RECORD,
        `no ${process.platform} baseline recorded`,
      );
      await page.clock.setFixedTime(new Date("2026-10-08T14:00:00Z"));
      await signIn(context, p.roles);
      await installShellFake(page);
      await page.setViewportSize(vp);
      await page.goto(p.path);
      await page.locator(p.ready).first().waitFor();
      await page.waitForLoadState("networkidle").catch(() => {});
      await page.waitForTimeout(500);
      const name = `${p.id}-${vp.width}x${vp.height}.png`;
      const dir = process.env.UI_REVAMP_SHOTS_DIR;
      if (dir) await page.screenshot({ path: `${dir}/${name}` });
      await expect(page).toHaveScreenshot(name, { animations: "disabled" });
    });
  }
}
