/**
 * Phase 3 (tasks 3.4, 3.5, 3.8, review iteration 3): axe on every migrated
 * create/edit FormDialog while it is open, plus whole-page axe on the
 * Settings sections that aren't list pages in design.md §6 (so they aren't in
 * `pages.ts`). Same tags and threshold as axe.spec: 0 critical/serious.
 *
 * Each dialog opens from its title-row action and must be a labelled modal
 * dialog that Escape closes.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import { installShellFake, signIn } from "./shellFake";

const TAGS = ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"];

async function scan(page: Page, include?: string) {
  let b = new AxeBuilder({ page }).withTags(TAGS);
  if (include) b = b.include(include);
  const r = await b.analyze();
  return r.violations
    .filter((v) => v.impact === "critical" || v.impact === "serious")
    .map((v) => ({
      id: v.id,
      impact: v.impact,
      nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
    }));
}

const DIALOGS: {
  id: string;
  path: string;
  trigger: RegExp;
  dialog: string | RegExp;
  roles?: string[];
}[] = [
  {
    id: "customer-create",
    path: "/dashboard/customers",
    trigger: /^New customer$/,
    dialog: "New customer",
  },
  {
    id: "certification-add",
    path: "/dashboard/compliance?tab=certifications",
    trigger: /^Add certification$/i,
    dialog: "Add certification",
  },
  {
    id: "meter-register",
    path: "/dashboard/compliance?tab=meters",
    trigger: /^Register meter$/,
    dialog: "Register meter",
  },
  {
    id: "bol-upload",
    path: "/dashboard/compliance?tab=bols",
    trigger: /^Upload BOL$/,
    dialog: "Upload terminal BOL",
  },
  {
    id: "price-book-new",
    roles: ["admin", "dispatcher", "platform_admin"],
    path: "/dashboard/billing?tab=price-books",
    trigger: /^New price book$/,
    dialog: /price book/i,
  },
  {
    id: "pricing-rule-add",
    roles: ["admin", "dispatcher", "platform_admin"],
    path: "/dashboard/billing?tab=pricing-rules",
    trigger: /^Add rule$/i,
    dialog: "Add pricing rule",
  },
  {
    id: "contract-new",
    roles: ["admin", "dispatcher", "platform_admin"],
    path: "/dashboard/billing?tab=contracts",
    trigger: /^Add contract$/i,
    dialog: /contract/i,
  },
  {
    id: "tax-rate-add",
    path: "/dashboard/settings?tab=tax",
    trigger: /^Add rate$/i,
    dialog: "Add jurisdiction rate",
  },
  {
    id: "exemption-add",
    path: "/dashboard/settings?tab=exemptions",
    trigger: /^Add exemption$/i,
    dialog: "Add exemption certificate",
  },
  {
    id: "depot-new",
    path: "/dashboard/settings?tab=company",
    trigger: /^(New|Add) depot$/i,
    dialog: /depot/i,
  },
  {
    id: "intake-channel-register",
    path: "/dashboard/settings?tab=intake-channels",
    trigger: /^Register channel$/i,
    dialog: "Register channel",
  },
  {
    id: "road-restriction-upload",
    path: "/dashboard/settings?tab=road-restrictions",
    trigger: /^Upload restriction$/i,
    dialog: "Upload road restriction",
  },
];

for (const d of DIALOGS) {
  test(`dialog ${d.id}: labelled, Escape closes, axe 0 critical/serious`, async ({
    page,
    context,
  }) => {
    await signIn(context, d.roles);
    await installShellFake(page);
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(d.path);
    const trigger = page
      .locator('[data-chrome="titlerow"]')
      .getByRole("button", { name: d.trigger })
      .first();
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: d.dialog });
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("aria-modal", "true");
    await page.waitForTimeout(250);
    const found = await scan(page, '[role="dialog"]');
    await test.info().attach("violations", {
      body: JSON.stringify(found, null, 2),
      contentType: "application/json",
    });
    expect(found).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });
}

const SECTIONS = [
  "road-restrictions",
  "weather-alerts",
  "notifications",
  "integrations",
  "intake-channels",
  "stripe",
  "agents",
  "import",
  "metrics",
];

for (const s of SECTIONS) {
  test(`settings ${s}: whole page axe 0 critical/serious`, async ({
    page,
    context,
  }) => {
    await signIn(context);
    await installShellFake(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/dashboard/settings?tab=${s}`);
    await expect(page.locator("h1")).toHaveText("Settings");
    await page.waitForLoadState("networkidle").catch(() => {});
    await page.waitForTimeout(250);
    const found = await scan(page);
    await test.info().attach("violations", {
      body: JSON.stringify(found, null, 2),
      contentType: "application/json",
    });
    expect(found).toEqual([]);
  });
}
