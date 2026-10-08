/**
 * Lanes grid (plan task 27; R1.6, R2.5, R2.6, R2.8, R2.9, R4.1, R4.2, R4.5,
 * R6.5, R7.5, R13.5, R19.3). API, socket and Pragmatic are mocked.
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

import {
  makeCheck,
  makeCompartment,
  makeDriver,
  makeLane,
  makeLoad,
  makeStop,
  makeTrayOrder,
} from "../state/testFixtures";
import {
  boardSnap,
  mockSend,
  okResponse,
  renderBoard,
  resetBoardMocks,
  sentBodies,
  sentModalities,
  TODAY,
} from "../testBoard";
import { dropOn, navigation, resetMocks, sourceData } from "../testMocks";
import { addDays } from "../viewState";

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
});

const row = (truck: string) =>
  document.querySelector(`[data-lane-row="${truck}"]`) as HTMLElement;

describe("lane rows", () => {
  it("names each lane with its summary (R19.3)", async () => {
    const load = makeLoad("L1", ["O1", "O2"], {
      allocations: [
        {
          order_id: "O1",
          compartment_id: "T1-c1",
          product_code: "ULSD",
          liters: 5000,
          capacity_liters: 10000,
        },
      ],
    });
    await renderBoard(
      boardSnap({
        lanes: [
          makeLane("T1", 1, {
            driver_id: "D1",
            driver: makeDriver("D1", { name: "Ana" }),
            loads: [load],
            checks: [makeCheck({ scope: { truck_id: "T1", order_id: "O1" } })],
          }),
        ],
      }),
    );
    expect(
      screen.getByRole("row", {
        name: "Truck T1, driver Ana, 1 load, 2 stops, fullest compartment 50%, 1 warning, Draft",
      }),
    ).toBeInTheDocument();
    const grid = screen.getByRole("grid", { name: "Trucks and loads" });
    expect(grid).toHaveAttribute("aria-rowcount", "2");
    // The stop cell carries its warning as text (R19.4).
    expect(
      screen.getByRole("gridcell", {
        name: /Stop 1, Order O1, ULSD, 3,000 gal, no ETA, Warning: Delivery window at risk/,
      }),
    ).toBeInTheDocument();
  });

  it("shows the truck type in the header and the lane name, and nothing when unknown (R2.8)", async () => {
    await renderBoard(
      boardSnap({
        lanes: [
          makeLane("T1", 1, { truck_type: "tank_wagon" }),
          makeLane("T2", 1),
        ],
      }),
    );
    expect(
      screen.getByRole("row", {
        name: "Truck T1 (tank wagon), no driver, 0 loads, 0 stops, 0 warnings, Draft",
      }),
    ).toBeInTheDocument();
    expect(within(row("T1")).getByText("tank wagon")).toBeInTheDocument();
    expect(
      screen.getByRole("row", {
        name: "Truck T2, no driver, 0 loads, 0 stops, 0 warnings, Draft",
      }),
    ).toBeInTheDocument();
  });

  it("the Pair button sends pair_driver for the suggested driver (R6.5)", async () => {
    mockSend.mockResolvedValue(
      okResponse([makeLane("T1", 2, { driver_id: "D7" })]),
    );
    await renderBoard(
      boardSnap({
        lanes: [
          makeLane("T1", 1, {
            suggested_driver: {
              driver_id: "D7",
              name: "Sam",
              source: "assigned_truck_id",
            },
          }),
        ],
      }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Pair Sam" }));
    });
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toEqual({
      type: "pair_driver",
      truck_id: "T1",
      driver_id: "D7",
      expected_lane_versions: { T1: 1 },
    });
    expect(sentModalities()).toEqual(["menu"]);
  });

  it("shades the HOS window on today only (R2.9, R2.10)", async () => {
    const hos = {
      remaining_drive_time: { availability: "available", value: 6 },
      remaining_on_duty_window: { availability: "available", value: 9 },
    };
    const lane = makeLane("T1", 1, {
      driver_id: "D1",
      driver: makeDriver("D1", { hos }),
    });
    const { unmount } = await renderBoard(boardSnap({ lanes: [lane] }));
    const shade = within(row("T1")).getByTestId("hos-window");
    expect(shade).toHaveTextContent(
      /On-duty window ends .*\. Driving limit at /,
    );
    unmount();

    navigation.params = new URLSearchParams(`date=${addDays(TODAY, 2)}`);
    await renderBoard(
      boardSnap({ service_date: addDays(TODAY, 2), lanes: [lane] }),
    );
    expect(within(row("T1")).queryByTestId("hos-window")).toBeNull();
  });

  it("Timeline places stops by ETA; a lane without ETAs falls back to Sequence with a badge (R2.5, R2.6)", async () => {
    const timed = makeLoad("L1", [], {
      stops: [
        makeStop("O1", { eta: `${TODAY}T15:00:00Z` }),
        makeStop("O2", { eta: `${TODAY}T17:00:00Z` }),
      ],
    });
    const untimed = makeLoad("L2", ["O3"]);
    await renderBoard(
      boardSnap({
        lanes: [
          makeLane("T1", 1, { loads: [timed] }),
          makeLane("T2", 1, { loads: [untimed] }),
        ],
      }),
    );
    expect(row("T1")).toHaveAttribute("data-layout", "timeline");
    expect(row("T2")).toHaveAttribute("data-layout", "sequence");
    expect(within(row("T2")).getByText("ETAs unavailable")).toBeInTheDocument();
    expect(within(row("T1")).queryByText("ETAs unavailable")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sequence" }));
    expect(row("T1")).toHaveAttribute("data-layout", "sequence");
  });

  it("shows the To reassign shelf for dispatched orders taken off a load (R13.5)", async () => {
    await renderBoard(
      boardSnap({
        lanes: [
          makeLane("T1", 3, {
            shelf: ["O9"],
            ever_published: true,
            state: "modified",
          }),
        ],
      }),
    );
    expect(within(row("T1")).getByText("To reassign (1)")).toBeInTheDocument();
    expect(
      within(row("T1")).getByRole("gridcell", {
        name: /To reassign, Order O9/,
      }),
    ).toBeInTheDocument();
  });
});

describe("lane menu", () => {
  it("Remove lane is disabled with the reason for an ever-published lane (R7.5)", async () => {
    await renderBoard(
      boardSnap({
        lanes: [
          makeLane("T1", 1, { ever_published: true, state: "published" }),
          makeLane("T2", 1),
        ],
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Truck T1 actions" }));
    const item = within(screen.getByRole("menu")).getByRole("menuitem", {
      name: /Remove lane/,
    });
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(item).toHaveTextContent("Move or reassign this truck's stops first");
    fireEvent.click(item);
    expect(mockSend).not.toHaveBeenCalled();

    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    mockSend.mockResolvedValue(okResponse([]));
    fireEvent.click(screen.getByRole("button", { name: "Truck T2 actions" }));
    await act(async () => {
      fireEvent.click(
        within(screen.getByRole("menu")).getByRole("menuitem", {
          name: "Remove lane",
        }),
      );
    });
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toEqual({
      type: "remove_lane",
      truck_id: "T2",
      expected_lane_versions: { T2: 1 },
    });
  });

  it("a collapsed lane still takes drops (R4.5)", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T1", 2)]));
    await renderBoard(
      boardSnap({
        lanes: [makeLane("T1", 1, { loads: [makeLoad("L1", ["O1"])] })],
        trays: {
          orders: [makeTrayOrder("O5")],
          orders_truncated: false,
          drivers: [],
          trucks: [],
        },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Truck T1 actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Collapse lane" }));
    expect(
      within(row("T1")).getByText(/Collapsed · 1 stops/),
    ).toBeInTheDocument();
    const card = screen
      .getAllByRole("option")
      .find(
        (o) => o.getAttribute("data-focus-key") === "order:O5",
      ) as HTMLElement;
    await act(async () => {
      dropOn(
        screen.getByRole("button", { name: "Truck T1 actions" }),
        sourceData(card),
      );
    });
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toMatchObject({
      type: "assign_orders",
      order_ids: ["O5"],
      truck_id: "T1",
      target: {},
    });
  });
});

describe("windowing, deep link, filters", () => {
  const many = () =>
    Array.from({ length: 60 }, (_, i) => makeLane(`T${i + 1}`, 1));

  it("renders a window of the 60 lanes and keeps the full row count", async () => {
    await renderBoard(boardSnap({ lanes: many() }));
    const rows = document.querySelectorAll("[data-lane-row]");
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThan(60);
    expect(screen.getByRole("grid")).toHaveAttribute("aria-rowcount", "61");
  });

  it("?truck= scrolls the windowed grid to that lane (R1.6)", async () => {
    navigation.params = new URLSearchParams("truck=T50");
    await renderBoard(boardSnap({ lanes: many() }));
    await waitFor(() => expect(row("T50")).not.toBeNull());
    const scroller = screen.getByRole("region", { name: "Lanes" });
    expect(Number(scroller.getAttribute("data-scroll-top"))).toBe(49 * 104);
    expect(row("T1")).toBeNull();
  });

  it("filter chips and search dim non-matching stop cards and never remove them (R4.1, R4.2)", async () => {
    navigation.params = new URLSearchParams("product=ULSD");
    const load = makeLoad("L1", [], {
      stops: [
        makeStop("O1"),
        makeStop("O2", {
          snapshot: { ...makeStop("O2").snapshot, product_code: "GAS" },
        }),
      ],
    });
    await renderBoard(
      boardSnap({
        lanes: [
          makeLane("T1", 1, {
            loads: [load],
            compartments: [makeCompartment("c1")],
          }),
        ],
      }),
    );
    const cell = (id: string) =>
      document.querySelector(`[data-focus-key="stop:${id}"]`) as HTMLElement;
    expect(cell("O1")).toHaveAttribute("data-match", "plain");
    expect(cell("O2")).toHaveAttribute("data-match", "dim");
    fireEvent.change(screen.getByRole("searchbox", { name: /Search orders/ }), {
      target: { value: "O1" },
    });
    expect(cell("O1")).toHaveAttribute("data-match", "match");
    expect(cell("O2")).toBeInTheDocument();
  });
});
