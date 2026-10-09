/**
 * Portal fixes 2026-10-09 (owner screenshot, desktop Orders at ~1024 px):
 * every portal page at 390×844, 1024×768, 1280×800 and 1440×900 with data
 * shaped like the owner's account (long customer name, long PO, a cancelled
 * request, a 275.5 gal delivery).
 *
 * On every page and size: no page-level horizontal scroll, no table wider
 * than its card, every truncated text carries a `title`, nothing is struck
 * through, no tenant slug or internal id in the text, axe 0 critical/serious.
 * Plus the specific fixes: names in the top bar not truncated at 1024 px,
 * the user's initials in the avatar, one date format with the zone on every
 * Orders row, one quantity pattern.
 *
 * PORTAL_FIXES_SHOTS_DIR writes the screenshots (evidence folder).
 * Fixture-backed: run with -c playwright.portal.config.ts.
 */
import { expect, type Page, test } from "@playwright/test";
import {
  onlyWithPortalConfig,
  openPortal,
  seriousAxe,
} from "./fixtures/helpers";
import { ORDERS, type Scenario } from "./fixtures/portalFake";

const SIZES = [
  { width: 390, height: 844 },
  { width: 1024, height: 768 },
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
] as const;

const OWNER_ORDERS = [
  {
    ...ORDERS[0],
    order_id: "QA-OWNER-REQ-1",
    fill_to_full: false,
    gallons_requested: 1600,
    po_number: "QA-OWNER-REQ-1-PO-WITH-A-LONG-REFERENCE",
  },
  ...ORDERS.slice(1, 4),
  {
    ...ORDERS[4],
    order_id: "QA-OWNER-DLV-2",
    delivered_gallons: 275.5,
    ticket_number: "QA-OWNER-TKT-1002-LONG",
  },
  ...ORDERS.slice(5),
];

const OWNER: Scenario = {
  me: {
    customer_display_name: "QA-OWNER Demo Fuels Customer",
    supplier_name: "Demo Fuels",
    email: "qa-owner.person@example.test",
  },
  orders: { data: OWNER_ORDERS, next_cursor: null, limit: 25, request_id: "e" },
};

const PAGES = [
  { id: "home", path: "/portal", ready: "[data-tank-row]" },
  { id: "orders", path: "/portal/orders", ready: "main [data-portal-first]" },
  {
    id: "request",
    path: "/portal/orders/new?tank=QA-TANK-WARN",
    ready: '[role="dialog"] [role="radiogroup"]',
  },
  {
    id: "invoices",
    path: "/portal/invoices",
    ready: "[data-invoice-row], main tbody tr",
  },
  {
    id: "invoice",
    path: "/portal/invoices/QA-INV-OPEN",
    ready: "main [data-portal-first]",
  },
  {
    id: "invoice-void",
    path: "/portal/invoices/QA-INV-VOID",
    ready: "main [data-portal-first]",
  },
  {
    id: "pay",
    path: "/portal/invoices/QA-INV-OPEN/pay",
    ready: "text=Payment amount (USD)",
  },
  { id: "tanks", path: "/portal/tanks", ready: "[data-tank-row]" },
  {
    id: "tank",
    path: "/portal/tanks/QA-TANK-WARN",
    ready: "main [data-portal-first]",
  },
  { id: "account", path: "/portal/account", ready: "main [data-portal-first]" },
];

/** Layout and content problems the owner flagged, as a list of findings. */
async function problems(page: Page) {
  return page.evaluate(() => {
    const out: string[] = [];
    const doc = document.documentElement;
    if (doc.scrollWidth > window.innerWidth + 1) {
      out.push(
        `page scrolls sideways: ${doc.scrollWidth} > ${window.innerWidth}`,
      );
    }
    for (const table of Array.from(document.querySelectorAll("main table"))) {
      const box = table.parentElement?.getBoundingClientRect();
      const r = table.getBoundingClientRect();
      if (box && r.right > box.right + 1) {
        out.push(
          `table overflows its card by ${Math.round(r.right - box.right)} px`,
        );
      }
      for (const cell of Array.from(table.querySelectorAll("td, th"))) {
        const c = cell.getBoundingClientRect();
        for (const child of Array.from(cell.querySelectorAll("*"))) {
          const k = child.getBoundingClientRect();
          if (k.width > 0 && k.right > c.right + 1) {
            out.push(
              `cell content spills: "${(child.textContent ?? "").slice(0, 40)}"`,
            );
            break;
          }
        }
      }
    }
    for (const el of Array.from(
      document.querySelectorAll<HTMLElement>("body *"),
    )) {
      const cs = getComputedStyle(el);
      if (
        cs.textDecorationLine.includes("line-through") &&
        el.textContent?.trim()
      ) {
        out.push(`struck through: "${el.textContent.trim().slice(0, 40)}"`);
      }
      if (
        cs.textOverflow === "ellipsis" &&
        el.scrollWidth > el.clientWidth + 1 &&
        !el.title &&
        !el.closest("[title]")
      ) {
        out.push(
          `truncated without a title: "${(el.textContent ?? "").slice(0, 40)}"`,
        );
      }
    }
    const text = document.body.innerText;
    for (const leak of [
      "demo-tenant",
      "QA-PORTAL-CUST",
      "cust_",
      "acct_",
      "inv_",
      "undefined",
      "NaN",
    ]) {
      if (text.includes(leak)) out.push(`text shows "${leak}"`);
    }
    return out;
  });
}

