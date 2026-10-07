/**
 * Non-drag paths (plan task 30; R18.1–R18.3, R5.7, R6.1, R7.1–R7.3, SC
 * 2.5.7). For every drag action, the card menu, "Assign to truck…" / "Move
 * to…", Place mode and the keyboard send the same command body as the drag
 * (only `client_command_id` and `input_modality` differ, K14.4).
 */
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("next/navigation", () => require("./testMocks").navigationMock);
jest.mock(
  "../../hooks/useDispatchBoardSocket",
  () => require("./testMocks").socketMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop/adapter/element-adapter",
  () => require("./testMocks").pragmaticMock,
);
jest.mock(
  "@atlaskit/pragmatic-drag-and-drop-auto-scroll/element",
  () => require("./testMocks").autoScrollMock,
);
jest.mock("../../services/dispatchBoardApi", () => ({
  ...jest.requireActual("../../services/dispatchBoardApi"),
  getBoard: jest.fn(),
  sendBoardCommand: jest.fn(),
  validateBoard: jest.fn(),
}));
jest.mock("../../utils/auth", () => ({
  ...jest.requireActual("../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));

import {
  makeCheck,
  makeDriver,
  makeLane,
  makeLoad,
  makeTrayOrder,
} from "./state/testFixtures";
import {
  boardSnap,
  mockSend,
  mockValidate,
  okResponse,
  renderBoard,
  resetBoardMocks,
  sentBodies,
  sentModalities,
} from "./testBoard";
import { dropOn, resetMocks, sourceData, startDrag } from "./testMocks";

function snapshot() {
  return boardSnap({
    lanes: [
      makeLane("T1", 3, { loads: [makeLoad("L1", ["A", "B", "C"])] }),
      makeLane("T2", 5, { loads: [makeLoad("L2", ["D"])] }),
    ],
    trays: {
      orders: [makeTrayOrder("O1"), makeTrayOrder("O2")],
      orders_truncated: false,
      drivers: [makeDriver("D1", { name: "Ana" })],
      trucks: [{ truck_id: "T9", compartment_count: 2, capacity_l: 20000 }],
    },
  });
}

// Each path is several user events followed by the command's own promise
// chain (queue → send → settle), as in a browser. RTL still wraps every
// event in act(); the chain that follows is awaited with waitFor.
beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;
});
afterAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

beforeEach(async () => {
  resetMocks();
  resetBoardMocks();
  // Echo the held lanes, so versions don't move between the paths compared.
  mockSend.mockImplementation(async () => okResponse(snapshot().lanes));
  await renderBoard(snapshot());
});

const q = (key: string) =>
  document.querySelector(`[data-focus-key="${key}"]`) as HTMLElement;

function stubRect(el: Element, left: number, width: number) {
  (el as HTMLElement).getBoundingClientRect = () =>
    ({
      left,
      right: left + width,
      width,
      top: 0,
      bottom: 40,
      height: 40,
      x: left,
      y: 0,
    }) as DOMRect;
}

/** Runs one path and returns the single command body it sent. */
async function bodyOf(path: () => Promise<void> | void) {
  mockSend.mockClear();
  // Not inside one act(): each event must render before the next query.
  await path();
  await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
  // Let the command settle (no "Checking…" left) before the next path.
  await waitFor(() =>
    expect(document.querySelector('[data-outcome="checking"]')).toBeNull(),
  );
  return { body: sentBodies()[0], modality: sentModalities()[0] };
}

function menuItem(name: RegExp | string) {
  return within(screen.getByRole("menu")).getByRole("menuitem", { name });
}

function openMenu(el: HTMLElement) {
  fireEvent.keyDown(el, { key: "F10", shiftKey: true });
}

async function chooseLane(name: RegExp) {
  const dialog = await screen.findByRole("dialog");
  await waitFor(() =>
    expect(within(dialog).queryByText("Checking trucks…")).toBeNull(),
  );
  fireEvent.click(within(dialog).getByRole("button", { name }));
}

const drag = (target: HTMLElement, source: HTMLElement, x = 0) => {
  act(() => {
    dropOn(target, sourceData(source), x);
  });
};

describe("assign an order", () => {
  it("best fit: drag = menu = Place mode = keyboard (R18.1, R18.3)", async () => {
    const dragged = await bodyOf(() => drag(q("lane:T2"), q("order:O1")));
    const menu = await bodyOf(async () => {
      openMenu(q("order:O1"));
      fireEvent.click(menuItem("Assign to truck…"));
      await chooseLane(/^Truck T2/);
    });
    const placed = await bodyOf(() => {
      fireEvent.click(q("order:O1"));
      fireEvent.click(
        screen.getByRole("button", { name: "Place Order O1 on Truck T2" }),
      );
    });
    const keyed = await bodyOf(async () => {
      fireEvent.keyDown(q("order:O1"), { key: "Enter" });
      fireEvent.keyDown(q("order:O1"), { key: "a" });
      await chooseLane(/^Truck T2/);
    });
    expect(dragged.body).toEqual({
      type: "assign_orders",
      order_ids: ["O1"],
      truck_id: "T2",
      target: {},
      expected_lane_versions: { T2: 5 },
    });
    for (const other of [menu, placed, keyed])
      expect(other.body).toEqual(dragged.body);
    expect([dragged, menu, placed, keyed].map((x) => x.modality)).toEqual([
      "drag",
      "menu",
      "place",
      "menu",
    ]);
  });

  it("exact position: drag = Position… step = Place slot (R5.2)", async () => {
    stubRect(q("stop:B"), 200, 100);
    const dragged = await bodyOf(() => drag(q("stop:B"), q("order:O1"), 290));
    const menu = await bodyOf(async () => {
      openMenu(q("order:O1"));
      fireEvent.click(menuItem("Assign to truck…"));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Position on Truck T1" }),
      );
      fireEvent.click(
        within(dialog).getByRole("button", {
          name: /^Load 1, after stop 2 \(Order B\)/,
        }),
      );
    });
    const placed = await bodyOf(() => {
      fireEvent.click(q("order:O1"));
      fireEvent.click(
        screen.getByRole("button", {
          name: "Place at stop 3 of load 1 on Truck T1",
        }),
      );
    });
    expect(dragged.body).toMatchObject({ target: { load_id: "L1", index: 2 } });
    expect(menu.body).toEqual(dragged.body);
    expect(placed.body).toEqual(dragged.body);
  });

  it("several selected orders go as one command from the menu too (R5.7)", async () => {
    const menu = await bodyOf(async () => {
      fireEvent.click(q("order:O1"));
      fireEvent.click(q("order:O2"), { shiftKey: true });
      openMenu(q("order:O2"));
      fireEvent.click(menuItem("Assign 2 orders to truck…"));
      await chooseLane(/^Truck T2/);
    });
    const dragged = await bodyOf(() => {
      fireEvent.click(q("order:O1"));
      fireEvent.keyDown(q("order:O2"), { key: " " });
      drag(q("lane:T2"), q("order:O2"));
    });
    expect(menu.body).toMatchObject({
      order_ids: ["O1", "O2"],
      truck_id: "T2",
    });
    expect(dragged.body).toEqual(menu.body);
  });
});

describe("rearrange stops", () => {
  it.each([
    ["Move earlier", "B", "A", "left"],
    ["Move later", "A", "B", "right"],
    ["Move to first stop", "B", "A", "left"],
    ["Move to last stop", "A", "C", "right"],
  ] as const)(
    "%s = dragging to that slot (R18.1, R7.1)",
    async (label, stop, onto, edge) => {
      stubRect(q(`stop:${onto}`), 100, 100);
      const dragged = await bodyOf(() =>
        drag(q(`stop:${onto}`), q(`stop:${stop}`), edge === "left" ? 105 : 195),
      );
      const menu = await bodyOf(() => {
        openMenu(q(`stop:${stop}`));
        fireEvent.click(menuItem(label));
      });
      expect(dragged.body.type).toBe("move_stops");
      expect(menu.body).toEqual(dragged.body);
      expect(menu.modality).toBe("menu");
    },
  );

  it("Move to… another truck = dragging to its header", async () => {
    const dragged = await bodyOf(() => drag(q("lane:T2"), q("stop:A")));
    const menu = await bodyOf(async () => {
      openMenu(q("stop:A"));
      fireEvent.click(menuItem("Move to…"));
      await chooseLane(/^Truck T2/);
    });
    expect(dragged.body).toMatchObject({
      type: "move_stops",
      truck_id: "T2",
      expected_lane_versions: { T1: 3, T2: 5 },
    });
    expect(menu.body).toEqual(dragged.body);
  });

  it("Unassign: drag to tray = menu = U key", async () => {
    const dragged = await bodyOf(() =>
      drag(screen.getByRole("region", { name: "Order tray" }), q("stop:B")),
    );
    const menu = await bodyOf(() => {
      openMenu(q("stop:B"));
      fireEvent.click(menuItem("Unassign"));
    });
    const keyed = await bodyOf(() => {
      fireEvent.keyDown(q("stop:B"), { key: "Enter" });
      fireEvent.keyDown(q("stop:B"), { key: "u" });
    });
    expect(dragged.body).toEqual({
      type: "unassign_orders",
      order_ids: ["B"],
      expected_lane_versions: { T1: 3 },
    });
    expect(menu.body).toEqual(dragged.body);
    expect(keyed.body).toEqual(dragged.body);
    expect(keyed.modality).toBe("keyboard");
  });

  it("Move load: drag chip = Move load to… = Place mode (R7.2)", async () => {
    const chip = () => q("load:L1");
    const dragged = await bodyOf(() => drag(q("lane:T2"), chip()));
    const menu = await bodyOf(async () => {
      openMenu(chip());
      fireEvent.click(menuItem("Move load to…"));
      await chooseLane(/^Truck T2/);
    });
    const placed = await bodyOf(() => {
      fireEvent.click(chip());
      fireEvent.click(
        screen.getByRole("button", { name: "Move the load to Truck T2" }),
      );
    });
    expect(dragged.body).toMatchObject({
      type: "move_load",
      load_id: "L1",
      truck_id: "T2",
      index: 1,
    });
    expect(menu.body).toEqual(dragged.body);
    expect(placed.body).toEqual(dragged.body);
  });
});

describe("pair a driver and add a lane", () => {
  it("Pair: drag = Pair with truck… = Place mode (R6.1)", async () => {
    fireEvent.click(screen.getByRole("tab", { name: /Drivers/ }));
    const dragged = await bodyOf(() =>
      drag(q("driver-slot:T2"), q("driver:D1")),
    );
    const menu = await bodyOf(async () => {
      openMenu(q("driver:D1"));
      fireEvent.click(menuItem("Pair with truck…"));
      await chooseLane(/^Truck T2/);
    });
    const placed = await bodyOf(() => {
      fireEvent.click(q("driver:D1"));
      fireEvent.click(q("driver-slot:T2"));
    });
    expect(dragged.body).toEqual({
      type: "pair_driver",
      truck_id: "T2",
      driver_id: "D1",
      expected_lane_versions: { T2: 5 },
    });
    expect(menu.body).toEqual(dragged.body);
    expect(placed.body).toEqual(dragged.body);
  });

  it("Add lane: drop on the board = Add lane button", async () => {
    fireEvent.click(screen.getByRole("tab", { name: /Trucks/ }));
    const row = screen.getByText("Truck T9").closest("li") as HTMLElement;
    act(() => {
      startDrag(row);
    });
    const area = await screen.findByText("Drop the truck here to add a lane");
    const dropped = await bodyOf(() => drag(area, row));
    const button = await bodyOf(() => {
      fireEvent.click(
        screen.getByRole("button", { name: "Add lane for Truck T9" }),
      );
    });
    expect(dropped.body).toEqual({
      type: "add_lane",
      truck_id: "T9",
      expected_lane_versions: { T9: 0 },
    });
    expect(button.body).toEqual(dropped.body);
  });
});

describe("Assign to truck… list and Place mode", () => {
  it("blocked lanes are disabled and say why in their name (R18.2)", async () => {
    mockValidate.mockResolvedValue({
      degraded_sources: [],
      results: {
        T1: {
          outcome: "pass",
          reason: null,
          worst_checks: [],
          preview: {
            fill_by_compartment: {},
            insertion_index: null,
            load_id: null,
            eta_delta_minutes: null,
          },
        },
        T2: {
          outcome: "block",
          reason: null,
          worst_checks: [
            makeCheck({
              outcome: "block",
              message: "Truck certification lapsed.",
            }),
          ],
          preview: {
            fill_by_compartment: {},
            insertion_index: null,
            load_id: null,
            eta_delta_minutes: null,
          },
        },
      },
    });
    openMenu(q("order:O1"));
    fireEvent.click(menuItem("Assign to truck…"));
    const dialog = await screen.findByRole("dialog");
    const blocked = await within(dialog).findByRole("button", {
      name: "Truck T2, no driver, blocked: Truck certification lapsed.",
    });
    expect(blocked).toHaveAttribute("aria-disabled", "true");
    expect(blocked).toHaveTextContent("Truck certification lapsed.");
    expect(
      within(dialog).getByRole("button", { name: "Truck T1, no driver, OK" }),
    ).not.toHaveAttribute("aria-disabled");
    fireEvent.click(blocked);
    expect(mockSend).not.toHaveBeenCalled();
    // Searchable.
    fireEvent.change(within(dialog).getByRole("searchbox"), {
      target: { value: "T1" },
    });
    expect(
      within(dialog).queryByRole("button", { name: /^Truck T2/ }),
    ).toBeNull();
  });

  it("Place mode shows the banner and Escape cancels it (R18.3)", async () => {
    fireEvent.click(q("order:O1"));
    expect(
      screen.getByText("Placing Order O1. Choose a truck or press Escape."),
    ).toBeInTheDocument();
    expect(q("order:O1")).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(q("order:O1"), { key: "Escape" });
    expect(
      screen.queryByText("Placing Order O1. Choose a truck or press Escape."),
    ).toBeNull();
    expect(q("order:O1")).toHaveAttribute("aria-selected", "false");
  });
});
