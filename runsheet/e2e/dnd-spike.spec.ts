/**
 * Dispatch Board DnD spike (plan task 1, throwaway; deleted after).
 * (a) tray → lane drop resolves the insertion index from the pointer;
 * (b) nested vertical + horizontal auto-scroll during a drag;
 * (c) the same drag in WebKit with an iPad (touch) device profile.
 */
import { expect, type Page, test } from "@playwright/test";

async function centre(page: Page, testId: string) {
  const box = await page.getByTestId(testId).boundingBox();
  if (!box) throw new Error(`no box for ${testId}`);
  return { x: box.x + box.width / 2, y: box.y + box.height / 2, box };
}

async function startDrag(page: Page, gripTestId: string) {
  const grip = await centre(page, gripTestId);
  await page.mouse.move(grip.x, grip.y);
  await page.mouse.down();
  await page.mouse.move(grip.x + 8, grip.y + 8, { steps: 4 });
}

test.beforeEach(async ({ page }) => {
  await page.goto("/dnd-spike");
  await expect(page.getByTestId("lane-T0")).toBeVisible();
  // Give the client bundle time to hydrate and bind the drag handlers.
  await expect(page.getByTestId("grip-o1")).toBeVisible();
  await page.waitForFunction(() =>
    document
      .querySelector('[data-testid="card-o1"]')
      ?.getAttribute("draggable"),
  );
});

test("(a) tray to lane drop inserts at the pointer gap", async ({ page }) => {
  await startDrag(page, "grip-o1");
  // Between the 2nd and 3rd stop of lane T1: just right of stop 1's midpoint.
  const s1 = await centre(page, "stop-T1-s1");
  const target = { x: s1.box.x + s1.box.width - 2, y: s1.y };
  await page.mouse.move(target.x, target.y, { steps: 12 });
  await expect(page.getByTestId("lane-T1")).toHaveAttribute("data-hover", "2");
  await page.mouse.up();

  await expect(page.getByTestId("last-drop")).toContainText('"index":2');
  const order = await page.getByTestId("lane-T1").getAttribute("data-order");
  expect(order?.split(",").slice(0, 4)).toEqual([
    "T1-s0",
    "T1-s1",
    "o1",
    "T1-s2",
  ]);
  await expect(page.getByTestId("card-o1")).toHaveCount(0);
});

test("(b) nested vertical and horizontal auto-scroll", async ({ page }) => {
  const grid = page.getByTestId("grid");
  const gridBox = await grid.boundingBox();
  if (!gridBox) throw new Error("no grid box");
  await startDrag(page, "grip-o2");

  // Hover near the bottom edge of the vertical scroller.
  const nearBottom = {
    x: gridBox.x + 300,
    y: gridBox.y + gridBox.height - 6,
  };
  await page.mouse.move(nearBottom.x, nearBottom.y, { steps: 10 });
  for (let i = 0; i < 30; i += 1) {
    await page.mouse.move(nearBottom.x + (i % 2), nearBottom.y);
    await page.waitForTimeout(40);
  }
  const scrollTop = await grid.evaluate((el) => el.scrollTop);
  expect(scrollTop).toBeGreaterThan(0);

  // Then near the right edge of a visible lane's horizontal scroller.
  const laneId = await page.evaluate(() => {
    const g = document.querySelector('[data-testid="grid"]') as HTMLElement;
    const gr = g.getBoundingClientRect();
    const lanes = Array.from(
      g.querySelectorAll<HTMLElement>('[data-testid^="scroller-"]'),
    );
    const visible = lanes.find((l) => {
      const r = l.getBoundingClientRect();
      return r.top > gr.top + 10 && r.bottom < gr.bottom - 80;
    });
    return visible?.dataset.testid ?? "";
  });
  const scroller = page.getByTestId(laneId);
  const sBox = await scroller.boundingBox();
  if (!sBox) throw new Error("no scroller box");
  const nearRight = { x: sBox.x + sBox.width - 6, y: sBox.y + sBox.height / 2 };
  await page.mouse.move(nearRight.x, nearRight.y, { steps: 10 });
  for (let i = 0; i < 30; i += 1) {
    await page.mouse.move(nearRight.x, nearRight.y + (i % 2));
    await page.waitForTimeout(40);
  }
  const scrollLeft = await scroller.evaluate((el) => el.scrollLeft);
  expect(scrollLeft).toBeGreaterThan(0);
  await page.mouse.up();
});

test("(c) touch device profile still drags (automated part)", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "ipad-webkit",
    "iPad profile only; desktop projects cover (a) and (b)",
  );
  const grip = page.getByTestId("grip-o3");
  // The grip opts out of browser panning so a long press is not a scroll.
  await expect(grip).toHaveCSS("touch-action", "none");
  await startDrag(page, "grip-o3");
  const s0 = await centre(page, "stop-T0-s0");
  await page.mouse.move(s0.box.x + 4, s0.y, { steps: 12 });
  await page.mouse.up();
  await expect(page.getByTestId("last-drop")).toContainText('"index":0');
});
