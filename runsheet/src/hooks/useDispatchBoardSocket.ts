/**
 * Live updates for the Dispatch Board (design K10.2, K10.3, K10.5; R15).
 *
 * - `/ws/dispatch-board?service_date=` on `useWebSocket` (reconnect with
 *   backoff, fresh token per attempt). Server events are routed to typed
 *   handlers; presence is sent every 15 s and whenever the focused lane
 *   changes.
 * - While the socket is down after it dropped, `paused` is true (the board
 *   shows "Live updates paused") and `onRefetch("poll")` fires every 30 s.
 *   When it reconnects, `onRefetch("reconnect")` fires once.
 * - `/ws/orders` (`useOrdersWebSocket`) refreshes the order tray, debounced
 *   1 s like `DispatchCockpit`; `/ws/plan-execution`
 *   (`usePlanExecutionSocket`) reports stop progress on published lanes.
 *
 * Board correctness never depends on delivery: versions and refetch do.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { LaneView } from "../services/dispatchBoardApi";
import { getCurrentTenantId } from "../services/tenant";
import { getAuthToken } from "../utils/auth";
import { useOrdersWebSocket } from "./useOrdersWebSocket";
import {
  type ExecutionUpdateData,
  usePlanExecutionSocket,
} from "./usePlanExecutionSocket";
import {
  useWebSocket,
  type WebSocketOptions,
  type WebSocketState,
} from "./useWebSocket";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";
const WS_BASE = API_BASE_URL.replace(/\/api$/, "").replace("http", "ws");
export const DISPATCH_BOARD_WS_BASE_URL = `${WS_BASE}/ws/dispatch-board`;

/** R15.6: snapshot poll while live updates are paused. */
export const BOARD_POLL_INTERVAL_MS = 30_000;
/** K10.3: presence heartbeat. */
export const PRESENCE_INTERVAL_MS = 15_000;
/** K10.4: order tray refresh debounce. */
export const ORDERS_REFRESH_DEBOUNCE_MS = 1_000;

export async function buildDispatchBoardWebSocketUrl(
  serviceDate: string,
): Promise<string> {
  const params = new URLSearchParams({ service_date: serviceDate });
  const token = await getAuthToken();
  if (token) params.set("token", token);
  return `${DISPATCH_BOARD_WS_BASE_URL}?${params.toString()}`;
}

// ─── Event payloads (K10.2) ──────────────────────────────────────────────────

export interface BoardActor {
  user_id: string;
  name: string | null;
}

export interface BoardLanesUpdatedEvent {
  service_date: string;
  draft_version: number;
  actor: BoardActor | null;
  command_type: string;
  /** Full lanes, or `{truck_id, version}` only when `lanes_truncated` (> 256 KB). */
  lanes: (LaneView | { truck_id: string; version: number })[];
  lanes_truncated?: boolean;
}

export interface BoardLaneStaleEvent {
  service_date: string;
  truck_ids: string[];
  reason: string;
}

export interface BoardPresenceUser {
  user_id: string;
  name: string | null;
  focus_truck_id: string | null;
  last_seen: string;
}

export interface BoardPublishProgressEvent {
  service_date: string;
  publish_id: string;
  lanes: {
    truck_id: string;
    state: string;
    last_result: Record<string, unknown> | null;
  }[];
}

export interface BoardSocketHandlers {
  onLanesUpdated?: (event: BoardLanesUpdatedEvent) => void;
  onLaneStale?: (event: BoardLaneStaleEvent) => void;
  onPresence?: (users: BoardPresenceUser[]) => void;
  onPublishProgress?: (event: BoardPublishProgressEvent) => void;
  onSuggestionsChanged?: () => void;
  /** Every 30 s while paused, and once after a reconnect. */
  onRefetch?: (reason: "poll" | "reconnect") => void;
  /** An order was placed, changed status or was assigned (debounced 1 s). */
  onOrdersChanged?: () => void;
  /** `/ws/plan-execution` `execution_update` (stop progress). */
  onExecutionUpdate?: (update: ExecutionUpdateData) => void;
}

export interface DispatchBoardSocketOptions {
  /** `false` keeps every socket closed (board hidden or disabled). */
  enabled?: boolean;
  /** Lane the user is focused on, sent as presence. */
  focusTruckId?: string | null;
}

export interface UseDispatchBoardSocketReturn {
  state: WebSocketState;
  isConnected: boolean;
  /** "Live updates paused": the socket dropped and is not back yet. */
  paused: boolean;
  sendPresence: (focusTruckId: string | null) => boolean;
}

interface BoardMessage {
  type?: string;
  data?: unknown;
}

const ORDER_SUBSCRIPTIONS = [
  "order_placed",
  "order_status_changed",
  "order_assigned",
] as const;

