/**
 * Keyboard model and shortcuts (plan task 31; R18.4–R18.6, R11.4, SC 2.1.4,
 * SC 2.4.11): roving tabindex in trays and the grid, the shortcut map,
 * input suppression, the single-key toggle, the help dialog, focus return.
 */
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

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
jest.mock("../../../services/dispatchBoardApi", () => ({
  ...jest.requireActual("../../../services/dispatchBoardApi"),
  getBoard: jest.fn(),
  sendBoardCommand: jest.fn(),
  validateBoard: jest.fn(),
}));
jest.mock("../../../utils/auth", () => ({
  ...jest.requireActual("../../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import { BoardApiError } from "../../../services/dispatchBoardApi";
import { SINGLE_KEY_STORAGE_KEY } from "../dialogs/ShortcutHelpDialog";
import { makeLane, makeLoad, makeTrayOrder } from "../state/testFixtures";
import {
  boardSnap,
  mockSend,
  okResponse,
  renderBoard,
  resetBoardMocks,
  sentBodies,
  TODAY,
} from "../testBoard";
import { navigation, resetMocks } from "../testMocks";
import { addDays } from "../viewState";

function snapshot() {
  return boardSnap({
    lanes: [
      makeLane("T1", 3, { loads: [makeLoad("L1", ["A", "B", "C"])] }),
      makeLane("T2", 5, { loads: [makeLoad("L2", ["D"])] }),
    ],
    trays: {
      orders: [makeTrayOrder("O1"), makeTrayOrder("O2"), makeTrayOrder("O3")],
      orders_truncated: false,
      drivers: [],
      trucks: [],
    },
  });
}

// Key sequences are followed by the command's own promise chain, awaited
// with waitFor as in a browser (RTL still wraps each event in act()).
beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;
});
afterAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

beforeEach(async () => {
  resetMocks();
  resetBoardMocks();
  await renderBoard(snapshot());
});

const q = (key: string) =>
  document.querySelector(`[data-focus-key="${key}"]`) as HTMLElement;
const active = () => document.activeElement?.getAttribute("data-focus-key");

describe("roving tabindex (R18.4)", () => {
  it("a tray is one tab stop; arrows move between its cards", () => {
    const list = screen.getByRole("listbox", { name: "Orders to plan" });
    const tabbable = () =>
      within(list)
        .getAllByRole("option")
        .filter((o) => o.tabIndex === 0)
        .map((o) => o.getAttribute("data-focus-key"));
    expect(tabbable()).toEqual(["order:O1"]);
    act(() => q("order:O1").focus());
    fireEvent.keyDown(q("order:O1"), { key: "ArrowDown" });
    expect(active()).toBe("order:O2");
    expect(tabbable()).toEqual(["order:O2"]);
    fireEvent.keyDown(q("order:O2"), { key: "End" });
    expect(active()).toBe("order:O3");
    fireEvent.keyDown(q("order:O3"), { key: "Home" });
    expect(active()).toBe("order:O1");
  });

  it("the grid is one tab stop; Left/Right move along a lane, Up/Down between lanes", () => {
    const grid = screen.getByRole("grid");
    expect(grid.querySelectorAll('[data-roving][tabindex="0"]')).toHaveLength(
      1,
    );
    act(() => q("lane:T1").focus());
    const key = (k: string) =>
      fireEvent.keyDown(document.activeElement as Element, { key: k });
    key("ArrowRight");
    expect(active()).toBe("driver-slot:T1");
    key("ArrowRight");
    expect(active()).toBe("load:L1");
    key("ArrowRight");
    expect(active()).toBe("stop:A");
    key("ArrowDown");
    expect(active()).toBe("stop:D");
    key("ArrowUp");
    expect(active()).toBe("stop:A");
    key("End");
    expect(active()).toBe("stop:C");
    key("Home");
    expect(active()).toBe("lane:T1");
    expect(grid.querySelectorAll('[data-roving][tabindex="0"]')).toHaveLength(
      1,
    );
  });

  it("cells keep clear of the sticky axis and header when focused (SC 2.4.11)", () => {
    const style = q("stop:A").style;
    expect(Number.parseInt(style.scrollMarginTop, 10)).toBeGreaterThan(0);
    expect(Number.parseInt(style.scrollMarginLeft, 10)).toBeGreaterThan(0);
  });
});

describe("shortcuts (R18.5)", () => {
  it("/ focuses search, ? opens help, T toggles zoom, ] changes day", () => {
    act(() => q("order:O1").focus());
    fireEvent.keyDown(q("order:O1"), { key: "/" });
    expect(document.activeElement).toBe(
      screen.getByRole("searchbox", { name: /Search orders/ }),
    );
    act(() => q("order:O1").focus());
    fireEvent.keyDown(q("order:O1"), { key: "t" });
    expect(screen.getByRole("button", { name: "Sequence" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.keyDown(q("order:O1"), { key: "?" });
    const dialog = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    fireEvent.keyDown(q("order:O1"), { key: "]" });
    expect(navigation.replace).toHaveBeenLastCalledWith(
      expect.stringContaining(`date=${addDays(TODAY, 1)}`),
      { scroll: false },
    );
  });

  it("single-key shortcuts are off while typing in an input", () => {
    const search = screen.getByRole("searchbox", { name: /Search orders/ });
    fireEvent.keyDown(search, { key: "t" });
    expect(screen.getByRole("button", { name: "Timeline" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("the help dialog turns single-key shortcuts off and returns focus (SC 2.1.4)", () => {
    const help = screen.getByRole("button", { name: "Keyboard shortcuts" });
    act(() => help.focus());
    fireEvent.click(help);
    const dialog = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(dialog).getByText("Assign selected")).toBeInTheDocument();
    fireEvent.click(
      within(dialog).getByRole("checkbox", { name: "Single-key shortcuts" }),
    );
    expect(window.localStorage.getItem(SINGLE_KEY_STORAGE_KEY)).toBe("off");
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(document.activeElement).toBe(help);
    act(() => q("order:O1").focus());
    fireEvent.keyDown(q("order:O1"), { key: "t" });
    expect(screen.getByRole("button", { name: "Timeline" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("Cmd/Ctrl+Z undoes and Shift+Cmd/Ctrl+Z redoes (R11.4)", async () => {
    mockSend.mockImplementation(async () => okResponse(snapshot().lanes));
    act(() => q("stop:B").focus());
    fireEvent.keyDown(q("stop:B"), { key: "Enter" });
    fireEvent.keyDown(q("stop:B"), { key: "u" });
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Undo" })).toBeEnabled(),
    );
    const firstId = mockSend.mock.calls[0][1].client_command_id;
    fireEvent.keyDown(document.activeElement as Element, {
      key: "z",
      metaKey: true,
    });
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(2));
    expect(sentBodies()[1]).toMatchObject({
      type: "revert",
      target_command_id: firstId,
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Redo" })).toBeEnabled(),
    );
    fireEvent.keyDown(document.activeElement as Element, {
      key: "z",
      ctrlKey: true,
      shiftKey: true,
    });
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(3));
    expect(sentBodies()[2]).toMatchObject({
      type: "reapply",
      target_command_id: firstId,
    });
  });
});

describe("focus return (R18.6)", () => {
  it("moves to the card in its new place after a commit", async () => {
    mockSend.mockResolvedValue(
      okResponse([
        makeLane("T1", 4, { loads: [makeLoad("L1", ["B", "A", "C"])] }),
      ]),
    );
    act(() => q("stop:B").focus());
    fireEvent.keyDown(q("stop:B"), { key: "F10", shiftKey: true });
    fireEvent.click(screen.getByRole("menuitem", { name: "Move earlier" }));
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(active()).toBe("stop:B"));
    // B is now first in its lane row.
    const cells = Array.from(
      document.querySelectorAll('[data-lane-row="T1"] [data-stop-card]'),
    ).map((c) => c.getAttribute("data-focus-key"));
    expect(cells).toEqual(["stop:B", "stop:A", "stop:C"]);
  });

  it("goes back to where it started when refused", async () => {
    mockSend.mockRejectedValue(
      new BoardApiError("Blocked", 422, "BOARD_COMMAND_BLOCKED", {
        checks: {},
      }),
    );
    act(() => q("order:O2").focus());
    fireEvent.click(q("order:O2"));
    fireEvent.click(
      screen.getByRole("button", { name: "Place Order O2 on Truck T2" }),
    );
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(active()).toBe("order:O2"));
  });
});
