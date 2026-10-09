/**
 * Dispatch Board end-to-end (plan tasks 37–40): real pointer drags, the
 * second-dispatcher conflict, the publish flow, the iPad profile and the
 * stacked layout, the 24 px target audit, drag frame time at the N1 size,
 * and accessibility snapshots plus axe-core scans.
 *
 * Runs only with `playwright.dispatch-board.config.ts` (harness route on its
 * own dev server). Every API call and socket is answered by
 * `dispatchBoardFake.ts`; nothing reaches a backend or staging.
 *
 * Limits: Playwright can't synthesize iOS's native long-press drag, so the
 * iPad tests cover the touch profile, the stacked layout, tap-to-place and
 * a pointer-driven drag; the long press itself is an owner device check
 * (evidence.md, Phase 7).
 */
import AxeBuilder from "@axe-core/playwright";
import {
  type BrowserContext,
  expect,
  type Locator,
  type Page,
  test,
} from "@playwright/test";
import { type Actor, FakeBoard, type FixtureSize } from "./dispatchBoardFake";

const ANA: Actor = { userId: "qa-user-ana", name: "ana" };
const BEN: Actor = { userId: "qa-user-ben", name: "ben" };
const SMALL: FixtureSize = {
  lanes: 4,
  stopsPerLane: 3,
  trayOrders: 4,
  drivers: 3,
};
/**
 * N1: 60 lanes and 1,500 stops a day, plus 120 orders in the tray. N1's
 * "600 orders" can't hold at 1,500 stops (one stop per order), so the
 * fixture uses the larger figure, as the backend budgets do.
 */
const N1: FixtureSize = {
  lanes: 60,
  stopsPerLane: 25,
  trayOrders: 120,
  drivers: 60,
};

const desktop = (name: string) => name === "chromium" || name === "webkit";

async function boot(
  page: Page,
  board: FakeBoard,
  actor: Actor = ANA,
  query = "",
) {
  await board.install(page, actor);
  await page.goto(`/e2e/dispatch-board${query}`);
  await expect(page.getByRole("grid")).toBeVisible({ timeout: 60_000 });
  // Hydrated and the drag adapter bound.
  await page.waitForFunction(() =>
    Boolean(document.querySelector('[draggable="true"]')),
  );
}

const key = (page: Page, k: string) => page.locator(`[data-focus-key="${k}"]`);
const grip = (page: Page, k: string) => key(page, k).locator(".cursor-grab");
const laneRow = (page: Page, t: string) =>
  page.locator(`[data-lane-row="${t}"]`);

async function stopOrder(page: Page, t: string): Promise<string[]> {
  return laneRow(page, t)
    .locator('[data-focus-key^="stop:"]')
    .evaluateAll((els) =>
      els.map((e) => (e.getAttribute("data-focus-key") ?? "").slice(5)),
    );
}

async function centre(l: Locator) {
  const b = await l.boundingBox();
  if (!b) throw new Error("element has no box");
  return { x: b.x + b.width / 2, y: b.y + b.height / 2, b };
}

/** A native drag with the mouse: press on `from`, move to `to()`, release. */
async function pointerDrag(
  page: Page,
  from: Locator,
  to: () => Promise<{ x: number; y: number }>,
) {
  const s = await centre(from);
  await page.mouse.move(s.x, s.y);
  await page.mouse.down();
  await page.mouse.move(s.x + 8, s.y + 8, { steps: 4 });
  const t = await to();
  await page.mouse.move(t.x, t.y, { steps: 12 });
  await page.mouse.up();
}

function lastCommand(board: FakeBoard) {
  return board.commands[board.commands.length - 1]?.body;
}

async function waitForCommands(board: FakeBoard, n: number) {
  await expect.poll(() => board.commands.length).toBeGreaterThanOrEqual(n);
}

// ─── Task 37: drags, conflict, publish ──────────────────────────────────────

