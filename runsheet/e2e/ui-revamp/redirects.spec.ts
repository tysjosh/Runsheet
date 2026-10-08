/**
 * Retired routes (R3.1–R3.3, design.md §4): one test per redirect row. Loads
 * the old URL, follows the 308, and asserts the final URL (dynamic segment
 * and query preserved) and the destination's heading. Detail destinations use
 * an id the fake answers 404 for, so their own "… not found" state proves
 * which page rendered.
 */
import { expect, test } from "@playwright/test";
import { REDIRECTS } from "../../src/config/redirects";
import { installShellFake, MISSING_ID, signIn } from "./shellFake";

/** Expected heading per destination (h1 for hubs, detail state otherwise). */
const EXPECT: Record<number, { h1?: string; heading?: RegExp; tab?: string }> =
  {
    0: { h1: "Today" },
    1: { h1: "Dispatch", tab: "Jobs" },
    2: { heading: /job/i },
    3: { heading: /job|cargo/i },
    4: { h1: "Live" },
    6: { h1: "Fuel", tab: "Stations" },
    7: { heading: /depot/i },
    8: { heading: /tank/i },
    9: { h1: "Fleet", tab: "Inventory" },
    10: { h1: "Billing" },
    11: { h1: "Customers", tab: "Customers" },
    12: { heading: /customer/i },
    13: { heading: /invoice/i },
    14: { heading: /account/i },
    15: { h1: "Billing", tab: "AR Aging" },
    16: { heading: /order/i },
    17: { heading: /terminal/i },
    18: { h1: "Settings" },
    19: { h1: "Settings" },
    20: { h1: "Fleet", tab: "Drivers" },
    21: { h1: "Customers", tab: "Communications" },
    22: { h1: "Settings" },
    23: { h1: "Settings" },
  };

const concrete = (p: string) => p.replace(/:[a-zA-Z]+/g, MISSING_ID);

for (const r of REDIRECTS) {
  test(`row ${r.row}: ${r.source} → ${r.destination}`, async ({
    page,
    context,
  }) => {
    // Staff shape: platform_admin alongside admin (sees the Tier 4 tabs).
    await signIn(context, ["admin", "dispatcher", "platform_admin"]);
    await installShellFake(page);
    const from = `${concrete(r.source)}?qa=1`;
    const res = await page.request.get(from, { maxRedirects: 0 });
    expect(res.status()).toBe(308);
    await page.goto(from);
    const want = new URL(concrete(r.destination), "http://x");
    await expect.poll(() => new URL(page.url()).pathname).toBe(want.pathname);
    const got = new URL(page.url());
    for (const [k, v] of want.searchParams)
      expect(got.searchParams.get(k)).toBe(v);
    expect(got.searchParams.get("qa")).toBe("1");
    const e = EXPECT[r.row];
    if (e.h1) await expect(page.locator("h1")).toHaveText(e.h1);
    if (e.heading)
      await expect(
        page.getByRole("heading", { name: e.heading }).first(),
      ).toBeVisible();
    if (e.tab)
      await expect(page.getByRole("tab", { name: e.tab })).toHaveAttribute(
        "aria-selected",
        "true",
      );
  });
}
