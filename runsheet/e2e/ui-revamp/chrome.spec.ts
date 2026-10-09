/**
 * Chrome budget (R4.7, D8): the first data row starts ≤ 172 px from the
 * viewport top at 1280×800 and 1440×900. Phase 1 delivers the 48 px top bar
 * and the 44 px title row; every page in `pages.ts` now meets the budget
 * (no `test.fail()` marks remain after task 3.10).
 */
import { expect, test } from "@playwright/test";
import { FIRST_ROW_BUDGET, firstRowTop } from "./measure";
import { SHELL_PAGES, VIEWPORTS } from "./pages";
import { installShellFake, signIn } from "./shellFake";

test.describe("shell chrome", () => {
  for (const vp of VIEWPORTS) {
    test(`top bar is 48 px and the title row 44 px at ${vp.width}×${vp.height}`, async ({
      page,
      context,
    }) => {
      await signIn(context);
      await installShellFake(page);
      await page.setViewportSize(vp);
      await page.goto("/dashboard/billing?tab=invoices");
      const bar = page.locator('[data-chrome="topbar"]');
      const title = page.locator('[data-chrome="titlerow"]').first();
      await expect(title).toBeVisible();
      expect((await bar.boundingBox())?.height).toBe(48);
      expect((await title.boundingBox())?.height).toBe(44);
      expect((await title.boundingBox())?.y).toBe(48);
      await expect(page.locator("h1")).toHaveCount(1);
    });
  }
});

for (const p of SHELL_PAGES) {
  for (const vp of VIEWPORTS) {
    test(`${p.id}: first row ≤ ${FIRST_ROW_BUDGET} px at ${vp.width}×${vp.height}`, async ({
      page,
      context,
    }) => {
      await signIn(context);
      await installShellFake(page);
      await page.setViewportSize(vp);
      await page.goto(p.path);
      await expect(page.locator("h1")).toHaveText(p.h1);
      if (p.listPage === false) {
        // Not a list page (design.md §6): the content starts right under
        // the 44 px title row (no toolbar or KPI band above it).
        const panel = page.locator('[role="tabpanel"]').first();
        await expect(panel).toBeVisible();
        const top = Math.round((await panel.boundingBox())?.y ?? 999);
        test
          .info()
          .annotations.push({ type: "contentTop", description: String(top) });
        expect(top).toBeLessThanOrEqual(92);
        return;
      }
      if (p.ready)
        await page.locator(p.ready).first().waitFor({ timeout: 10_000 });
      const top = await firstRowTop(page);
      test
        .info()
        .annotations.push({ type: "firstRowTop", description: String(top) });
      expect(top).not.toBeNull();
      expect(top as number).toBeLessThanOrEqual(FIRST_ROW_BUDGET);
    });
  }
}

// R4.7: the Dispatch Board's first lane also fits at 1024×768 (the narrowest
// panels layout; below 1024 px the board stacks).
test(`dispatch-board: first lane ≤ ${FIRST_ROW_BUDGET} px at 1024×768`, async ({
  page,
  context,
}) => {
  await signIn(context);
  await installShellFake(page);
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto("/dashboard/dispatch?tab=board");
  await expect(page.locator("h1")).toHaveText("Dispatch");
  await page.locator('[role="grid"] [role="row"]').first().waitFor();
  await expect(page.locator('[data-layout="panels"]')).toHaveCount(1);
  const top = await firstRowTop(page);
  test
    .info()
    .annotations.push({ type: "firstRowTop", description: String(top) });
  expect(top).not.toBeNull();
  expect(top as number).toBeLessThanOrEqual(FIRST_ROW_BUDGET);
});