export function useDispatchBoardSocket(
  serviceDate: string | null,
  handlers: BoardSocketHandlers,
  { enabled = true, focusTruckId = null }: DispatchBoardSocketOptions = {},
): UseDispatchBoardSocketReturn {
  const live = enabled && Boolean(serviceDate);
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const dateRef = useRef(serviceDate);
  dateRef.current = serviceDate;
  const focusRef = useRef(focusTruckId);
  focusRef.current = focusTruckId;

  const [dropped, setDropped] = useState(false);
  const everConnected = useRef(false);

  const handleMessage = useCallback((raw: unknown) => {
    const msg = raw as BoardMessage;
    const h = handlersRef.current;
    if (!msg || typeof msg !== "object") return;
    const data = msg.data as Record<string, unknown> | undefined;
    // Ignore events for another day (a late event after a date switch).
    if (
      data &&
      typeof data.service_date === "string" &&
      data.service_date !== dateRef.current
    ) {
      return;
    }
    switch (msg.type) {
      case "board_lanes_updated":
        if (data) h.onLanesUpdated?.(data as unknown as BoardLanesUpdatedEvent);
        break;
      case "board_lane_stale":
        if (data) h.onLaneStale?.(data as unknown as BoardLaneStaleEvent);
        break;
      case "board_presence":
        h.onPresence?.(((data?.users as BoardPresenceUser[]) ?? []).slice());
        break;
      case "board_publish_progress":
        if (data)
          h.onPublishProgress?.(data as unknown as BoardPublishProgressEvent);
        break;
      case "board_suggestions_changed":
        h.onSuggestionsChanged?.();
        break;
      default:
        break; // pong, error frames, unknown types
    }
  }, []);

  const wsOptions: WebSocketOptions = useMemo(
    () => ({
      autoConnect: live,
      initialReconnectDelay: 1000,
      maxReconnectDelay: 30000,
      maxReconnectAttempts: 0,
      backoffMultiplier: 2,
      getUrl: () => buildDispatchBoardWebSocketUrl(dateRef.current ?? ""),
      onConnect: () => {
        const reconnect = everConnected.current;
        everConnected.current = true;
        setDropped(false);
        if (reconnect) handlersRef.current.onRefetch?.("reconnect");
      },
      onDisconnect: () => setDropped(true),
      onMessage: handleMessage,
    }),
    [live, handleMessage],
  );

  const { state, isConnected, connect, send } = useWebSocket("", wsOptions);

  // `useWebSocket` opens and closes with `autoConnect`; a new day on an
  // already-live socket needs a new URL. Going live (e.g. `null` → date) is
  // left to `autoConnect`, so only one socket opens.
  const lastLive = useRef({ date: serviceDate, live });
  useEffect(() => {
    const prev = lastLive.current;
    lastLive.current = { date: serviceDate, live };
    if (!live) return;
    if (!prev.live) {
      everConnected.current = false;
      setDropped(false);
      return;
    }
    if (prev.date === serviceDate) return;
    everConnected.current = false;
    setDropped(false);
    connect();
  }, [live, serviceDate, connect]);

  const paused = live && dropped && state !== "connected";

  // R15.6: poll every 30 s while paused.
  useEffect(() => {
    if (!paused) return;
    const id = setInterval(
      () => handlersRef.current.onRefetch?.("poll"),
      BOARD_POLL_INTERVAL_MS,
    );
    return () => clearInterval(id);
  }, [paused]);

  const sendPresence = useCallback(
    (focus: string | null) => send({ type: "presence", focus_truck_id: focus }),
    [send],
  );

  // K10.3: presence on focus change and every 15 s.
  useEffect(() => {
    if (!isConnected) return;
    sendPresence(focusTruckId);
    const id = setInterval(
      () => sendPresence(focusRef.current),
      PRESENCE_INTERVAL_MS,
    );
    return () => clearInterval(id);
  }, [isConnected, focusTruckId, sendPresence]);

  // /ws/orders → debounced tray refresh.
  const ordersTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleOrders = useCallback(() => {
    if (ordersTimer.current) clearTimeout(ordersTimer.current);
    ordersTimer.current = setTimeout(() => {
      ordersTimer.current = null;
      handlersRef.current.onOrdersChanged?.();
    }, ORDERS_REFRESH_DEBOUNCE_MS);
  }, []);
  useEffect(
    () => () => {
      if (ordersTimer.current) clearTimeout(ordersTimer.current);
    },
    [],
  );
  const ordersOptions = useMemo(
    () => ({
      autoConnect: live,
      subscriptions: [...ORDER_SUBSCRIPTIONS],
      onOrderPlaced: scheduleOrders,
      onOrderStatusChanged: scheduleOrders,
      onOrderAssigned: scheduleOrders,
    }),
    [live, scheduleOrders],
  );
  useOrdersWebSocket(getCurrentTenantId(), ordersOptions);

  // /ws/plan-execution → stop progress.
  const executionOptions = useMemo(
    () => ({
      autoConnect: live,
      onExecutionUpdate: (u: ExecutionUpdateData) =>
        handlersRef.current.onExecutionUpdate?.(u),
    }),
    [live],
  );
  usePlanExecutionSocket(executionOptions);

  return { state, isConnected, paused, sendPresence };
}

export default useDispatchBoardSocket;