test.describe("pointer drags (R5.1, R6.1, R7.1, R7.2)", () => {
  test.beforeEach(({ browserName: _browser }, info) => {
    test.skip(!desktop(info.project.name), "desktop projects");
  });

  test("tray → lane drops at the pointer's gap", async ({ page }) => {
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    const target = key(page, "stop:1001");
    await pointerDrag(page, grip(page, "order:5000"), async () => {
      const t = await centre(target);
      return { x: t.b.x + t.b.width - 4, y: t.y };
    });
    await waitForCommands(board, 1);
    expect(lastCommand(board)).toMatchObject({
      type: "assign_orders",
      order_ids: ["5000"],
      truck_id: "T01",
      target: { load_id: "L-T01", index: 2 },
      input_modality: "drag",
    });
    await expect
      .poll(() => stopOrder(page, "T01"))
      .toEqual(["1000", "1001", "5000", "1002"]);
    await expect(key(page, "order:5000")).toHaveCount(0);
  });

  test("reorder within a load", async ({ page }) => {
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    const target = key(page, "stop:1002");
    await pointerDrag(page, grip(page, "stop:1000"), async () => {
      const t = await centre(target);
      return { x: t.b.x + t.b.width - 4, y: t.y };
    });
    await waitForCommands(board, 1);
    expect(lastCommand(board)).toMatchObject({
      type: "move_stops",
      order_ids: ["1000"],
      truck_id: "T01",
      input_modality: "drag",
    });
    await expect
      .poll(() => stopOrder(page, "T01"))
      .toEqual(["1001", "1002", "1000"]);
  });

  test("cross-lane move to another truck (best fit)", async ({ page }) => {
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    await pointerDrag(page, grip(page, "stop:1001"), () =>
      centre(key(page, "lane:T02")),
    );
    await waitForCommands(board, 1);
    expect(lastCommand(board)).toMatchObject({
      type: "move_stops",
      order_ids: ["1001"],
      truck_id: "T02",
      input_modality: "drag",
    });
    await expect.poll(() => stopOrder(page, "T01")).toEqual(["1000", "1002"]);
    await expect.poll(() => stopOrder(page, "T02")).toContain("1001");
  });

  test("driver drop on a driver slot pairs the driver", async ({ page }) => {
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    await page.getByRole("tab", { name: /^Drivers/ }).click();
    await pointerDrag(page, grip(page, "driver:D1"), () =>
      centre(key(page, "driver-slot:T03")),
    );
    await waitForCommands(board, 1);
    expect(lastCommand(board)).toMatchObject({
      type: "pair_driver",
      truck_id: "T03",
      driver_id: "D1",
      input_modality: "drag",
    });
    await expect(key(page, "driver-slot:T03")).toHaveAccessibleName(
      "Driver QA- Driver 1",
    );
  });

  test("load move to another truck", async ({ page }) => {
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    await pointerDrag(page, key(page, "load:L-T01"), () =>
      centre(key(page, "load:L-T03")),
    );
    await waitForCommands(board, 1);
    expect(lastCommand(board)).toMatchObject({
      type: "move_load",
      load_id: "L-T01",
      truck_id: "T03",
      input_modality: "drag",
    });
    await expect.poll(() => stopOrder(page, "T01")).toEqual([]);
    await expect.poll(() => stopOrder(page, "T03")).toContain("1000");
  });

  test("stop dropped on the order tray is unassigned (R7.1)", async ({
    page,
  }) => {
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    const tray = page.getByRole("region", { name: "Order tray" });
    await pointerDrag(page, grip(page, "stop:1001"), async () => {
      const t = await centre(tray);
      return { x: t.x, y: t.b.y + t.b.height - 20 };
    });
    await waitForCommands(board, 1);
    expect(lastCommand(board)).toMatchObject({
      type: "unassign_orders",
      order_ids: ["1001"],
      input_modality: "drag",
    });
    await expect.poll(() => stopOrder(page, "T01")).toEqual(["1000", "1002"]);
    // The tray reloads after a tray-returning command (R3.1).
    await expect(key(page, "order:1001")).toBeVisible();
  });

  test("auto-scrolls the lane list near its edge during a drag", async ({
    page,
  }) => {
    const board = new FakeBoard({ ...SMALL, lanes: 30 });
    await boot(page, board);
    const scroller = page.getByRole("region", { name: "Lanes" });
    const box = await scroller.boundingBox();
    if (!box) throw new Error("no grid box");
    const g = await centre(grip(page, "order:5000"));
    await page.mouse.move(g.x, g.y);
    await page.mouse.down();
    await page.mouse.move(g.x + 8, g.y + 8, { steps: 4 });
    const edge = { x: box.x + box.width / 2, y: box.y + box.height - 6 };
    await page.mouse.move(edge.x, edge.y, { steps: 10 });
    for (let i = 0; i < 30; i++) {
      await page.mouse.move(edge.x + (i % 2), edge.y);
      await page.waitForTimeout(40);
    }
    const top = await scroller.evaluate((el) => el.scrollTop);
    expect(top).toBeGreaterThan(0);
    await page.keyboard.press("Escape");
    await page.mouse.up();
  });
});

