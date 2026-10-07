/**
 * Drag wiring on the board (plan task 29; R5.1, R5.2, R5.7, R6.1, R7.1,
 * R7.2, R8.3–R8.6, R4.5). Jest invokes the Pragmatic drop handlers directly
 * through `testMocks`; Playwright drives real drags in task 37.
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
import {
  makeDriver,
  makeLane,
  makeLoad,
  makeTrayOrder,
} from "../state/testFixtures";
import {
  boardSnap,
  emptyValidate,
  mockSend,
  mockValidate,
  okResponse,
  renderBoard,
  resetBoardMocks,
  sentBodies,
  sentModalities,
} from "../testBoard";
import {
  dragOver,
  dropOn,
  resetMocks,
  sourceData,
  startDrag,
} from "../testMocks";
import { POSITION_DEBOUNCE_MS } from "../useBoardController";

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
});

function snapshot() {
  return boardSnap({
    lanes: [
      makeLane("T1", 3, {
        loads: [makeLoad("L1", ["A", "B", "C"])],
      }),
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

const trayCard = (id: string) =>
  document.querySelector(`[data-focus-key="order:${id}"]`) as HTMLElement;
const stopCell = (id: string) =>
  document.querySelector(`[data-focus-key="stop:${id}"]`) as HTMLElement;
const header = (truck: string) =>
  document.querySelector(`[data-focus-key="lane:${truck}"]`) as HTMLElement;
const loadBody = (id: string) =>
  document.querySelector(`[data-load="${id}"]`) as HTMLElement;

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

async function drop(target: Element, data: Record<string, unknown>, x = 0) {
  let accepted = false;
  await act(async () => {
    accepted = dropOn(target, data, x);
  });
  return accepted;
}

describe("drops become commands (input_modality drag)", () => {
  it("order on a lane header: best fit (R5.1)", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T1", 4)]));
    await renderBoard(snapshot());
    await drop(header("T1"), sourceData(trayCard("O1")));
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toEqual({
      type: "assign_orders",
      order_ids: ["O1"],
      truck_id: "T1",
      target: {},
      expected_lane_versions: { T1: 3 },
    });
    expect(sentModalities()).toEqual(["drag"]);
  });

  it("order on the right half of stop B: exactly after it (R5.2)", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T1", 4)]));
    await renderBoard(snapshot());
    stubRect(stopCell("B"), 200, 100);
    await drop(stopCell("B"), sourceData(trayCard("O1")), 290);
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toMatchObject({
      type: "assign_orders",
      target: { load_id: "L1", index: 2 },
    });
  });

  it("order in a load's gap: the pointer snaps to the nearest slot", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T1", 4)]));
    await renderBoard(snapshot());
    stubRect(stopCell("A"), 100, 100);
    stubRect(stopCell("B"), 210, 100);
    stubRect(stopCell("C"), 320, 100);
    await drop(loadBody("L1"), sourceData(trayCard("O1")), 315);
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toMatchObject({
      target: { load_id: "L1", index: 2 },
    });
  });

  it("selected orders drag together as one command (R5.7)", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T2", 6)]));
    await renderBoard(snapshot());
    await act(async () => {
      trayCard("O1").click();
    });
    await act(async () => {
      trayCard("O2").dispatchEvent(
        new MouseEvent("click", { bubbles: true, shiftKey: true }),
      );
    });
    await drop(header("T2"), sourceData(trayCard("O2")));
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toMatchObject({
      type: "assign_orders",
      order_ids: ["O1", "O2"],
      truck_id: "T2",
    });
  });

  it("stop reorder inside its load uses the engine index (R7.1)", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T1", 4)]));
    await renderBoard(snapshot());
    stubRect(stopCell("C"), 300, 100);
    await drop(stopCell("C"), sourceData(stopCell("A")), 390);
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toEqual({
      type: "move_stops",
      order_ids: ["A"],
      truck_id: "T1",
      target: { load_id: "L1", index: 2 },
      expected_lane_versions: { T1: 3 },
    });
  });

  it("stop to another lane touches both lanes; stop to the tray unassigns (R7.1)", async () => {
    mockSend.mockResolvedValue(
      okResponse([
        makeLane("T1", 4, { loads: [makeLoad("L1", ["B", "C"])] }),
        makeLane("T2", 6, { loads: [makeLoad("L2", ["D", "A"])] }),
      ]),
    );
    await renderBoard(snapshot());
    await drop(header("T2"), sourceData(stopCell("A")));
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toMatchObject({
      type: "move_stops",
      order_ids: ["A"],
      truck_id: "T2",
      expected_lane_versions: { T1: 3, T2: 5 },
    });
    await drop(
      screen.getByRole("region", { name: "Order tray" }),
      sourceData(stopCell("B")),
    );
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(2));
    expect(sentBodies()[1]).toMatchObject({
      type: "unassign_orders",
      order_ids: ["B"],
    });
  });

  it("driver on a driver slot pairs; on a load it is refused by canDrop (R6.1)", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T2", 6)]));
    await renderBoard(snapshot());
    fireEvent.click(screen.getByRole("tab", { name: /Drivers/ }));
    const chip = await screen.findByRole("option", { name: /^Ana/ });
    const data = sourceData(chip);
    expect(await drop(loadBody("L2"), data)).toBe(false);
    expect(mockSend).not.toHaveBeenCalled();
    expect(
      await drop(screen.getAllByRole("button", { name: "No driver" })[1], data),
    ).toBe(true);
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toMatchObject({
      type: "pair_driver",
      truck_id: "T2",
      driver_id: "D1",
    });
  });

  it("load chip onto another lane moves the load (R7.2)", async () => {
    mockSend.mockResolvedValue(
      okResponse([makeLane("T1", 4), makeLane("T2", 6)]),
    );
    await renderBoard(snapshot());
    const chip = screen.getByRole("button", {
      name: /^Load 1, terminal No terminal, 3 stops/,
    });
    await drop(header("T2"), sourceData(chip));
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toEqual({
      type: "move_load",
      load_id: "L1",
      truck_id: "T2",
      index: 1,
      expected_lane_versions: { T1: 3, T2: 5 },
    });
  });

  it("a truck dropped on the add-lane area adds a lane (R3.5)", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T9", 1)]));
    await renderBoard(snapshot());
    fireEvent.click(screen.getByRole("tab", { name: /Trucks/ }));
    const row = (await screen.findByText("Truck T9")).closest(
      "li",
    ) as HTMLElement;
    await act(async () => {
      startDrag(row);
    });
    const area = await screen.findByText("Drop the truck here to add a lane");
    await drop(area, sourceData(row));
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(sentBodies()[0]).toMatchObject({ type: "add_lane", truck_id: "T9" });
  });
});

describe("drag-time validation (R8.3, R8.4)", () => {
  it("drag start validates the item against the rendered lanes once", async () => {
    await renderBoard(snapshot());
    await act(async () => {
      startDrag(trayCard("O1"));
    });
    expect(mockValidate).toHaveBeenCalledTimes(1);
    expect(mockValidate.mock.calls[0][1]).toEqual({
      item: { kind: "order", ids: ["O1"] },
      candidates: ["T1", "T2"],
    });
  });

  it("hovering a slot validates that position after 150 ms, aborting earlier calls", async () => {
    jest.useFakeTimers();
    try {
      await renderBoard(snapshot());
      const data = sourceData(trayCard("O1"));
      act(() => {
        startDrag(trayCard("O1"));
      });
      mockValidate.mockClear();
      stubRect(stopCell("A"), 100, 100);
      stubRect(stopCell("B"), 210, 100);
      act(() => {
        dragOver(stopCell("A"), data, 110);
      });
      act(() => {
        jest.advanceTimersByTime(POSITION_DEBOUNCE_MS - 50);
      });
      act(() => {
        dragOver(stopCell("B"), data, 300);
      });
      expect(mockValidate).not.toHaveBeenCalled();
      // The insertion line shows "Checking…" until the answer.
      expect(screen.getByTestId("insertion-indicator")).toHaveTextContent(
        "Checking…",
      );
      await act(async () => {
        jest.advanceTimersByTime(POSITION_DEBOUNCE_MS);
      });
      expect(mockValidate).toHaveBeenCalledTimes(1);
      expect(mockValidate.mock.calls[0][1]).toEqual({
        item: { kind: "order", ids: ["O1"] },
        candidates: ["T1"],
        position: { load_id: "L1", index: 2 },
      });
    } finally {
      jest.useRealTimers();
    }
  });
});

describe("optimistic placement (R8.6)", () => {
  it("draws the order at its target with Checking…, and returns it on refusal", async () => {
    let reject: (e: unknown) => void = () => {};
    mockSend.mockImplementation(
      () =>
        new Promise((_res, rej) => {
          reject = rej;
        }),
    );
    mockValidate.mockResolvedValue(emptyValidate());
    await renderBoard(snapshot());
    await drop(header("T2"), sourceData(trayCard("O1")));
    await waitFor(() =>
      expect(
        within(
          document.querySelector('[data-lane-row="T2"]') as HTMLElement,
        ).getByRole("gridcell", { name: "Order O1, checking" }),
      ).toBeInTheDocument(),
    );
    expect(trayCard("O1")).toBeNull();
    await act(async () => {
      reject(
        new BoardApiError("Blocked", 422, "BOARD_COMMAND_BLOCKED", {
          checks: {
            T2: [
              {
                check: "compartment_fit",
                outcome: "block",
                reason_code: "capacity_shortfall",
                message: "Not enough room.",
                source: "x",
                scope: { truck_id: "T2" },
                warning_id: null,
                fix_link: null,
              },
            ],
          },
        }),
      );
    });
    await waitFor(() => expect(trayCard("O1")).not.toBeNull());
    expect(
      screen.queryByRole("gridcell", { name: "Order O1, checking" }),
    ).toBeNull();
  });
});
