/**
 * Dispatch Board shell: one RTL test per state (R21.1–R21.6, R1.4, R2.3,
 * R3.8, R15.6), toolbar view state (R2.2, R4.4) and live lane updates.
 * The API and the socket hook are mocked; nothing reaches a backend.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

const mockReplace = jest.fn();
const mockPush = jest.fn();
let mockParams = new URLSearchParams();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: mockPush }),
  useSearchParams: () => mockParams,
}));
jest.mock("../../services/dispatchBoardApi", () => {
  const actual = jest.requireActual("../../services/dispatchBoardApi");
  return { ...actual, getBoard: jest.fn(), sendBoardCommand: jest.fn() };
});
jest.mock("../../utils/auth", () => ({
  ...jest.requireActual("../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("u1"),
}));
const socketState = { paused: false };
let socketHandlers: import("../../hooks/useDispatchBoardSocket").BoardSocketHandlers =
  {};
jest.mock("../../hooks/useDispatchBoardSocket", () => ({
  useDispatchBoardSocket: (_date: string | null, handlers: never) => {
    socketHandlers = handlers;
    return {
      state: "connected",
      isConnected: true,
      paused: socketState.paused,
      sendPresence: () => true,
    };
  },
}));

import {
  BoardApiError,
  type BoardSnapshot,
  getBoard,
} from "../../services/dispatchBoardApi";
import DispatchBoard from "./DispatchBoard";
import {
  makeLane,
  makeLoad,
  makeSnapshot,
  makeTrayOrder,
} from "./state/testFixtures";
import { todayIn } from "./viewState";

const mockGetBoard = getBoard as jest.MockedFunction<typeof getBoard>;
const today = todayIn("America/Chicago");

function snap(over: Partial<BoardSnapshot> = {}): BoardSnapshot {
  return makeSnapshot({
    service_date: today,
    lanes: [makeLane("T1", 1)],
    trays: {
      orders: [makeTrayOrder("O1")],
      orders_truncated: false,
      drivers: [],
      trucks: [],
    },
    ...over,
  });
}

function renderBoard(mode: "shadow" | "active_gated" = "active_gated") {
  return render(<DispatchBoard mode={mode} />);
}

beforeEach(() => {
  mockParams = new URLSearchParams();
  mockReplace.mockReset();
  mockPush.mockReset();
  mockGetBoard.mockReset();
  socketState.paused = false;
  socketHandlers = {};
  window.localStorage.clear();
});

describe("load states", () => {
  it("shows skeleton lanes and trays while loading (R21.1)", async () => {
    mockGetBoard.mockReturnValue(new Promise(() => {}));
    renderBoard();
    expect(screen.getByText("Loading the board")).toBeInTheDocument();
    expect(screen.getByTestId("lane-skeleton").children).toHaveLength(6);
    expect(screen.getByTestId("tray-skeleton")).toBeInTheDocument();
    // A labelled group, not an APG toolbar (date and search inputs; P4-7).
    expect(screen.getByRole("group", { name: "Board" })).toBeInTheDocument();
  });

  it("no trucks with compartments links to setup (R21.2)", async () => {
    mockGetBoard.mockResolvedValue(
      snap({
        lanes: [],
        trays: { orders: [], orders_truncated: false, drivers: [], trucks: [] },
      }),
    );
    renderBoard();
    fireEvent.click(
      await screen.findByRole("button", { name: "Set up truck compartments" }),
    );
    expect(mockPush).toHaveBeenCalledWith("/dashboard/fleet");
  });

  it("no orders for the day links to Orders (R21.3)", async () => {
    mockGetBoard.mockResolvedValue(
      snap({
        trays: { orders: [], orders_truncated: false, drivers: [], trucks: [] },
      }),
    );
    renderBoard();
    expect(
      await screen.findByText("No orders for this day."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Go to Orders" }));
    expect(mockPush).toHaveBeenCalledWith("/dashboard/orders");
  });

  it("all orders planned shows the count (R21.4)", async () => {
    mockGetBoard.mockResolvedValue(
      snap({
        lanes: [makeLane("T1", 1, { loads: [makeLoad("L1", ["O1", "O2"])] })],
        trays: { orders: [], orders_truncated: false, drivers: [], trucks: [] },
      }),
    );
    renderBoard();
    expect(
      await screen.findByText("All orders are planned (2)."),
    ).toBeInTheDocument();
  });

  it("a disabled board classifies as module disabled (R21.5)", async () => {
    mockGetBoard.mockRejectedValue(
      new BoardApiError("off", 404, "DISPATCH_BOARD_DISABLED"),
    );
    renderBoard();
    expect(
      await screen.findByText("Dispatch Board isn't enabled for your account"),
    ).toBeInTheDocument();
  });

  it("a server error shows the message with Retry, which reloads (R21.5)", async () => {
    mockGetBoard.mockRejectedValueOnce(
      new BoardApiError("Service temporarily unavailable.", 503),
    );
    mockGetBoard.mockResolvedValueOnce(snap());
    renderBoard();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Service temporarily unavailable.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Truck T1")).toBeInTheDocument();
  });

  it("a failed day switch shows the error, not the previous day's board (R21.5)", async () => {
    mockGetBoard.mockResolvedValueOnce(
      snap({ read_only: true, read_only_reason: "past_service_day" }),
    );
    mockGetBoard.mockRejectedValueOnce(
      new BoardApiError("Service temporarily unavailable.", 503),
    );
    renderBoard();
    await screen.findByText("Truck T1");
    fireEvent.click(screen.getByRole("button", { name: "Next day" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Service temporarily unavailable.",
    );
    expect(screen.queryByText("Truck T1")).toBeNull();
    expect(screen.queryByRole("region", { name: "Lanes" })).toBeNull();
    expect(screen.queryByText(/This day is in the past/)).toBeNull();
    expect(mockGetBoard.mock.calls[1][0]).not.toBe(today);
  });

  it("renders lanes with their state badge", async () => {
    mockGetBoard.mockResolvedValue(
      snap({ lanes: [makeLane("T1", 1, { state: "modified" })] }),
    );
    renderBoard();
    const lanes = await screen.findByRole("region", { name: "Lanes" });
    expect(within(lanes).getByText("Modified")).toBeInTheDocument();
  });
});

describe("banners", () => {
  it("shadow mode is read-only with the preview banner (R1.4)", async () => {
    mockGetBoard.mockResolvedValue(
      snap({ mode: "shadow", read_only: true, read_only_reason: "shadow" }),
    );
    renderBoard("shadow");
    expect(
      await screen.findByText("Preview mode, changes are not saved."),
    ).toBeInTheDocument();
  });

  it("a past day is read-only (R2.3)", async () => {
    mockGetBoard.mockResolvedValue(
      snap({ read_only: true, read_only_reason: "past_service_day" }),
    );
    renderBoard();
    expect(
      await screen.findByText(
        "This day is in the past. The board is read-only.",
      ),
    ).toBeInTheDocument();
  });

  it("names each degraded source (R21.6)", async () => {
    mockGetBoard.mockResolvedValue(
      snap({ degraded_sources: ["hos", "delivery_priorities", "locations"] }),
    );
    renderBoard();
    const region = await screen.findByRole("status", {
      name: "Unavailable data",
    });
    expect(region).toHaveTextContent(
      "HOS data unavailable, HOS checks are warnings only.",
    );
    expect(region).toHaveTextContent(
      "Delivery priorities unavailable, orders are sorted by delivery window.",
    );
    expect(region).toHaveTextContent(
      "Delivery locations unavailable, stop times and route checks are estimates.",
    );
  });

  it("truncation banner (R3.8)", async () => {
    mockGetBoard.mockResolvedValue(
      snap({
        trays: {
          orders: [makeTrayOrder("O1")],
          orders_truncated: true,
          drivers: [],
          trucks: [],
        },
      }),
    );
    renderBoard();
    expect(
      await screen.findByText(
        "Showing the 1,000 earliest delivery windows. Filter to narrow the list.",
      ),
    ).toBeInTheDocument();
  });

  it("live updates paused (R15.6)", async () => {
    socketState.paused = true;
    mockGetBoard.mockResolvedValue(snap());
    renderBoard();
    expect(await screen.findByText(/Live updates paused/)).toBeInTheDocument();
  });

  it("no banners on a normal day", async () => {
    mockGetBoard.mockResolvedValue(snap());
    renderBoard();
    await screen.findByText("Truck T1");
    expect(
      screen.queryByText(
        /Preview mode|in the past|Live updates paused|earliest delivery/,
      ),
    ).toBeNull();
  });
});

describe("view state and toolbar", () => {
  it("opens the ?date= day and limits navigation to 7 back / 14 ahead (R2.2)", async () => {
    mockParams = new URLSearchParams(`tab=board&date=${today}`);
    mockGetBoard.mockResolvedValue(snap());
    renderBoard();
    await screen.findByText("Truck T1");
    expect(mockGetBoard.mock.calls[0][0]).toBe(today);
    const input = screen.getByLabelText("Service day") as HTMLInputElement;
    expect(input.value).toBe(today);
    expect(input.min < today && input.max > today).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Next day" }));
    const url = new URLSearchParams(
      mockReplace.mock.calls.at(-1)?.[0].slice(1),
    );
    expect(url.get("tab")).toBe("board");
    expect(url.get("date")).not.toBe(today);
    await waitFor(() => expect(mockGetBoard).toHaveBeenCalledTimes(2));
  });

  it("remembers the tenant zone so the first fetch uses the tenant's today (R2.2)", async () => {
    mockGetBoard.mockResolvedValue(
      snap({
        timezone: "Pacific/Kiritimati",
        service_date: todayIn("Pacific/Kiritimati"),
      }),
    );
    const first = renderBoard();
    await screen.findByText("Truck T1");
    expect(
      window.localStorage.getItem("runsheet.dispatchBoard.timezone.v1"),
    ).toBe("Pacific/Kiritimati");
    first.unmount();
    mockGetBoard.mockClear();
    renderBoard();
    await screen.findByText("Truck T1");
    expect(mockGetBoard.mock.calls[0][0]).toBe(todayIn("Pacific/Kiritimati"));
  });

  it("zoom, density and search go to the URL and local storage (R4.4)", async () => {
    mockGetBoard.mockResolvedValue(snap());
    renderBoard();
    await screen.findByText("Truck T1");
    fireEvent.click(screen.getByRole("button", { name: "Sequence" }));
    fireEvent.click(screen.getByRole("button", { name: "Compact" }));
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "1042" },
    });
    const url = new URLSearchParams(
      mockReplace.mock.calls.at(-1)?.[0].slice(1),
    );
    expect(url.get("zoom")).toBe("sequence");
    expect(url.get("density")).toBe("compact");
    expect(url.get("q")).toBe("1042");
    expect(screen.getByRole("button", { name: "Sequence" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const stored = JSON.parse(
      window.localStorage.getItem("runsheet.dispatchBoard.view.v1") ?? "{}",
    );
    expect(stored).toMatchObject({
      zoom: "sequence",
      density: "compact",
      search: "1042",
    });
  });

  it("filters go to the server only after a truncated response (R3.8, K5)", async () => {
    mockGetBoard.mockResolvedValue(snap());
    renderBoard();
    await screen.findByText("Truck T1");
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    fireEvent.click(screen.getByRole("button", { name: "Will call" }));
    expect(screen.getByRole("button", { name: "Will call" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(mockGetBoard).toHaveBeenCalledTimes(1);
    expect(mockGetBoard.mock.calls[0][1]?.filters).toBeUndefined();
  });

  it("after truncation, a filter change refetches with the server params", async () => {
    mockGetBoard.mockResolvedValue(
      snap({
        trays: {
          orders: [makeTrayOrder("O1")],
          orders_truncated: true,
          drivers: [],
          trucks: [],
        },
      }),
    );
    renderBoard();
    await screen.findByText("Truck T1");
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    fireEvent.click(screen.getByRole("button", { name: "Overdue" }));
    await waitFor(() => expect(mockGetBoard).toHaveBeenCalledTimes(2));
    expect(mockGetBoard.mock.calls[1][1]?.filters).toMatchObject({
      window: "overdue",
    });
  });

  it("undo is disabled with nothing to undo; help lists shortcuts", async () => {
    mockGetBoard.mockResolvedValue(snap());
    renderBoard();
    await screen.findByText("Truck T1");
    expect(screen.getByRole("button", { name: "Undo" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Redo" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Keyboard shortcuts" }));
    expect(screen.getByText("Assign selected")).toBeInTheDocument();
  });
});

describe("live updates", () => {
  it("applies a newer lane from the socket and shows presence names", async () => {
    mockGetBoard.mockResolvedValue(snap());
    renderBoard();
    await screen.findByText("Truck T1");
    act(() => {
      socketHandlers.onLanesUpdated?.({
        service_date: today,
        draft_version: 3,
        actor: { user_id: "u2", name: "ana" },
        command_type: "pair_driver",
        lanes: [makeLane("T1", 2, { state: "published" })],
      });
      socketHandlers.onPresence?.([
        { user_id: "u2", name: "ana", focus_truck_id: null, last_seen: "x" },
      ]);
    });
    expect(
      within(screen.getByRole("region", { name: "Lanes" })).getByText(
        "Published",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("list", { name: "Also viewing: ana" }),
    ).toBeInTheDocument();
  });

  it("a stale lane refetches only that lane; a poll refetches the board", async () => {
    mockGetBoard.mockResolvedValue(snap());
    renderBoard();
    await screen.findByText("Truck T1");
    await act(async () => {
      socketHandlers.onLaneStale?.({
        service_date: today,
        truck_ids: ["T1"],
        reason: "order_cancelled",
      });
    });
    expect(mockGetBoard.mock.calls[1][1]).toMatchObject({ lanes: ["T1"] });
    await act(async () => {
      socketHandlers.onRefetch?.("poll");
    });
    expect(mockGetBoard).toHaveBeenCalledTimes(3);
    expect(mockGetBoard.mock.calls[2][1]?.lanes).toBeUndefined();
  });
});
