/**
 * The order tray follows the lanes (R3.1; found by the Phase 7 e2e run):
 * an order placed on a truck leaves the tray once the command commits, and a
 * command that can return orders to the tray reloads the snapshot in the
 * background, here or from another dispatcher's socket event.
 */
import { act, screen, waitFor } from "@testing-library/react";

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
  makeLane,
  makeLoad,
  makeStop,
  makeTrayOrder,
} from "../state/testFixtures";
import {
  boardSnap,
  mockGetBoard,
  mockSend,
  okResponse,
  renderBoard,
  resetBoardMocks,
  TODAY,
} from "../testBoard";
import { dropOn, resetMocks, socket, sourceData } from "../testMocks";
import { onLaneOrderIds, trayOrders } from "./trayOrders";

const trayCard = (id: string) =>
  document.querySelector(`[data-focus-key="order:${id}"]`);
const header = (truck: string) =>
  document.querySelector(`[data-focus-key="lane:${truck}"]`) as HTMLElement;

function snapshot() {
  return boardSnap({
    lanes: [makeLane("T1", 3, { loads: [makeLoad("L1", ["A"])] })],
    trays: {
      orders: [makeTrayOrder("O1"), makeTrayOrder("O2")],
      orders_truncated: false,
      drivers: [],
      trucks: [],
    },
  });
}

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
});

describe("trayOrders", () => {
  it("drops orders that are on a lane or its shelf", () => {
    const lanes = {
      T1: makeLane("T1", 1, { loads: [makeLoad("L1", ["O1"])] }),
      T2: makeLane("T2", 1, { shelf: ["O3"] }),
    };
    expect([...onLaneOrderIds(lanes)].sort()).toEqual(["O1", "O3"]);
    const tray = ["O1", "O2", "O3"].map((id) => makeTrayOrder(id));
    expect(trayOrders(tray, lanes).map((o) => o.order_id)).toEqual(["O2"]);
  });
});

describe("the order tray after a command (R3.1)", () => {
  it("an assigned order leaves the tray and the count once the command commits", async () => {
    mockSend.mockResolvedValue(
      okResponse([
        makeLane("T1", 4, {
          loads: [
            makeLoad("L1", ["A"], { stops: [makeStop("A"), makeStop("O1")] }),
          ],
        }),
      ]),
    );
    await renderBoard(snapshot());
    expect(screen.getByRole("tab", { name: "Orders (2)" })).toBeInTheDocument();
    await act(async () => {
      dropOn(header("T1"), sourceData(trayCard("O1") as HTMLElement));
    });
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        document.querySelector('[data-focus-key="stop:O1"]'),
      ).not.toBeNull(),
    );
    expect(trayCard("O1")).toBeNull();
    expect(trayCard("O2")).not.toBeNull();
    expect(screen.getByRole("tab", { name: "Orders (1)" })).toBeInTheDocument();
    // An assign needs no reload: the lanes already say where the order is.
    expect(mockGetBoard).toHaveBeenCalledTimes(1);
  });

  it("an unassign reloads the snapshot so the order comes back to the tray", async () => {
    mockSend.mockResolvedValue(okResponse([makeLane("T1", 4)]));
    await renderBoard(snapshot());
    const back = boardSnap({
      lanes: [makeLane("T1", 4)],
      trays: {
        orders: [makeTrayOrder("A"), makeTrayOrder("O1"), makeTrayOrder("O2")],
        orders_truncated: false,
        drivers: [],
        trucks: [],
      },
    });
    mockGetBoard.mockResolvedValue(back);
    const tray = document.querySelector(
      '[aria-label="Order tray"]',
    ) as HTMLElement;
    const stop = document.querySelector(
      '[data-focus-key="stop:A"]',
    ) as HTMLElement;
    await act(async () => {
      dropOn(tray, sourceData(stop));
    });
    await waitFor(() => expect(mockSend).toHaveBeenCalledTimes(1));
    expect(mockSend.mock.calls[0][1].type).toBe("unassign_orders");
    await waitFor(() => expect(mockGetBoard).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(trayCard("A")).not.toBeNull());
  });

  it("another dispatcher's unassign reloads the snapshot too", async () => {
    await renderBoard(snapshot());
    mockGetBoard.mockClear();
    mockGetBoard.mockResolvedValue(snapshot());
    act(() => {
      socket.handlers.onLanesUpdated?.({
        service_date: TODAY,
        draft_version: 9,
        actor: { user_id: "u2", name: "ben" },
        command_type: "unassign_orders",
        lanes: [makeLane("T1", 9)],
      });
    });
    await waitFor(() => expect(mockGetBoard).toHaveBeenCalledTimes(1));
    act(() => {
      socket.handlers.onLanesUpdated?.({
        service_date: TODAY,
        draft_version: 10,
        actor: { user_id: "u2", name: "ben" },
        command_type: "pair_driver",
        lanes: [makeLane("T1", 10)],
      });
    });
    expect(mockGetBoard).toHaveBeenCalledTimes(1);
  });
});