test.describe("second dispatcher (R14.2)", () => {
  test("a stale drop shows the other dispatcher's name and is not applied", async ({
    browser,
  }, info) => {
    test.skip(info.project.name !== "chromium", "one browser is enough");
    const board = new FakeBoard(SMALL);
    const ctxA: BrowserContext = await browser.newContext();
    const ctxB: BrowserContext = await browser.newContext();
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();
    await boot(a, board, ANA);
    await boot(b, board, BEN);
    // Ben's commit reaches Ana's socket only once her own command is in
    // flight, so she sends with the old version (the race R14.2 covers).
    board.holdBroadcastsFor(ANA.userId);
    await pointerDrag(b, grip(b, "order:5001"), () =>
      centre(key(b, "lane:T01")),
    );
    await waitForCommands(board, 1);
    await pointerDrag(a, grip(a, "order:5000"), () =>
      centre(key(a, "lane:T01")),
    );
    await waitForCommands(board, 2);
    await expect(
      a
        .getByText("Truck T01 was changed by ben. Your change was not applied.")
        .first(),
    ).toBeVisible();
    // Ana's order is back in her tray; Ben's order shows on her board.
    await expect(key(a, "order:5000")).toHaveCount(1);
    await expect.poll(() => stopOrder(a, "T01")).toContain("5001");
    await expect.poll(() => stopOrder(a, "T01")).not.toContain("5000");
    await ctxA.close();
    await ctxB.close();
  });
});

test.describe("publish (R12.2, R12.8)", () => {
  test("review, publish, progress and the published lane", async ({
    page,
  }, info) => {
    test.skip(!desktop(info.project.name), "desktop projects");
    const board = new FakeBoard(SMALL);
    const lane = board.lanes.get("T01");
    if (lane) {
      lane.driver_id = "D1";
      lane.driver = board.drivers[0];
    }
    await boot(page, board);
    await page
      .getByRole("button", { name: /^Publish (all|\d+) ready$/ })
      .click();
    const review = page.getByRole("dialog", { name: "Review and publish" });
    await expect(review).toBeVisible();
    await expect(review.getByText("QA- Driver 1").first()).toBeVisible();
    await review.getByRole("button", { name: "Publish", exact: true }).click();
    await expect(
      page.getByRole("dialog", { name: "Publish finished" }),
    ).toBeVisible({ timeout: 20_000 });
    expect(board.publishes).toHaveLength(1);
    expect(board.publishes[0].body).toMatchObject({
      dry_run: false,
      lanes: [{ truck_id: "T01" }],
    });
    await expect(page.locator('[aria-live="polite"]').first()).toContainText(
      "Truck T01 published.",
    );
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await expect(laneRow(page, "T01")).toContainText("Published");
  });
});

// ─── Task 37/38: iPad profile and stacked layout ────────────────────────────

