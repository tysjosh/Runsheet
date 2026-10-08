/**
 * `useDispatchBoardSocket` against a mock `WebSocket` (design K10.5, R15).
 * Covers event routing, the paused indicator and 30 s poll, refetch on
 * reconnect, presence, the debounced order-tray refresh and plan-execution
 * progress. No real socket is opened.
 */
import { act, renderHook } from "@testing-library/react";

jest.mock("../utils/auth", () => ({
  getAuthToken: jest.fn().mockResolvedValue("tok"),
}));
jest.mock("../services/tenant", () => ({
  getCurrentTenantId: () => "tenant-1",
}));

import {
  BOARD_POLL_INTERVAL_MS,
  type BoardSocketHandlers,
  ORDERS_REFRESH_DEBOUNCE_MS,
  PRESENCE_INTERVAL_MS,
  useDispatchBoardSocket,
} from "./useDispatchBoardSocket";

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: MockWebSocket[] = [];

  url: string;
  readyState = MockWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: ((e: { wasClean: boolean; code: number }) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  send = jest.fn();

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
  }

  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  emit(type: string, data: unknown) {
    this.onmessage?.({ data: JSON.stringify({ type, data, timestamp: "t" }) });
  }

  drop() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ wasClean: false, code: 1006 });
  }

  sent() {
    return this.send.mock.calls.map((c) => JSON.parse(c[0] as string));
  }
}

const realWebSocket = global.WebSocket;

beforeAll(() => {
  (global as unknown as { WebSocket: unknown }).WebSocket = MockWebSocket;
});
afterAll(() => {
  (global as unknown as { WebSocket: unknown }).WebSocket = realWebSocket;
});
beforeEach(() => {
  MockWebSocket.instances = [];
  jest.useFakeTimers();
  jest.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  });
}

function sockets(path: string) {
  return MockWebSocket.instances.filter((s) => s.url.includes(path));
}

function board() {
  const all = sockets("/ws/dispatch-board");
  return all[all.length - 1];
}

async function mount(
  handlers: BoardSocketHandlers,
  opts: { date?: string; enabled?: boolean; focus?: string | null } = {},
) {
  const hook = renderHook(
    ({ date, enabled, focus }) =>
      useDispatchBoardSocket(date, handlers, { enabled, focusTruckId: focus }),
    {
      initialProps: {
        date: opts.date ?? "2026-10-08",
        enabled: opts.enabled ?? true,
        focus: opts.focus ?? null,
      },
    },
  );
  await flush();
  return hook;
}

