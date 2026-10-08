/**
 * Stacked layout below 1024 px (plan task 38; R20.2, R20.3, R4.3, N5;
 * design K14.8): Sequence forced, trays in a bottom sheet that closes when a
 * card is picked, a full-screen modal drawer with the board inert behind it,
 * and the Place mode banner pinned to the bottom. Target sizes are audited
 * in a real browser by `e2e/dispatch-board.spec.ts`; jsdom has no layout, so
 * this suite checks the size classes on the drag handles instead.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

jest.mock("next/navigation", () => require("../testMocks").navigationMock);
jest.mock(
  "../../../hooks/useDispatchBoardSocket",
  () => require("../testMocks").socketMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop/adapter/element-adapter",
  () => require("../testMocks").pragmaticMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop-auto-scroll/element",
  () => require("../testMocks").autoScrollMock,
);
jest.mock("../../../hooks/useFleetWebSocket", () => ({
  useFleetWebSocket: jest.fn(),
}));
jest.mock("../../../services/dispatchBoardApi", () => ({
  ...jest.requireActual("../../../services/dispatchBoardApi"),
  getBoard: jest.fn(),
  sendBoardCommand: jest.fn(),
  validateBoard: jest.fn(),
  getBoardHistory: jest
    .fn()
    .mockResolvedValue({ items: [], next_cursor: null }),
}));
jest.mock("../../../utils/auth", () => ({
  ...jest.requireActual("../../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import { render } from "@testing-library/react";
import DispatchBoard from "../DispatchBoard";
import {
  makeDriver,
  makeLane,
  makeLoad,
  makeTrayOrder,
} from "../state/testFixtures";
import {
  boardSnap,
  mockGetBoard,
  renderBoard,
  resetBoardMocks,
} from "../testBoard";
import { resetMocks } from "../testMocks";
import { STACKED_QUERY } from "./useStackedLayout";

const realMatchMedia = window.matchMedia;

/** Viewport `width` px; `coarse` = touch pointer. */
function setViewport(width: number, coarse = false) {
  window.matchMedia = ((query: string) => ({
    matches:
      (query === STACKED_QUERY && width < 1024) ||
      (query.includes("pointer: coarse") && coarse),
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

function snapshot() {
  return boardSnap({
    lanes: [
      makeLane("T1", 1, { loads: [makeLoad("L1", ["A", "B"])] }),
      makeLane("T2", 1),
    ],
    trays: {
      orders: [makeTrayOrder("O1"), makeTrayOrder("O2")],
      orders_truncated: false,
      drivers: [makeDriver("D1", { name: "Ana" })],
      trucks: [],
    },
  });
}

async function renderStacked() {
  mockGetBoard.mockResolvedValue(snapshot());
  const utils = render(<DispatchBoard mode="active_gated" />);
  await screen.findByRole("button", {
    name: "Orders, drivers and trucks (2 orders)",
  });
  return utils;
}

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
});
afterEach(() => {
  window.matchMedia = realMatchMedia;
});

describe("stacked layout below 1024 px (R20.2)", () => {
  it("keeps the three-panel layout at 1024 px and wider", async () => {
    setViewport(1024);
    const { container } = await renderBoard(snapshot());
    expect(container.querySelector("[data-layout]")).toHaveAttribute(
      "data-layout",
      "panels",
    );
    expect(
      screen.getByRole("button", { name: "Timeline", pressed: true }),
    ).toBeEnabled();
  });

  it("forces Sequence without changing the saved zoom", async () => {
    setViewport(800);
    window.localStorage.setItem(
      "runsheet.dispatchBoard.view.v1",
      JSON.stringify({ zoom: "timeline" }),
    );
    const { container } = await renderStacked();
    const root = container.querySelector("[data-layout]");
    expect(root).toHaveAttribute("data-layout", "stacked");
    expect(root).toHaveAttribute("data-zoom", "sequence");
    const timeline = screen.getByRole("button", { name: "Timeline" });
    expect(timeline).toBeDisabled();
    expect(timeline).toHaveAccessibleDescription(
      "Timeline needs a screen at least 1,024 pixels wide.",
    );
    // Shown as text too, not only to screen readers (review P7-5).
    expect(
      screen.getByText("Timeline needs a screen at least 1,024 pixels wide."),
    ).not.toHaveClass("sr-only");
    expect(
      screen.getByRole("button", { name: "Sequence", pressed: true }),
    ).toBeInTheDocument();
    expect(
      JSON.parse(
        window.localStorage.getItem("runsheet.dispatchBoard.view.v1") ?? "{}",
      ).zoom,
    ).toBe("timeline");
    // Lanes stay a vertical list (one grid, one row per truck).
    expect(screen.getAllByRole("row").length).toBeGreaterThanOrEqual(2);
  });

  it("shows the trays in a bottom sheet that closes when a card is picked", async () => {
    setViewport(800);
    await renderStacked();
    expect(screen.queryByRole("region", { name: "Trays" })).toBeNull();
    const opener = screen.getByRole("button", {
      name: "Orders, drivers and trucks (2 orders)",
    });
    fireEvent.click(opener);
    const sheet = screen.getByRole("dialog", {
      name: "Orders, drivers and trucks",
    });
    expect(sheet).toHaveAttribute("aria-modal", "true");
    expect(
      within(sheet).getByRole("region", { name: "Trays" }),
    ).toBeInTheDocument();
    // Tap a card: Place mode starts, the sheet closes, the banner is pinned.
    const card = sheet.querySelector(
      '[data-focus-key="order:O1"]',
    ) as HTMLElement;
    fireEvent.click(card);
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Orders, drivers and trucks" }),
      ).toBeNull(),
    );
    const banner = screen.getByTestId("place-mode-banner");
    expect(banner).toHaveTextContent(
      "Placing Order O1. Choose a truck or press Escape.",
    );
    expect(banner.className).toContain("fixed");
    expect(banner.className).toContain("bottom-0");
  });

  it("closes the sheet with Escape and returns focus to the opener", async () => {
    setViewport(800);
    await renderStacked();
    const opener = screen.getByRole("button", {
      name: "Orders, drivers and trucks (2 orders)",
    });
    opener.focus();
    fireEvent.click(opener);
    expect(
      screen.getByRole("dialog", { name: "Orders, drivers and trucks" }),
    ).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(
      screen.queryByRole("dialog", { name: "Orders, drivers and trucks" }),
    ).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("opens the drawer full screen and makes the board behind it inert", async () => {
    setViewport(800);
    const { container } = await renderStacked();
    fireEvent.click(screen.getByRole("button", { name: "Truck T1 actions" }));
    fireEvent.click(
      within(screen.getByRole("menu")).getByRole("menuitem", {
        name: "Details",
      }),
    );
    const drawer = screen.getByRole("dialog", { name: "Truck T1 details" });
    expect(drawer).toHaveAttribute("aria-modal", "true");
    expect(drawer.className).toContain("fixed");
    expect(drawer.className).toContain("inset-0");
    const grid = container.querySelector('[role="grid"]') as HTMLElement;
    expect(grid.closest("[inert]")).not.toBeNull();
    expect(
      screen.getByRole("group", { name: "Board" }).closest("[inert]"),
    ).not.toBeNull();
    // The live regions stay outside the inert part.
    for (const region of container.querySelectorAll("[aria-live]")) {
      expect(region.closest("[inert]")).toBeNull();
    }
    fireEvent.click(
      within(drawer).getByRole("button", { name: "Close details" }),
    );
    expect(container.querySelector("[inert]")).toBeNull();
  });

  it("keeps the drawer a side panel at 1024 px", async () => {
    setViewport(1280);
    const { container } = await renderBoard(snapshot());
    fireEvent.click(screen.getByRole("button", { name: "Truck T1 actions" }));
    fireEvent.click(
      within(screen.getByRole("menu")).getByRole("menuitem", {
        name: "Details",
      }),
    );
    expect(
      screen.getByRole("complementary", { name: "Truck T1 details" }),
    ).toBeInTheDocument();
    expect(container.querySelector("[inert]")).toBeNull();
  });
});

describe("drag handle sizes (R20.3, N5)", () => {
  it("every grip is at least 24 px and 44 px on a coarse pointer", async () => {
    setViewport(1280);
    const { container } = await renderBoard(snapshot());
    // Card and stop grips; the load's terminal chip is its own handle and is
    // CHIP_WIDTH (72 px) by the lane height (72/104 px), measured in e2e.
    const grips = container.querySelectorAll(".cursor-grab.touch-none");
    expect(grips.length).toBeGreaterThan(0);
    for (const grip of grips) {
      const cls = grip.className;
      expect(cls).toMatch(/\bmin-h-6\b/);
      expect(cls).toMatch(/\bmin-w-6\b/);
      expect(cls).toContain("pointer-coarse:min-h-11");
      expect(cls).toContain("pointer-coarse:min-w-11");
    }
  });
});