test.describe("iPad profile (R20.1, R20.2, R20.3)", () => {
  test.beforeEach(({ browserName: _browser }, info) => {
    test.skip(info.project.name !== "ipad-webkit", "iPad profile only");
  });

  test("stacked layout, coarse pointer grips and tap-to-place", async ({
    page,
  }) => {
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    expect(
      await page.evaluate(() => matchMedia("(pointer: coarse)").matches),
    ).toBe(true);
    await expect(page.locator('[data-layout="stacked"]')).toHaveCount(1);
    await expect(page.locator('[data-zoom="sequence"]')).toHaveCount(1);
    // Grips: no browser panning (a long press is not a scroll) and 44 px.
    const g = key(page, "stop:1000").locator("[data-drag-handle]");
    await expect(g).toHaveCSS("touch-action", "none");
    const box = await g.boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(44);
    expect(box?.height).toBeGreaterThanOrEqual(44);
    // Trays in a bottom sheet; tap a card, the sheet closes, tap a truck.
    await page
      .getByRole("button", { name: /^Orders, drivers and trucks/ })
      .tap();
    const sheet = page.getByRole("dialog", {
      name: "Orders, drivers and trucks",
    });
    await expect(sheet).toBeVisible();
    await key(page, "order:5000").tap();
    await expect(sheet).toHaveCount(0);
    const banner = page.getByTestId("place-mode-banner");
    await expect(banner).toContainText("Placing Order 5000.");
    const vb = page.viewportSize();
    const bb = await banner.boundingBox();
    expect(Math.round((bb?.y ?? 0) + (bb?.height ?? 0))).toBe(vb?.height);
    await page
      .getByRole("button", { name: /Truck T02/ })
      .first()
      .tap();
    await waitForCommands(board, 1);
    expect(lastCommand(board)).toMatchObject({
      type: "assign_orders",
      order_ids: ["5000"],
      truck_id: "T02",
      input_modality: "place",
    });
  });

  test("a drag from a grip works under the touch profile", async ({ page }) => {
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    await pointerDrag(
      page,
      key(page, "stop:1000").locator("[data-drag-handle]"),
      () => centre(key(page, "lane:T02")),
    );
    await waitForCommands(board, 1);
    expect(lastCommand(board)).toMatchObject({
      type: "move_stops",
      order_ids: ["1000"],
      truck_id: "T02",
    });
  });
});

test.describe("stacked layout below 1024 px (R20.2)", () => {
  test("narrow desktop window: stacked, Sequence, sheet and full-screen drawer", async ({
    page,
  }, info) => {
    test.skip(info.project.name !== "chromium", "viewport test, one engine");
    await page.setViewportSize({ width: 900, height: 900 });
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    await expect(page.locator('[data-layout="stacked"]')).toHaveCount(1);
    // Zoom lives in the View menu (UI revamp §7.2): Timeline is disabled.
    await page.getByRole("button", { name: /^View:/ }).click();
    await expect(
      page.getByRole("menuitemradio", { name: /^Timeline/ }),
    ).toHaveAttribute("aria-disabled", "true");
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Truck T01 actions" }).click();
    await page.getByRole("menuitem", { name: "Details" }).click();
    const drawer = page.getByRole("dialog", { name: "Truck T01 details" });
    const box = await drawer.boundingBox();
    expect(box).toMatchObject({ x: 0, y: 0, width: 900, height: 900 });
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
    await page.setViewportSize({ width: 1280, height: 900 });
    await expect(page.locator('[data-layout="panels"]')).toHaveCount(1);
    await expect(page.getByRole("region", { name: "Trays" })).toBeVisible();
  });
});

// ─── Task 38: 24 px target audit (SC 2.5.8, R4.3, N5) ───────────────────────

/**
 * Pointer targets in the board whose visible box is under 24×24 CSS px.
 * The box is clipped by every `overflow: hidden` ancestor, so a button
 * squeezed inside a short row counts at the size the user can hit.
 * sr-only text and elements scrolled out of view are skipped.
 */
async function smallTargets(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const sel =
      'button, a[href], input, select, textarea, [role="tab"], [role="option"], [role="menuitem"], [data-stop-card], [draggable="true"], [data-drag-handle]';
    const out: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>(sel)) {
      if (el.closest(".sr-only")) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue; // not rendered
      let top = r.top;
      let bottom = r.bottom;
      let left = r.left;
      let right = r.right;
      let scrolledOut = false;
      for (let a = el.parentElement; a; a = a.parentElement) {
        const ov = getComputedStyle(a);
        if (ov.overflowX === "visible" && ov.overflowY === "visible") continue;
        const c = a.getBoundingClientRect();
        const scrolls = /auto|scroll/.test(ov.overflowX + ov.overflowY);
        if (
          scrolls &&
          (r.bottom <= c.top ||
            r.top >= c.bottom ||
            r.right <= c.left ||
            r.left >= c.right)
        ) {
          scrolledOut = true;
          break;
        }
        if (scrolls) continue; // partly scrolled is still reachable
        top = Math.max(top, c.top);
        bottom = Math.min(bottom, c.bottom);
        left = Math.max(left, c.left);
        right = Math.min(right, c.right);
      }
      if (scrolledOut) continue;
      const w = Math.max(0, right - left);
      const h = Math.max(0, bottom - top);
      if (w < 24 || h < 24) {
        const name =
          el.getAttribute("aria-label") ||
          el.textContent?.trim().slice(0, 40) ||
          el.tagName;
        out.push(
          `${el.tagName.toLowerCase()} "${name}" ${Math.round(w)}×${Math.round(h)}`,
        );
      }
    }
    return out;
  });
}