describe("connection", () => {
  it("opens the board socket for the day with the token, plus orders and plan execution", async () => {
    await mount({});
    const url = new URL(board().url);
    expect(url.pathname).toBe("/ws/dispatch-board");
    expect(url.searchParams.get("service_date")).toBe("2026-10-08");
    expect(url.searchParams.get("token")).toBe("tok");
    expect(sockets("/ws/orders")).toHaveLength(1);
    expect(sockets("/ws/plan-execution")).toHaveLength(1);
  });

  it("opens nothing while disabled", async () => {
    await mount({}, { enabled: false });
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it("opens exactly one board socket when the date arrives after mount (null → date)", async () => {
    const hook = renderHook(
      ({ date }: { date: string | null }) =>
        useDispatchBoardSocket(date, {}, { enabled: Boolean(date) }),
      { initialProps: { date: null as string | null } },
    );
    await flush();
    expect(sockets("/ws/dispatch-board")).toHaveLength(0);
    hook.rerender({ date: "2026-10-08" });
    await flush();
    const created = sockets("/ws/dispatch-board");
    expect(created).toHaveLength(1);
    expect(
      created.filter((s) => s.readyState !== MockWebSocket.CLOSED),
    ).toHaveLength(1);
    hook.unmount();
    expect(
      sockets("/ws/dispatch-board").every(
        (s) => s.readyState === MockWebSocket.CLOSED,
      ),
    ).toBe(true);
  });

  it("reconnects with the new day's URL", async () => {
    const hook = await mount({});
    act(() => board().open());
    hook.rerender({ date: "2026-10-09", enabled: true, focus: null });
    await flush();
    expect(new URL(board().url).searchParams.get("service_date")).toBe(
      "2026-10-09",
    );
    expect(
      sockets("/ws/dispatch-board").filter(
        (s) => s.readyState !== MockWebSocket.CLOSED,
      ),
    ).toHaveLength(1);
  });
});

describe("events", () => {
  it("routes each board event to its handler and ignores another day's", async () => {
    const h = {
      onLanesUpdated: jest.fn(),
      onLaneStale: jest.fn(),
      onPresence: jest.fn(),
      onPublishProgress: jest.fn(),
      onSuggestionsChanged: jest.fn(),
    };
    await mount(h);
    act(() => {
      const ws = board();
      ws.open();
      ws.emit("board_lanes_updated", {
        service_date: "2026-10-08",
        draft_version: 4,
        actor: { user_id: "u2", name: "ana" },
        command_type: "assign_orders",
        lanes: [{ truck_id: "T1", version: 2 }],
      });
      ws.emit("board_lanes_updated", { service_date: "2026-10-09", lanes: [] });
      ws.emit("board_lane_stale", {
        service_date: "2026-10-08",
        truck_ids: ["T1"],
        reason: "order_cancelled",
      });
      ws.emit("board_presence", {
        service_date: "2026-10-08",
        users: [
          { user_id: "u2", name: "ana", focus_truck_id: "T1", last_seen: "x" },
        ],
      });
      ws.emit("board_publish_progress", {
        service_date: "2026-10-08",
        publish_id: "p1",
        lanes: [],
      });
      ws.emit("board_suggestions_changed", { service_date: "2026-10-08" });
      ws.emit("pong", undefined);
    });
    expect(h.onLanesUpdated).toHaveBeenCalledTimes(1);
    expect(h.onLanesUpdated.mock.calls[0][0].actor.name).toBe("ana");
    expect(h.onLaneStale).toHaveBeenCalledWith(
      expect.objectContaining({ truck_ids: ["T1"] }),
    );
    expect(h.onPresence.mock.calls[0][0][0].name).toBe("ana");
    expect(h.onPublishProgress).toHaveBeenCalledTimes(1);
    expect(h.onSuggestionsChanged).toHaveBeenCalledTimes(1);
  });
});

describe("paused, poll and reconnect", () => {
  it("polls every 30 s while paused and refetches once on reconnect", async () => {
    const onRefetch = jest.fn();
    const hook = await mount({ onRefetch });
    act(() => board().open());
    expect(hook.result.current.paused).toBe(false);
    act(() => jest.advanceTimersByTime(BOARD_POLL_INTERVAL_MS * 2));
    expect(onRefetch).not.toHaveBeenCalled();

    act(() => board().drop());
    expect(hook.result.current.paused).toBe(true);
    // The reconnect attempt opens a new socket but it doesn't come up yet.
    await act(async () => {
      jest.advanceTimersByTime(BOARD_POLL_INTERVAL_MS);
    });
    await flush();
    expect(
      onRefetch.mock.calls.filter((c) => c[0] === "poll").length,
    ).toBeGreaterThanOrEqual(1);
    expect(onRefetch).not.toHaveBeenCalledWith("reconnect");

    act(() => board().open());
    expect(hook.result.current.paused).toBe(false);
    expect(
      onRefetch.mock.calls.filter((c) => c[0] === "reconnect"),
    ).toHaveLength(1);
    const polls = onRefetch.mock.calls.filter((c) => c[0] === "poll").length;
    act(() => jest.advanceTimersByTime(BOARD_POLL_INTERVAL_MS * 2));
    expect(onRefetch.mock.calls.filter((c) => c[0] === "poll").length).toBe(
      polls,
    );
  });

  it("is not paused before the first connection", async () => {
    const hook = await mount({});
    expect(hook.result.current.paused).toBe(false);
  });
});

describe("presence", () => {
  it("sends presence on connect, on focus change and every 15 s", async () => {
    const hook = await mount({}, { focus: "T1" });
    act(() => board().open());
    expect(board().sent()).toEqual([
      { type: "presence", focus_truck_id: "T1" },
    ]);
    hook.rerender({ date: "2026-10-08", enabled: true, focus: "T2" });
    expect(board().sent().at(-1)).toEqual({
      type: "presence",
      focus_truck_id: "T2",
    });
    const n = board().sent().length;
    act(() => jest.advanceTimersByTime(PRESENCE_INTERVAL_MS));
    expect(board().sent()).toHaveLength(n + 1);
    expect(board().sent().at(-1)).toEqual({
      type: "presence",
      focus_truck_id: "T2",
    });
  });
});

describe("companion sockets", () => {
  it("debounces order events into one tray refresh after 1 s", async () => {
    const onOrdersChanged = jest.fn();
    await mount({ onOrdersChanged });
    const orders = sockets("/ws/orders")[0];
    act(() => {
      orders.open();
      orders.emit("order_placed", { order_id: "O1" });
      orders.emit("order_status_changed", { order_id: "O2" });
      orders.emit("order_assigned", { order_id: "O3" });
    });
    act(() => jest.advanceTimersByTime(ORDERS_REFRESH_DEBOUNCE_MS - 1));
    expect(onOrdersChanged).not.toHaveBeenCalled();
    act(() => jest.advanceTimersByTime(1));
    expect(onOrdersChanged).toHaveBeenCalledTimes(1);
  });

  it("forwards plan execution progress", async () => {
    const onExecutionUpdate = jest.fn();
    await mount({ onExecutionUpdate });
    const exec = sockets("/ws/plan-execution")[0];
    const update = {
      plan_id: "bp-L1-r1",
      route_id: "br-L1-r1",
      stop: { station_id: "S1", sequence: 1, status: "completed" },
      completed_stops: 1,
      total_stops: 3,
      updated_at: "t",
    };
    act(() => {
      exec.open();
      exec.emit("execution_update", update);
    });
    expect(onExecutionUpdate).toHaveBeenCalledWith(update);
  });
});
