/**
 * Live regions and announcements (plan task 32; R19.1, R19.2, R14.2,
 * R11.2, R12.8, R6.2). Text is checked on the two regions themselves.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
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

import { BoardApiError } from "../../services/dispatchBoardApi";
import DispatchBoard from "./DispatchBoard";
import {
  makeCheck,
  makeDriver,
  makeLane,
  makeLoad,
  makeTrayOrder,
} from "./state/testFixtures";
import {
  boardSnap,
  mockGetBoard,
  mockSend,
  okResponse,
  renderBoard,
  resetBoardMocks,
  TODAY,
} from "./testBoard";
import { dropOn, resetMocks, socket, sourceData } from "./testMocks";

const polite = () => screen.getByTestId("board-live-polite");
const assertive = () => screen.getByTestId("board-live-assertive");
const q = (key: string) =>
  document.querySelector(`[data-focus-key="${key}"]`) as HTMLElement;

function snapshot() {
  return boardSnap({
    lanes: [
      makeLane("T1", 3, {
        driver_id: "D1",
        driver: makeDriver("D1", { name: "Ana" }),
      }),
      makeLane("T2", 5, { loads: [makeLoad("L2", ["D"])] }),
    ],
    trays: {
      orders: [makeTrayOrder("O1", { product_code: "diesel" })],
      orders_truncated: false,
      drivers: [makeDriver("D1", { name: "Ana", paired_truck_id: "T1" })],
      trucks: [],
    },
  });
}

beforeEach(() => {
  resetMocks();
  resetBoardMocks();
});

it("mounts both regions at page load, before the snapshot arrives", () => {
  mockGetBoard.mockReturnValue(new Promise(() => {}));
  render(<DispatchBoard mode="active_gated" />);
  expect(polite()).toHaveAttribute("aria-live", "polite");
  expect(assertive()).toHaveAttribute("aria-live", "assertive");
  expect(polite()).toHaveAttribute("aria-atomic", "true");
});

describe("command results", () => {
  beforeEach(async () => {
    await renderBoard(snapshot());
  });

  it("announces what moved, where, the fill and the worst check (R19.1)", async () => {
    const lane = makeLane("T2", 6, {
      loads: [
        makeLoad("L2", [], {
          stops: [
            makeLoad("x", ["D"]).stops[0],
            {
              ...makeLoad("x", ["O1"]).stops[0],
              snapshot: {
                ...makeLoad("x", ["O1"]).stops[0].snapshot,
                product_code: "diesel",
              },
            },
          ],
          allocations: [
            {
              order_id: "O1",
              compartment_id: "T2-c1",
              product_code: "diesel",
              liters: 9200,
              capacity_liters: 10000,
            },
          ],
        }),
      ],
      checks: [makeCheck({ scope: { truck_id: "T2", order_id: "O1" } })],
    });
    mockSend.mockResolvedValue(okResponse([lane]));
    await act(async () => {
      dropOn(q("lane:T2"), sourceData(q("order:O1")));
    });
    await waitFor(() =>
      expect(polite()).toHaveTextContent(
        "Order O1, 3,000 gallons diesel, assigned to Truck T2, load 1, stop 2. Compartment 1 is 92% full. Warning: delivery window at risk.",
      ),
    );
  });

  it("names both trucks when a pairing moves (R6.2)", async () => {
    mockSend.mockResolvedValue(
      okResponse([
        makeLane("T2", 6, {
          driver_id: "D1",
          driver: makeDriver("D1", { name: "Ana" }),
        }),
        makeLane("T1", 4, { driver_id: null }),
      ]),
    );
    fireEvent.click(screen.getByRole("tab", { name: /Drivers/ }));
    await act(async () => {
      dropOn(q("driver-slot:T2"), sourceData(q("driver:D1")));
    });
    await waitFor(() =>
      expect(polite()).toHaveTextContent(
        "Ana paired with Truck T2, moved from Truck T1.",
      ),
    );
  });

  it("a conflict names the other dispatcher in the assertive region (R14.2, R19.2)", async () => {
    act(() => {
      socket.handlers.onLanesUpdated?.({
        service_date: TODAY,
        draft_version: 3,
        actor: { user_id: "u2", name: "ana" },
        command_type: "assign_orders",
        lanes: [makeLane("T2", 5, { loads: [makeLoad("L2", ["D"])] })],
      });
    });
    mockSend.mockRejectedValue(
      new BoardApiError("Conflict", 409, "BOARD_LANE_CONFLICT", {
        lanes: [makeLane("T2", 7, { loads: [makeLoad("L2", ["D"])] })],
      }),
    );
    await act(async () => {
      dropOn(q("lane:T2"), sourceData(q("order:O1")));
    });
    await waitFor(() =>
      expect(assertive()).toHaveTextContent(
        "Truck T2 was changed by ana. Your change was not applied.",
      ),
    );
    expect(polite()).not.toHaveTextContent("was changed by");
  });

  it("without a name the conflict says Another dispatcher", async () => {
    mockSend.mockRejectedValue(
      new BoardApiError("Conflict", 409, "BOARD_LANE_CONFLICT", {
        lanes: [makeLane("T2", 9, { loads: [makeLoad("L2", ["D"])] })],
      }),
    );
    await act(async () => {
      dropOn(q("lane:T2"), sourceData(q("order:O1")));
    });
    await waitFor(() =>
      expect(assertive()).toHaveTextContent(
        "Truck T2 was changed by Another dispatcher. Your change was not applied.",
      ),
    );
  });

  it("announces an undo refusal (R11.2)", async () => {
    mockSend.mockResolvedValueOnce(okResponse(snapshot().lanes));
    await act(async () => {
      dropOn(q("lane:T2"), sourceData(q("order:O1")));
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Undo" })).toBeEnabled(),
    );
    mockSend.mockRejectedValueOnce(
      new BoardApiError("Stale", 409, "BOARD_UNDO_STALE", {
        reason: "changed_by_other",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() =>
      expect(polite()).toHaveTextContent(
        "Can't undo. Another dispatcher changed this truck since.",
      ),
    );
  });

  it("a blocked drop says why, politely", async () => {
    mockSend.mockRejectedValue(
      new BoardApiError("Blocked", 422, "BOARD_COMMAND_BLOCKED", {
        checks: {
          T2: [makeCheck({ outcome: "block", message: "Not enough room" })],
        },
      }),
    );
    await act(async () => {
      dropOn(q("lane:T2"), sourceData(q("order:O1")));
    });
    await waitFor(() =>
      expect(polite()).toHaveTextContent("Not applied. Not enough room."),
    );
  });
});

describe("publish results (R12.8)", () => {
  beforeEach(async () => {
    await renderBoard(snapshot());
  });

  it("failures are assertive, success polite, each once", async () => {
    await act(async () => {
      socket.handlers.onPublishProgress?.({
        service_date: TODAY,
        publish_id: "p1",
        lanes: [
          {
            truck_id: "T1",
            state: "failed",
            last_result: { writes_made: false },
          },
          { truck_id: "T2", state: "published", last_result: null },
        ],
      });
    });
    expect(assertive()).toHaveTextContent(
      "Publish failed on Truck T1. Nothing was changed. Retry from the lane.",
    );
    expect(polite()).toHaveTextContent("Truck T2 published.");
    const before = assertive().textContent;
    await act(async () => {
      socket.handlers.onPublishProgress?.({
        service_date: TODAY,
        publish_id: "p1",
        lanes: [
          {
            truck_id: "T1",
            state: "failed",
            last_result: { writes_made: false },
          },
        ],
      });
    });
    expect(assertive().textContent).toBe(before);
  });
});