test.describe("target size audit (SC 2.5.8)", () => {
  for (const density of ["comfortable", "compact"] as const) {
    test(`no target under 24 px in ${density}`, async ({ page }, info) => {
      test.skip(info.project.name !== "chromium", "layout audit, one engine");
      const board = new FakeBoard(SMALL);
      await boot(page, board, ANA, `?density=${density}`);
      await expect(page.locator(`[data-density="${density}"]`)).toHaveCount(1);
      expect(await smallTargets(page)).toEqual([]);
    });
  }
});

// ─── Task 39: drag frame time at the N1 size (K15) ──────────────────────────

test.describe("performance (N1, K15)", () => {
  test("60 lanes / 1,500 stops: drag frame time p95", async ({
    page,
  }, info) => {
    test.skip(info.project.name !== "chromium", "frame timing on Chromium");
    const board = new FakeBoard(N1);
    await board.install(page, ANA);
    const started = Date.now();
    await page.goto("/e2e/dispatch-board");
    await expect(key(page, "lane:T01")).toBeVisible({ timeout: 60_000 });
    const paintMs = Date.now() - started;
    await page.waitForFunction(() =>
      Boolean(document.querySelector('[draggable="true"]')),
    );
    const rendered = await page.locator("[data-lane-row]").count();
    // Frame deltas from requestAnimationFrame while a drag crosses lanes and
    // auto-scrolls the list (K15: no layout reflow while dragging).
    await page.evaluate(() => {
      const w = window as unknown as { __frames: number[]; __stop: boolean };
      w.__frames = [];
      w.__stop = false;
      let last = performance.now();
      performance.mark("board-drag-start");
      const tick = (t: number) => {
        w.__frames.push(t - last);
        last = t;
        if (!w.__stop) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    const g = await centre(grip(page, "order:5000"));
    const grid = await page
      .getByRole("region", { name: "Lanes" })
      .boundingBox();
    if (!grid) throw new Error("no grid");
    await page.mouse.move(g.x, g.y);
    await page.mouse.down();
    await page.mouse.move(g.x + 8, g.y + 8, { steps: 4 });
    for (let i = 0; i < 40; i++) {
      const y = grid.y + 40 + ((i * 37) % Math.max(1, grid.height - 80));
      await page.mouse.move(grid.x + 320 + ((i * 53) % 400), y, { steps: 3 });
    }
    const split = await page.evaluate(
      () => (window as unknown as { __frames: number[] }).__frames.length,
    );
    const edge = { x: grid.x + 400, y: grid.y + grid.height - 6 };
    for (let i = 0; i < 40; i++) {
      await page.mouse.move(edge.x + (i % 2), edge.y);
      await page.waitForTimeout(16);
    }
    await page.keyboard.press("Escape");
    await page.mouse.up();
    const all = await page.evaluate(() => {
      const w = window as unknown as { __frames: number[]; __stop: boolean };
      w.__stop = true;
      performance.measure("board-drag", "board-drag-start");
      return w.__frames;
    });
    const frames = all.slice(1);
    const pct = (xs: number[], q: number) => {
      const s = [...xs].sort((x, y) => x - y);
      return Number(
        s[Math.min(s.length - 1, Math.floor(q * s.length))].toFixed(1),
      );
    };
    const result = {
      frames: frames.length,
      median_ms: pct(frames, 0.5),
      p95_ms: pct(frames, 0.95),
      max_ms: pct(frames, 1),
      // Hovering across lanes vs auto-scrolling (lanes mounting).
      hover_p95_ms: pct(all.slice(1, split), 0.95),
      scroll_p95_ms: pct(all.slice(split), 0.95),
      rendered_lane_rows: rendered,
      first_lanes_visible_ms: paintMs,
      validate_calls: board.validations,
      bundle: process.env.PW_BOARD_PROD === "1" ? "production" : "dev",
    };
    console.log(`[perf] drag frame time ${JSON.stringify(result)}`);
    await info.attach("drag-frame-time.json", {
      body: JSON.stringify(result, null, 2),
      contentType: "application/json",
    });
    // Windowed: far fewer rows than lanes are in the DOM.
    expect(rendered).toBeLessThan(60);
    // K15: 20 ms p95 on the production bundle (PW_BOARD_PROD=1), enforced
    // locally and advisory on CI hardware. Dev React is several times slower,
    // so a dev-server run only records the number.
    if (process.env.PW_BOARD_PROD === "1" && !process.env.CI)
      expect(result.p95_ms).toBeLessThanOrEqual(20);
  });
});

// ─── Task 40: accessibility snapshots and axe-core ──────────────────────────

async function axe(page: Page, include = "main") {
  const res = await new AxeBuilder({ page })
    .include(include)
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  return res.violations.map(
    (v) =>
      `${v.id} (${v.impact}): ${v.nodes.length} node(s) — ${v.nodes
        .slice(0, 3)
        .map((n) => n.target.join(" "))
        .join(
          " | ",
        )} — ${(v.nodes[0]?.failureSummary ?? "").replace(/\s+/g, " ").slice(0, 300)}`,
  );
}

test.describe("accessibility (N5, R18, R19)", () => {
  test.beforeEach(({ browserName: _browser }, info) => {
    test.skip(info.project.name !== "chromium", "a11y scans on Chromium");
  });

  test("board: aria snapshot of the main landmarks and no axe violations", async ({
    page,
  }) => {
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    await expect(
      page.getByRole("group", { name: "Board" }),
    ).toMatchAriaSnapshot(`
      - group "Board":
        - searchbox "Search orders, customers, trucks and drivers"
        - button "Filters"
        - 'button "View: Timeline · Comfortable"'
        - button "Map" [pressed=false]
        - button "Undo" [disabled]
        - button "Redo" [disabled]
        - button "More board actions"
    `);
    // The day and primary actions are the title row (standalone harness).
    await expect(page.getByRole("group", { name: "Day" })).toMatchAriaSnapshot(`
      - group "Day":
        - button "Previous day"
        - textbox "Service day"
        - button "Next day"
    `);
    await expect(page.getByRole("combobox", { name: "Shift" })).toBeVisible();
    await expect(
      page.getByRole("button", { name: /^Publish (all|\d+) ready$/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Trays" }),
    ).toMatchAriaSnapshot(`
      - region "Trays":
        - tablist "Trays":
          - tab "Orders (4)" [selected]
          - tab "Drivers (3)"
          - tab "Trucks (1)"
        - combobox "Sort"
        - tabpanel "Orders (4)":
          - region "Order tray":
            - listbox "Orders to plan":
              - option "Order 5000, QA- Customer 1, DIESEL_2, 1,200 gal, No window, One off"
    `);
    await expect(laneRow(page, "T01")).toMatchAriaSnapshot(`
      - row:
        - rowheader:
          - button "Truck T01 actions"
        - gridcell:
          - button "No driver"
    `);
    expect(await axe(page)).toEqual([]);
  });

  test("order tray: Sort sits in the tray tab row, no duplicate heading", async ({
    page,
  }) => {
    // Owner item 3.x-owner-4: the "Orders (n)" heading + Sort row (40 px:
    // a 32 px control and 8 px padding) repeated the tab label.
    const board = new FakeBoard(SMALL);
    await page.setViewportSize({ width: 1280, height: 800 });
    await boot(page, board);
    const trays = page.getByRole("region", { name: "Trays" });
    await expect(
      trays.getByRole("heading", { name: /^Orders \(/ }),
    ).toHaveCount(0);
    const tabs = trays.getByRole("tablist", { name: "Trays" });
    const sort = trays.getByRole("combobox", { name: "Sort" });
    const tabBox = await tabs.boundingBox();
    const sortBox = await sort.boundingBox();
    // Same row as the tabs.
    expect(
      Math.abs(
        (sortBox?.y ?? 0) -
          (tabBox?.y ?? 99) -
          ((tabBox?.height ?? 0) - (sortBox?.height ?? 0)) / 2,
      ),
    ).toBeLessThanOrEqual(6);
    const first = trays
      .getByRole("listbox", { name: "Orders to plan" })
      .getByRole("option")
      .first();
    const top = Math.round((await first.boundingBox())?.y ?? 999);
    const tabBottom = Math.round((tabBox?.y ?? 0) + (tabBox?.height ?? 0));
    console.log(
      `order tray first card top 1280x800: ${top} (tab row bottom ${tabBottom})`,
    );
    test
      .info()
      .annotations.push({ type: "trayFirstCardTop", description: String(top) });
    // Only the tab panel's 12 px padding sits between the tabs and the first card.
    expect(top - tabBottom).toBeLessThanOrEqual(16);
  });
  test("Place mode, card menu, publish review and drawer have no axe violations", async ({
    page,
  }) => {
    const board = new FakeBoard(SMALL);
    const lane = board.lanes.get("T01");
    if (lane) {
      lane.driver_id = "D1";
      lane.driver = board.drivers[0];
    }
    await boot(page, board);
    const found: Record<string, string[]> = {};
    await key(page, "order:5000").click();
    await expect(page.getByTestId("place-mode-banner")).toBeVisible();
    found.place = await axe(page);
    await page.keyboard.press("Escape");
    await key(page, "stop:1000").click({ button: "right" });
    await expect(page.getByRole("menu")).toBeVisible();
    await expect(page.getByRole("menu")).toMatchAriaSnapshot(`
      - menu:
        - menuitem /Move earlier/
    `);
    found.menu = await axe(page, "body");
    await page.keyboard.press("Escape");
    await page
      .getByRole("button", { name: /^Publish (all|\d+) ready$/ })
      .click();
    const review = page.getByRole("dialog", { name: "Review and publish" });
    await expect(review).toBeVisible();
    found.publish = await axe(page, "body");
    await review.getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Truck T01 actions" }).click();
    await page.getByRole("menuitem", { name: "Details" }).click();
    const drawer = page.getByRole("complementary", {
      name: "Truck T01 details",
    });
    await expect(drawer).toMatchAriaSnapshot(`
      - complementary "Truck T01 details":
        - heading "Truck T01 details" [level=2]
        - tablist "Details":
          - tab "Checks" [selected]
          - tab "Compartments"
          - tab "Map"
          - tab "History"
    `);
    found.drawer = await axe(page);
    expect(found).toEqual({ place: [], menu: [], publish: [], drawer: [] });
  });

  test("200 % zoom approximation and forced colours", async ({ page }) => {
    // 1280×800 at 200 % zoom is a 640×400 CSS px viewport.
    await page.setViewportSize({ width: 640, height: 400 });
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    await expect(page.locator('[data-layout="stacked"]')).toHaveCount(1);
    // The page itself doesn't scroll sideways; only the lane grid does.
    const overflow = await page.evaluate(
      () => (document.scrollingElement?.scrollWidth ?? 0) - window.innerWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    // Windows High Contrast stand-in: forced colours, then axe again.
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.emulateMedia({ forcedColors: "active" });
    await expect(page.locator('[data-layout="panels"]')).toHaveCount(1);
    expect(await axe(page)).toEqual([]);
  });

  test("stacked layout (900 px) and keyboard-only lane navigation", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 900 });
    const board = new FakeBoard(SMALL);
    await boot(page, board);
    await page
      .getByRole("button", { name: /^Orders, drivers and trucks/ })
      .click();
    const sheet = await axe(page, "body");
    await page.keyboard.press("Escape");
    // Keyboard: focus the first stop, arrow along and across lanes.
    await key(page, "stop:1000").focus();
    await page.keyboard.press("ArrowRight");
    await expect(key(page, "stop:1001")).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(page.locator(":focus")).toHaveAttribute(
      "data-focus-key",
      /^(stop|lane|load):.*T02|stop:100[3-5]|load:L-T02/,
    );
    expect({ sheet, board: await axe(page) }).toEqual({
      sheet: [],
      board: [],
    });
  });
});