test.describe("portal fixes: layout, formats and identity", () => {
  onlyWithPortalConfig();

  for (const p of PAGES) {
    for (const vp of SIZES) {
      const name = `${p.id}-${vp.width}x${vp.height}`;
      test(name, async ({ page }) => {
        await openPortal(page, p.path, vp, OWNER);
        await page.locator(p.ready).first().waitFor({ timeout: 30_000 });
        await page.waitForTimeout(300);
        expect(await problems(page)).toEqual([]);
        const found = await seriousAxe(page);
        expect(found, JSON.stringify(found, null, 2)).toEqual([]);
        const dir = process.env.PORTAL_FIXES_SHOTS_DIR;
        if (dir) await page.screenshot({ path: `${dir}/${name}.png` });
      });
    }
  }

  test("top bar: supplier name, whole customer name at 1024 px, the user's initials", async ({
    page,
  }) => {
    await openPortal(
      page,
      "/portal/orders",
      { width: 1024, height: 768 },
      OWNER,
    );
    const bar = page.locator("[data-portal-topbar]");
    await expect(bar.getByText("Demo Fuels", { exact: true })).toBeVisible();
    const name = bar.getByText("QA-OWNER Demo Fuels Customer");
    await expect(name).toHaveAttribute("title", "QA-OWNER Demo Fuels Customer");
    const clipped = await name.evaluate(
      (el) => el.scrollWidth > el.clientWidth,
    );
    expect(clipped).toBe(false);
    await expect(bar.getByRole("img", { name: "qa-owner.person" })).toHaveText(
      "QP",
    );
  });

  test("orders: one date format with the zone, one quantity pattern, legible cancelled", async ({
    page,
  }) => {
    await openPortal(
      page,
      "/portal/orders",
      { width: 1280, height: 800 },
      OWNER,
    );
    const rows = page.locator("main tbody tr");
    await expect(rows).toHaveCount(OWNER_ORDERS.length);
    const delivery = await rows.locator("td:nth-child(3)").allInnerTexts();
    for (const cell of delivery) {
      const timed = /\d:\d\d [AP]M/.test(cell);
      if (timed) expect(cell, cell).toMatch(/C[DS]T/);
      expect(cell).toMatch(
        /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} [A-Z][a-z]{2}/,
      );
    }
    const qty = await rows.locator("td:nth-child(4)").allInnerTexts();
    for (const cell of qty) {
      expect(cell.replace(/\s+/g, " ").trim()).toMatch(
        /^(\d{1,3}(,\d{3})* gal (requested|delivered)|Fill to full)$/,
      );
    }
    expect(qty.join(" ")).toContain("276 gal");
    const cancelled = page.locator('main [data-status="cancelled"]');
    await expect(cancelled.first()).toBeVisible();
    await expect(cancelled.first().locator("svg")).toHaveCount(1);
    const po = page.getByText("QA-OWNER-REQ-1-PO-WITH-A-LONG-REFERENCE");
    await expect(po).toHaveAttribute(
      "title",
      "QA-OWNER-REQ-1-PO-WITH-A-LONG-REFERENCE",
    );
  });

  test("request dialog names the tenant's zone for the times", async ({
    page,
  }) => {
    await openPortal(
      page,
      "/portal/orders/new?tank=QA-TANK-WARN",
      { width: 1280, height: 800 },
      OWNER,
    );
    await expect(page.getByRole("dialog")).toContainText(
      "Times are Central Daylight Time.",
    );
    await page.getByLabel("Gallons", { exact: true }).check();
    await expect(page.getByRole("dialog")).toContainText(
      "Between 25 and 1,360 gal, the room left at the latest reading.",
    );
  });
});
