"use client";
/**
 * Dispatch Board container (design K14.1, K14.9, R2, R4.4, R21).
 *
 * Owns the view state (URL first, local storage for defaults), loads the
 * snapshot for the service day, keeps it live through
 * `useDispatchBoardSocket`, and holds the reducer and command senders that
 * the trays and lanes (plan Phase 5) plug into. This shell renders the
 * toolbar, banners and every load state; the lane and tray bodies are
 * summaries until Phase 5 replaces them with the grid and trays.
 */
import { CalendarClock, Truck } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import {
  type BoardPresenceUser,
  useDispatchBoardSocket,
} from "../../hooks/useDispatchBoardSocket";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import {
  type BoardMode,
  type BoardSnapshot,
  getBoard,
  isAbortError,
  type LaneState,
  type LaneView,
} from "../../services/dispatchBoardApi";
import { getCurrentUserId } from "../../utils/auth";
import {
  Badge,
  EmptyState,
  LoadErrorState,
  ToastContainer,
  useToasts,
} from "../ui";
import { BoardBanners } from "./BoardBanners";
import { BoardToolbar } from "./BoardToolbar";
import {
  announceBlocked,
  announceConflict,
  announceNotSaved,
  announceUndoRefused,
} from "./state/announce";
import {
  boardReducer,
  initialBoardState,
  lanesInOrder,
} from "./state/boardReducer";
import {
  type CommandOutcome,
  useBoardCommands,
} from "./state/useBoardCommands";
import {
  type BoardView,
  clampDate,
  parseView,
  readStoredView,
  readStoredZone,
  serverTrayFilters,
  todayIn,
  viewToParams,
  writeStoredView,
  writeStoredZone,
  zoneAbbreviation,
} from "./viewState";

export interface DispatchBoardProps {
  /** Mode from `/status`; the snapshot's own `mode` wins once loaded. */
  mode: BoardMode;
  /** Leave the board (LoadErrorState "back"). */
  onExit?: () => void;
}

const LANE_STATE_LABEL: Record<
  LaneState,
  {
    label: string;
    variant: "neutral" | "success" | "warning" | "info" | "error";
  }
> = {
  draft: { label: "Draft", variant: "neutral" },
  published: { label: "Published", variant: "success" },
  modified: { label: "Modified", variant: "warning" },
  publishing: { label: "Publishing", variant: "info" },
  failed: { label: "Publish failed", variant: "error" },
  recovering: { label: "Recovering", variant: "error" },
};

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

function BoardSkeleton() {
  return (
    <div className="flex flex-1 gap-4 p-4">
      <div role="status" className="sr-only">
        Loading the board
      </div>
      <div
        aria-hidden="true"
        className="w-80 shrink-0 space-y-3"
        data-testid="tray-skeleton"
      >
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="h-16 animate-pulse rounded-md bg-gray-200" />
        ))}
      </div>
      <div
        aria-hidden="true"
        className="flex-1 space-y-3"
        data-testid="lane-skeleton"
      >
        {Array.from({ length: 6 }, (_, i) => (
          <div
            key={i}
            className="h-[104px] animate-pulse rounded-md bg-gray-200"
          />
        ))}
      </div>
    </div>
  );
}

function stopCount(lanes: LaneView[]): number {
  return lanes.reduce(
    (n, l) => n + l.loads.reduce((m, ld) => m + ld.stops.length, 0),
    0,
  );
}

export default function DispatchBoard({ mode, onExit }: DispatchBoardProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [view, setView] = useState<BoardView>(() =>
    parseView(
      new URLSearchParams(searchParams?.toString() ?? ""),
      readStoredView(storage()),
    ),
  );
  const [state, dispatch] = useReducer(boardReducer, initialBoardState);
  const [failure, setFailure] = useState<LoadFailure | null>(null);
  const [presence, setPresence] = useState<BoardPresenceUser[]>([]);
  const { toasts, addToast, dismissToast } = useToasts();

  const snapshot = state.snapshot;
  const [storedZone] = useState(() => readStoredZone(storage()));
  const timezone = snapshot?.timezone ?? storedZone;
  useEffect(() => {
    if (snapshot?.timezone) writeStoredZone(storage(), snapshot.timezone);
  }, [snapshot?.timezone]);
  const today = todayIn(timezone);
  const serviceDate = clampDate(view.date ?? today, today);

  // ── View state: URL + local storage (R4.4) ────────────────────────────────
  const updateView = useCallback(
    (patch: Partial<BoardView>) => {
      setView((prev) => {
        const next = { ...prev, ...patch };
        writeStoredView(storage(), next);
        const params = viewToParams(
          next,
          new URLSearchParams(searchParams?.toString() ?? ""),
        );
        const qs = params.toString();
        router.replace(qs ? `?${qs}` : "?", { scroll: false });
        return next;
      });
    },
    [router, searchParams],
  );

  // ── Snapshot loading ──────────────────────────────────────────────────────
  // Server tray filters only after a truncated response (K5, R3.8); sticky per day.
  const serverFiltering = useRef(false);
  const filtersRef = useRef(view.filters);
  filtersRef.current = view.filters;
  const loadAbort = useRef<AbortController | null>(null);
  const dateRef = useRef(serviceDate);
  dateRef.current = serviceDate;
  const snapshotRef = useRef(snapshot);
  snapshotRef.current = snapshot;

  const load = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      loadAbort.current?.abort();
      const controller = new AbortController();
      loadAbort.current = controller;
      const date = dateRef.current;
      // A foreground load shows the skeleton, not a previous failure.
      if (!background) setFailure(null);
      try {
        const snap: BoardSnapshot = await getBoard(date, {
          filters: serverFiltering.current
            ? serverTrayFilters(filtersRef.current)
            : undefined,
          signal: controller.signal,
        });
        if (controller.signal.aborted || date !== dateRef.current) return;
        if (snap.trays.orders_truncated) serverFiltering.current = true;
        dispatch({ type: "snapshotLoaded", snapshot: snap });
        setFailure(null);
      } catch (err) {
        if (isAbortError(err) || controller.signal.aborted) return;
        // A background refresh keeps showing the last good board of this day.
        if (!background || snapshotRef.current?.service_date !== date) {
          setFailure(classifyLoadError(err, "The board could not be loaded."));
        }
      } finally {
        if (loadAbort.current === controller) loadAbort.current = null;
      }
    },
    [],
  );
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    serverFiltering.current = false;
    void loadRef.current();
    return () => loadAbort.current?.abort();
  }, [serviceDate]);

  // Refetch when the server filters change while the server is filtering.
  const serverKey = JSON.stringify(serverTrayFilters(view.filters));
  const lastServerKey = useRef(serverKey);
  useEffect(() => {
    if (lastServerKey.current === serverKey) return;
    lastServerKey.current = serverKey;
    if (serverFiltering.current) void loadRef.current({ background: true });
  }, [serverKey]);

  const loadLanes = useCallback(async (truckIds: string[]) => {
    if (truckIds.length === 0) return;
    const date = dateRef.current;
    try {
      const snap = await getBoard(date, { lanes: truckIds });
      if (date !== dateRef.current) return;
      dispatch({
        type: "lanesReceived",
        source: "fetch",
        lanes: snap.lanes,
        draftVersion: snap.draft_version,
      });
    } catch {
      // Socket and poll will catch up; a lane refetch failure is not shown.
    }
  }, []);

  // Deep link: select the order once the day is loaded (R1.6).
  const selectedDeepLink = useRef(false);
  useEffect(() => {
    if (!snapshot || selectedDeepLink.current || !view.order) return;
    selectedDeepLink.current = true;
    dispatch({ type: "selectionChanged", keys: [`order:${view.order}`] });
  }, [snapshot, view.order]);

  // ── Live updates ──────────────────────────────────────────────────────────
  const lanesRef = useRef(state.lanesById);
  lanesRef.current = state.lanesById;
  // Own echoed events must not name the current user as "the other dispatcher".
  const selfUserId = useRef<string | null>(null);
  useEffect(() => {
    let active = true;
    void getCurrentUserId().then((id) => {
      if (active) selfUserId.current = id;
    });
    return () => {
      active = false;
    };
  }, []);
  const socket = useDispatchBoardSocket(
    snapshot ? serviceDate : null,
    {
      onLanesUpdated: (e) => {
        if (e.lanes_truncated) {
          void loadLanes(e.lanes.map((l) => l.truck_id));
          return;
        }
        dispatch({
          type: "lanesReceived",
          source: "socket",
          lanes: e.lanes as LaneView[],
          draftVersion: e.draft_version,
          actor: e.actor,
          selfUserId: selfUserId.current,
        });
      },
      onLaneStale: (e) => void loadLanes(e.truck_ids),
      onPresence: setPresence,
      onPublishProgress: (e) => void loadLanes(e.lanes.map((l) => l.truck_id)),
      onSuggestionsChanged: () => void loadRef.current({ background: true }),
      onRefetch: () => void loadRef.current({ background: true }),
      onOrdersChanged: () => void loadRef.current({ background: true }),
      onExecutionUpdate: (u) => {
        const lane = Object.values(lanesRef.current).find((l) =>
          Object.values(l.publish.plans).some((p) => p.plan_id === u.plan_id),
        );
        if (lane) void loadLanes([lane.truck_id]);
      },
    },
    { enabled: Boolean(snapshot), focusTruckId: view.truck },
  );

  // ── Commands (undo/redo here; every other sender is wired in Phase 5) ─────
  const onOutcome = useCallback(
    (o: CommandOutcome) => {
      switch (o.kind) {
        case "conflict": {
          // Name the lane whose version moved, not just the first returned.
          const expected = o.command.expected_lane_versions;
          const changed =
            o.lanes.find(
              (l) =>
                expected[l.truck_id] !== undefined &&
                l.version !== expected[l.truck_id],
            ) ?? o.lanes[0];
          const truck = changed?.truck_id ?? "";
          const actor = state.laneActors[truck]?.name;
          addToast(announceConflict(truck, actor), "error");
          break;
        }
        case "blocked":
          addToast(announceBlocked(o.checks), "error");
          break;
        case "undo_stale":
          addToast(
            announceUndoRefused(o.reason, o.command.type === "reapply"),
            "error",
          );
          break;
        case "network":
          addToast(announceNotSaved(), "error");
          break;
        case "error":
          addToast(
            o.error instanceof Error ? o.error.message : "Not saved.",
            "error",
          );
          break;
        default:
          break;
      }
    },
    [addToast, state.laneActors],
  );
  const commands = useBoardCommands({
    serviceDate,
    state,
    dispatch,
    onOutcome,
  });

  // ── Derived ───────────────────────────────────────────────────────────────
  const lanes = useMemo(() => lanesInOrder(state), [state]);
  const readOnly = snapshot?.read_only ?? mode === "shadow";
  const readOnlyReason =
    snapshot?.read_only_reason ?? (mode === "shadow" ? "shadow" : null);
  const products = useMemo(
    () =>
      [
        ...new Set(
          (snapshot?.trays.orders ?? [])
            .map((o) => o.product_code)
            .filter((p): p is string => Boolean(p)),
        ),
      ].sort(),
    [snapshot],
  );
  const priorities = useMemo(
    () =>
      [
        ...new Set(
          (snapshot?.trays.orders ?? [])
            .map((o) => o.priority_bucket)
            .filter((p): p is string => Boolean(p)),
        ),
      ].sort(),
    [snapshot],
  );

  const toolbar = (
    <BoardToolbar
      view={view}
      serviceDate={serviceDate}
      today={today}
      zone={timezone ? zoneAbbreviation(timezone) : ""}
      shifts={snapshot?.shifts ?? []}
      products={products}
      priorities={priorities}
      presence={presence}
      canUndo={!readOnly && state.undoStack.length > 0}
      canRedo={!readOnly && state.redoStack.length > 0}
      onViewChange={updateView}
      onDateChange={(date) => updateView({ date: clampDate(date, today) })}
      onUndo={() => void commands.undo()}
      onRedo={() => void commands.redo()}
    />
  );

  // The held snapshot is for another day while a day switch loads or failed.
  const currentDay = snapshot?.service_date === serviceDate;
  let body: React.ReactNode;
  if (failure && !currentDay) {
    body = (
      <LoadErrorState
        failure={failure}
        entityLabel="Board"
        onBack={() => (onExit ? onExit() : router.push("/dashboard"))}
        backLabel="Back to Dispatch"
        onRetry={() => void load()}
      />
    );
  } else if (!snapshot || !currentDay) {
    body = <BoardSkeleton />;
  } else if (
    snapshot.lanes.length === 0 &&
    snapshot.trays.trucks.length === 0
  ) {
    body = (
      <EmptyState
        icon={<Truck />}
        title="No trucks with compartments"
        description="Set up truck compartments to plan loads on the board."
        action={{
          label: "Set up truck compartments",
          onClick: () => router.push("/dashboard/fleet"),
        }}
      />
    );
  } else {
    const trayOrders = snapshot.trays.orders;
    const planned = stopCount(lanes);
    body = (
      <div className="flex min-h-0 flex-1">
        <section
          aria-label="Trays"
          className="w-80 shrink-0 overflow-auto border-r border-gray-200 bg-white p-4"
        >
          <h2 className="text-sm font-semibold text-gray-900">Orders</h2>
          {trayOrders.length === 0 && planned === 0 && (
            <div className="mt-2 text-sm text-gray-600">
              <p>No orders for this day.</p>
              <button
                type="button"
                className="mt-1 font-medium text-primary underline hover:no-underline"
                onClick={() => router.push("/dashboard/orders")}
              >
                Go to Orders
              </button>
            </div>
          )}
          {trayOrders.length === 0 && planned > 0 && (
            <p className="mt-2 text-sm text-gray-600">
              All orders are planned ({planned}).
            </p>
          )}
          {trayOrders.length > 0 && (
            <p className="mt-2 text-sm text-gray-600">
              {trayOrders.length} {trayOrders.length === 1 ? "order" : "orders"}{" "}
              to plan
            </p>
          )}
          <h2 className="mt-4 text-sm font-semibold text-gray-900">Drivers</h2>
          <p className="mt-1 text-sm text-gray-600">
            {snapshot.trays.drivers.length} drivers
          </p>
          <h2 className="mt-4 text-sm font-semibold text-gray-900">Trucks</h2>
          <p className="mt-1 text-sm text-gray-600">
            {snapshot.trays.trucks.length} trucks without a lane
          </p>
        </section>
        <section
          aria-label="Lanes"
          className="min-w-0 flex-1 overflow-auto p-4"
        >
          {lanes.length === 0 ? (
            <EmptyState
              icon={<CalendarClock />}
              title="No trucks on the board yet"
              description="Add a truck from the truck tray to start planning."
            />
          ) : (
            <ul className="space-y-2">
              {lanes.map((lane) => {
                const badge = LANE_STATE_LABEL[lane.state];
                return (
                  <li
                    key={lane.truck_id}
                    className={`flex items-center gap-3 rounded-md border border-gray-200 bg-white px-4 ${view.density === "compact" ? "h-[72px]" : "h-[104px]"}`}
                  >
                    <span className="font-medium text-gray-900">
                      Truck {lane.truck_id}
                    </span>
                    <span className="text-sm text-gray-600">
                      {lane.driver?.name ?? "No driver"}
                    </span>
                    <Badge variant={badge.variant}>{badge.label}</Badge>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>
    );
  }

  return (
    <div
      className="flex h-full min-h-0 flex-col"
      data-density={view.density}
      data-zoom={view.zoom}
    >
      {toolbar}
      {snapshot && currentDay && (
        <BoardBanners
          readOnlyReason={readOnly ? readOnlyReason : null}
          paused={socket.paused}
          degradedSources={snapshot.degraded_sources}
          truncated={snapshot.trays.orders_truncated}
        />
      )}
      {body}
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
