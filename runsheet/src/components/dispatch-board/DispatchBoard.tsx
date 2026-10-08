"use client";
/**
 * Dispatch Board container (design K14.1, K14.9, R2, R4.4, R21).
 *
 * Owns the view state (URL first, local storage for defaults), loads the
 * snapshot for the service day, keeps it live through
 * `useDispatchBoardSocket`, and holds the reducer, command senders and the
 * interaction controller that trays, lanes, menus and dialogs share
 * (`BoardContext`). Renders the toolbar, banners, every load state, the
 * trays, the lanes grid, Place mode, the shortcut help and the two live
 * regions (mounted at page load, R19.1).
 *
 * Phase 6 adds the detail drawer, the Publish review and its progress
 * (socket + polling), failed / recovering publish banners, agent
 * suggestions with Generate plan, the map split view and live truck
 * positions for the Running late badge (R12, R13, R15.4, R16, R17, R22.3).
 */
import { Truck } from "lucide-react";
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
  boardErrorCode,
  getBoard,
  getPublish,
  isAbortError,
  type LaneView,
  newClientId,
  type PublishAccepted,
  type PublishResult,
  type PublishStatus,
  publishBoard,
} from "../../services/dispatchBoardApi";
import { generatePlan } from "../../services/fuelApi";
import { getCurrentTenantId } from "../../services/tenant";
import { getCurrentUserId } from "../../utils/auth";
import { EmptyState, LoadErrorState, ToastContainer, useToasts } from "../ui";
import { BoardBanners } from "./BoardBanners";
import {
  BoardContext,
  type BoardUiActions,
  type DrawerTarget,
} from "./BoardContext";
import { BoardLiveRegion, useAnnouncer } from "./BoardLiveRegion";
import { BoardToolbar } from "./BoardToolbar";
import { AssignToMenu } from "./dialogs/AssignToMenu";
import { CardMenu } from "./dialogs/CardMenu";
import {
  readSingleKeyEnabled,
  ShortcutHelpDialog,
  writeSingleKeyEnabled,
} from "./dialogs/ShortcutHelpDialog";
import { DetailDrawer } from "./drawer/DetailDrawer";
import { useTerminalIndex } from "./drawer/useTerminalIndex";
import { BoardGrid } from "./grid/BoardGrid";
import { handleShortcut } from "./keyboard/useBoardShortcuts";
import { LaneRouteMap } from "./map/LaneRouteMap";
import { LivePositionFeed } from "./map/LivePositionFeed";
import { minutesLate, type TruckPosition } from "./map/runningLate";
import { PlaceModeBanner } from "./PlaceModeBanner";
import { PublishBanners } from "./publish/PublishBanners";
import { PublishDialog } from "./publish/PublishDialog";
import { looksReady, retryGroup, trucksText } from "./publish/publishText";
import { BottomSheet } from "./responsive/BottomSheet";
import { useStackedLayout } from "./responsive/useStackedLayout";
import {
  announceBlocked,
  announceCommitted,
  announceConflict,
  announceNotSaved,
  announcePublishResult,
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
import { SuggestionsDialog } from "./suggestions/SuggestionsDialog";
import { TrayPanel } from "./trays/TrayPanel";
import { RETURNS_ORDERS_TO_TRAY, trayOrders } from "./trays/trayOrders";
import { useBoardController } from "./useBoardController";
import {
  addDays,
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

/** Publish status poll while a publish runs (the socket may be paused). */
export const PUBLISH_POLL_MS = 2_000;
/** Running late is re-evaluated this often between position updates. */
const LATE_TICK_MS = 60_000;
const RUNNING_STATES = new Set(["queued", "publishing"]);

interface ActivePublish {
  publishId: string;
  status: PublishStatus;
}

/** Merge progress lanes into a status; `done` once no lane is still running. */
function mergeStatus(
  prev: PublishStatus,
  lanes: { truck_id: string; state: string; last_result: unknown }[],
  done?: boolean,
): PublishStatus {
  const byTruck = new Map(prev.lanes.map((l) => [l.truck_id, l]));
  for (const l of lanes) {
    byTruck.set(l.truck_id, {
      truck_id: l.truck_id,
      state: l.state as PublishStatus["lanes"][number]["state"],
      last_result: (l.last_result as PublishResult | null) ?? null,
    });
  }
  const merged = [...byTruck.values()];
  return {
    publish_id: prev.publish_id,
    lanes: merged,
    done: done ?? merged.every((l) => !RUNNING_STATES.has(l.state)),
  };
}

const READ_ONLY_TEXT: Record<string, string> = {
  shadow: "Preview mode, changes are not saved.",
  past_service_day: "This day is in the past. The board is read-only.",
};

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
  const announcer = useAnnouncer();
  const { announce: announceTo } = announcer;
  const announce = useCallback(
    (text: string, assertive = false) =>
      announceTo(text, assertive ? "assertive" : "polite"),
    [announceTo],
  );
  const [helpOpen, setHelpOpen] = useState(false);
  const [drawer, setDrawer] = useState<DrawerTarget | null>(null);
  const [publishDialog, setPublishDialog] = useState<{
    truckIds: string[];
    n: number;
  } | null>(null);
  const [activePublish, setActivePublish] = useState<ActivePublish | null>(
    null,
  );
  const [suggestionsScope, setSuggestionsScope] = useState<{
    truckId?: string;
    suggestionId?: string;
  } | null>(null);
  const [splitMap, setSplitMap] = useState(false);
  const [visibleLanes, setVisibleLanes] = useState<string[]>([]);
  const [positions, setPositions] = useState<Record<string, TruckPosition>>({});
  const [generating, setGenerating] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [singleKey, setSingleKey] = useState(() => readSingleKeyEnabled());
  const rootRef = useRef<HTMLDivElement>(null);
  // Below 1024 px: stacked layout, Sequence forced, trays in a bottom sheet,
  // drawer full screen, Place mode banner pinned (design K14.8, R20.2).
  const stacked = useStackedLayout();
  const [traysOpen, setTraysOpen] = useState(false);
  const shownView = useMemo<BoardView>(
    () =>
      stacked && view.zoom !== "sequence"
        ? { ...view, zoom: "sequence" }
        : view,
    [stacked, view],
  );
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

  // The first snapshot waits for the session user id, and the socket waits
  // for the snapshot, so this user's own early echoes are never shown as
  // another dispatcher's (Phase 4 review).
  const selfUserId = useRef<string | null>(null);
  const selfIdPromise = useRef<Promise<void> | null>(null);
  const selfReady = () => {
    selfIdPromise.current ??= getCurrentUserId()
      .then((id) => {
        selfUserId.current = id;
      })
      .catch(() => undefined);
    return selfIdPromise.current;
  };
  const selfReadyRef = useRef(selfReady);
  selfReadyRef.current = selfReady;

  const load = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      loadAbort.current?.abort();
      const controller = new AbortController();
      loadAbort.current = controller;
      const date = dateRef.current;
      // A foreground load shows the skeleton, not a previous failure.
      if (!background) setFailure(null);
      try {
        const [snap]: [BoardSnapshot, void] = await Promise.all([
          getBoard(date, {
            filters: serverFiltering.current
              ? serverTrayFilters(filtersRef.current)
              : undefined,
            signal: controller.signal,
          }),
          selfReadyRef.current(),
        ]);
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
    // The drawer and dialogs belong to the day they were opened on.
    setDrawer(null);
    setPublishDialog(null);
    setSuggestionsScope(null);
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
  const laneActorsRef = useRef(state.laneActors);
  laneActorsRef.current = state.laneActors;
  // Publish result per lane, once, from the socket or the poll (R12.8):
  // announced (failures assertive) and shown as a toast.
  const announced = useRef<Set<string>>(new Set());
  const reportPublish = (
    publishId: string,
    lanes: { truck_id: string; state: string; last_result: unknown }[],
  ) => {
    for (const l of lanes) {
      const key = `${publishId}:${l.truck_id}:${l.state}`;
      if (announced.current.has(key)) continue;
      const result = l.last_result as { writes_made?: unknown } | null;
      const writes = result?.writes_made as
        | boolean
        | "unknown"
        | null
        | undefined;
      const msg = announcePublishResult(l.truck_id, l.state, writes);
      if (!msg) continue;
      announced.current.add(key);
      announce(msg.text, msg.assertive);
      addToast(msg.text, msg.assertive ? "error" : "success");
    }
  };
  const reportPublishRef = useRef(reportPublish);
  reportPublishRef.current = reportPublish;
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
        // Orders taken off the board come back to the tray via a snapshot.
        if (RETURNS_ORDERS_TO_TRAY.has(e.command_type))
          void loadRef.current({ background: true });
      },
      onLaneStale: (e) => void loadLanes(e.truck_ids),
      onPresence: setPresence,
      onPublishProgress: (e) => {
        reportPublishRef.current(e.publish_id, e.lanes);
        setActivePublish((a) =>
          a && a.publishId === e.publish_id
            ? { ...a, status: mergeStatus(a.status, e.lanes) }
            : a,
        );
        void loadLanes(e.lanes.map((l) => l.truck_id));
      },
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

  // ── Commands: results go to the live regions and toasts (R19, R14.2) ─────
  const onOutcome = useCallback(
    (o: CommandOutcome) => {
      switch (o.kind) {
        case "committed":
          announce(announceCommitted(o.command, o.response.lanes));
          if (RETURNS_ORDERS_TO_TRAY.has(o.command.type))
            void loadRef.current({ background: true });
          break;
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
          const actor = laneActorsRef.current[truck]?.name;
          const text = announceConflict(truck, actor);
          addToast(text, "error");
          announce(text, true);
          break;
        }
        case "blocked": {
          const text = announceBlocked(o.checks);
          addToast(text, "error");
          announce(text);
          break;
        }
        case "undo_stale": {
          const text = announceUndoRefused(
            o.reason,
            o.command.type === "reapply",
          );
          addToast(text, "error");
          announce(text);
          break;
        }
        case "network": {
          const text = announceNotSaved();
          addToast(text, "error");
          announce(text);
          break;
        }
        case "error": {
          const text =
            o.error instanceof Error ? o.error.message : "Not saved.";
          addToast(text, "error");
          announce(text);
          break;
        }
        default:
          break;
      }
    },
    [addToast, announce],
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

  // ── Interactions (trays, lanes, menus, Place mode, drag) ──────────────────
  const ensureVisibleRef = useRef<(truckId: string) => void>(() => {});
  const registerEnsureVisible = useCallback((fn: (truckId: string) => void) => {
    ensureVisibleRef.current = fn;
  }, []);
  const visibleRef = useRef<string[]>([]);
  const splitRef = useRef(splitMap);
  splitRef.current = splitMap;
  const onVisibleChange = useCallback((ids: string[]) => {
    visibleRef.current = ids;
    if (splitRef.current) {
      setVisibleLanes((prev) =>
        prev.length === ids.length && prev.every((t, i) => t === ids[i])
          ? prev
          : ids,
      );
    }
  }, []);
  const readOnlyText =
    READ_ONLY_TEXT[readOnlyReason ?? ""] ?? "The board is read-only.";

  // ── Publish (R12, R13.10) ─────────────────────────────────────────────────
  const startPublish = useCallback((accepted: PublishAccepted) => {
    if (!accepted.publish_id) return;
    setActivePublish({
      publishId: accepted.publish_id,
      status: {
        publish_id: accepted.publish_id,
        lanes: accepted.lanes.map((l) => ({
          truck_id: l.truck_id,
          state: l.state as PublishStatus["lanes"][number]["state"],
          last_result: null,
        })),
        done: accepted.lanes.every((l) => l.state === "already_published"),
      },
    });
  }, []);

  const publishId = activePublish?.publishId ?? null;
  const publishDone = activePublish?.status.done ?? true;
  useEffect(() => {
    if (!publishId || publishDone) return;
    const date = dateRef.current;
    let stopped = false;
    const timer = setInterval(() => {
      void getPublish(date, publishId)
        .then((status) => {
          if (stopped || date !== dateRef.current) return;
          reportPublishRef.current(publishId, status.lanes);
          setActivePublish((a) =>
            a && a.publishId === publishId
              ? {
                  ...a,
                  status: mergeStatus(a.status, status.lanes, status.done),
                }
              : a,
          );
          if (status.done) void loadLanes(status.lanes.map((l) => l.truck_id));
        })
        .catch(() => undefined);
    }, PUBLISH_POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [publishId, publishDone, loadLanes]);

  const openPublish = useCallback((truckIds: string[]) => {
    setPublishDialog((d) => ({ truckIds, n: (d?.n ?? 0) + 1 }));
  }, []);

  const retryPublish = useCallback(
    (truckId: string) => {
      const lane = lanesRef.current[truckId];
      if (!lane) return;
      const group = retryGroup(lane);
      if (lane.state !== "recovering") {
        openPublish(group);
        return;
      }
      // Recovery: resend the whole recorded group; it resumes the attempt (K7.2).
      void publishBoard(dateRef.current, {
        client_request_id: newClientId(),
        lanes: group.map((t) => ({
          truck_id: t,
          expected_version: lanesRef.current[t]?.version ?? 0,
        })),
      })
        .then((accepted) => {
          startPublish(accepted);
          announce(`Retrying the publish on ${trucksText(group)}.`);
        })
        .catch((err) => {
          const text =
            boardErrorCode(err) === "BOARD_LANE_CONFLICT"
              ? `${trucksText(group)} changed. Retry again.`
              : `Retry didn't start on ${trucksText(group)}. Try again.`;
          addToast(text, "error");
          announce(text, true);
          void loadLanes(group);
        });
    },
    [openPublish, startPublish, announce, addToast, loadLanes],
  );

  const publishAllReady = () => {
    const ready = lanesInOrder(state)
      .filter(looksReady)
      .map((l) => l.truck_id);
    if (ready.length === 0) {
      const text = "No trucks are ready to publish.";
      addToast(text, "error");
      announce(text);
      return;
    }
    openPublish(ready);
  };

  // ── Suggestions and Generate plan (R16.6) ─────────────────────────────────
  const generate = async () => {
    setGenerating(true);
    try {
      const res = await generatePlan(getCurrentTenantId());
      const text =
        res.degraded || res.status === "degraded"
          ? "The plan was generated with gaps. Suggestions may be incomplete."
          : "Plan generated. Suggestions appear on the trucks as they arrive.";
      addToast(text, res.degraded ? "error" : "success");
      announce(text);
      void loadRef.current({ background: true });
    } catch {
      const text = "The plan couldn't be generated. Try again.";
      addToast(text, "error");
      announce(text);
    } finally {
      setGenerating(false);
    }
  };

  // ── Live positions and Running late (R15.4) ───────────────────────────────
  const onPositions = useCallback((next: Record<string, TruckPosition>) => {
    setPositions((p) => ({ ...p, ...next }));
  }, []);
  const isToday = serviceDate === today;
  const hasPublished = Object.values(state.lanesById).some(
    (l) => Object.keys(l.publish.plans).length > 0,
  );
  const wantPositions =
    Boolean(snapshot) &&
    isToday &&
    (hasPublished || splitMap || drawer?.tab === "map");
  useEffect(() => {
    if (!wantPositions) return;
    const t = setInterval(() => setNow(Date.now()), LATE_TICK_MS);
    return () => clearInterval(t);
  }, [wantPositions]);
  const lateBy = useCallback(
    (truckId: string) => {
      const lane = lanesRef.current[truckId];
      return lane ? minutesLate(lane, positions[truckId], now) : null;
    },
    [positions, now],
  );
  const terminals = useTerminalIndex(Boolean(drawer) || splitMap);

  const ui: BoardUiActions = {
    openDetails: (target) => setDrawer({ tab: "checks", ...target }),
    openPublish,
    retryPublish,
    openSuggestions: (scope) => setSuggestionsScope(scope ?? {}),
    lateBy,
  };

  const controller = useBoardController({
    state,
    dispatch,
    snapshot:
      snapshot && snapshot.service_date === serviceDate ? snapshot : null,
    commands,
    view: shownView,
    serviceDate,
    timezone: timezone ?? "UTC",
    today,
    readOnly,
    readOnlyText,
    announce,
    toast: (text, kind) => addToast(text, kind),
    refresh: () => void loadRef.current({ background: true }),
    rootRef,
    ensureLaneVisible: (truckId) => ensureVisibleRef.current(truckId),
    visibleLanes: () => visibleRef.current,
    ui,
  });
  const { api } = controller;
  const selectedStops = useMemo(
    () =>
      new Set(
        state.selection
          .filter((k) => k.startsWith("stop:"))
          .map((k) => k.slice(5)),
      ),
    [state.selection],
  );

  const canUndo = !readOnly && state.undoStack.length > 0;
  const canRedo = !readOnly && state.redoStack.length > 0;
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    handleShortcut(e, {
      api,
      singleKey,
      focusSearch: () => document.getElementById("board-search")?.focus(),
      prevDay: () =>
        updateView({ date: clampDate(addDays(serviceDate, -1), today) }),
      nextDay: () =>
        updateView({ date: clampDate(addDays(serviceDate, 1), today) }),
      toggleZoom: () =>
        updateView({
          zoom: view.zoom === "timeline" ? "sequence" : "timeline",
        }),
      openHelp: () => setHelpOpen(true),
      undo: () => void commands.undo(),
      redo: () => void commands.redo(),
      canUndo,
      canRedo,
    });
  };

  // The sheet closes once a card is picked, so the dispatcher can choose a truck.
  const placing = state.placeMode !== null;
  useEffect(() => {
    if (placing || !stacked) setTraysOpen(false);
  }, [placing, stacked]);
  // A full-screen drawer is modal: the board behind it is inert.
  const behind = stacked && drawer !== null;

  const toolbar = (
    <BoardToolbar
      view={shownView}
      zoomLocked={stacked}
      serviceDate={serviceDate}
      today={today}
      zone={timezone ? zoneAbbreviation(timezone) : ""}
      shifts={snapshot?.shifts ?? []}
      products={products}
      priorities={priorities}
      presence={presence}
      canUndo={canUndo}
      canRedo={canRedo}
      onViewChange={updateView}
      onDateChange={(date) => updateView({ date: clampDate(date, today) })}
      onUndo={() => void commands.undo()}
      onRedo={() => void commands.redo()}
      onHelp={() => setHelpOpen(true)}
      suggestionCount={snapshot?.suggestions.length ?? 0}
      onSuggestions={() => setSuggestionsScope({})}
      generateDisabledReason={
        readOnly
          ? "Plans can't be generated while the board is read-only."
          : !isToday
            ? "Plans are generated for today."
            : null
      }
      generating={generating}
      onGenerate={() => void generate()}
      splitMap={splitMap}
      onToggleMap={() => {
        setVisibleLanes(visibleRef.current);
        setSplitMap((on) => !on);
      }}
      publishDisabledReason={
        readOnly
          ? "Nothing can be published while the board is read-only."
          : null
      }
      onPublishAll={publishAllReady}
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
  } else if (!snapshot || !currentDay || !api) {
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
  } else if (stacked) {
    body = (
      <div className="flex min-h-0 flex-1 flex-col">
        <div
          inert={behind}
          className={`flex min-h-0 min-w-0 flex-1 flex-col ${placing ? "pb-16" : ""}`}
        >
          <BoardGrid
            lanes={lanes}
            zone={timezone ? zoneAbbreviation(timezone) : ""}
            focusTruckId={view.truck}
            registerEnsureVisible={registerEnsureVisible}
            onVisibleChange={onVisibleChange}
          />
          {splitMap && (
            <LaneRouteMap
              lanes={lanes.filter((l) => visibleLanes.includes(l.truck_id))}
              selectedOrderIds={selectedStops}
              onSelectStop={(orderId) => api.selectStops([orderId])}
              positions={positions}
              terminals={terminals.coords}
              label="Routes of the trucks on screen"
              className="h-72 shrink-0 border-t border-gray-200"
            />
          )}
          <div className="shrink-0 border-t border-gray-200 bg-white p-2">
            <button
              type="button"
              aria-haspopup="dialog"
              onClick={() => setTraysOpen(true)}
              className="min-h-11 w-full rounded-md border border-gray-300 bg-white px-3 text-sm font-medium text-gray-800 hover:bg-gray-50"
            >
              {`Orders, drivers and trucks (${
                trayOrders(snapshot.trays.orders, state.lanesById).length
              } orders)`}
            </button>
          </div>
        </div>
        <BottomSheet
          isOpen={traysOpen}
          onClose={() => setTraysOpen(false)}
          title="Orders, drivers and trucks"
        >
          <TrayPanel
            variant="sheet"
            plannedCount={stopCount(lanes)}
            onGoToOrders={() => router.push("/dashboard/orders")}
          />
        </BottomSheet>
        {drawer && (
          <DetailDrawer
            fullScreen
            target={drawer}
            onTab={(tab) => setDrawer((d) => (d ? { ...d, tab } : d))}
            onClose={() => setDrawer(null)}
            terminals={terminals}
            positions={positions}
          />
        )}
      </div>
    );
  } else {
    body = (
      <div className="flex min-h-0 flex-1">
        <TrayPanel
          plannedCount={stopCount(lanes)}
          onGoToOrders={() => router.push("/dashboard/orders")}
        />
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <BoardGrid
            lanes={lanes}
            zone={timezone ? zoneAbbreviation(timezone) : ""}
            focusTruckId={view.truck}
            registerEnsureVisible={registerEnsureVisible}
            onVisibleChange={onVisibleChange}
          />
          {splitMap && (
            <LaneRouteMap
              lanes={lanes.filter((l) => visibleLanes.includes(l.truck_id))}
              selectedOrderIds={selectedStops}
              onSelectStop={(orderId) => api.selectStops([orderId])}
              positions={positions}
              terminals={terminals.coords}
              label="Routes of the trucks on screen"
              className="h-72 shrink-0 border-t border-gray-200"
            />
          )}
        </div>
        {drawer && (
          <DetailDrawer
            target={drawer}
            onTab={(tab) => setDrawer((d) => (d ? { ...d, tab } : d))}
            onClose={() => setDrawer(null)}
            terminals={terminals}
            positions={positions}
          />
        )}
      </div>
    );
  }

  const content = (
    <div
      ref={rootRef}
      className="flex h-full min-h-0 flex-col"
      data-density={view.density}
      data-zoom={shownView.zoom}
      data-layout={stacked ? "stacked" : "panels"}
      onKeyDown={onKeyDown}
    >
      <div inert={behind}>
        {toolbar}
        {snapshot && currentDay && (
          <BoardBanners
            readOnlyReason={readOnly ? readOnlyReason : null}
            paused={socket.paused}
            degradedSources={snapshot.degraded_sources}
            truncated={false}
          />
        )}
        {api && currentDay && <PublishBanners lanes={lanes} />}
      </div>
      {api && currentDay && <PlaceModeBanner pinned={stacked} />}
      {body}
      <CardMenu request={controller.menu} onClose={controller.closeMenu} />
      {api && controller.assign && (
        <AssignToMenu
          mode={controller.assign.mode}
          item={controller.assign.item}
          originKey={controller.assign.originKey}
          onClose={controller.closeAssign}
        />
      )}
      {api && currentDay && publishDialog && (
        <PublishDialog
          key={publishDialog.n}
          initialTruckIds={publishDialog.truckIds}
          onClose={() => setPublishDialog(null)}
          onAccepted={startPublish}
          progress={activePublish?.status ?? null}
        />
      )}
      {api && currentDay && suggestionsScope && (
        <SuggestionsDialog
          truckId={suggestionsScope.truckId}
          suggestionId={suggestionsScope.suggestionId}
          onClose={() => setSuggestionsScope(null)}
          notify={(text, kind) => {
            addToast(text, kind);
            announce(text);
          }}
          refresh={() => void loadRef.current({ background: true })}
        />
      )}
      {wantPositions && <LivePositionFeed onPositions={onPositions} />}
      <ShortcutHelpDialog
        isOpen={helpOpen}
        onClose={() => setHelpOpen(false)}
        singleKey={singleKey}
        onSingleKeyChange={(on) => {
          setSingleKey(on);
          writeSingleKeyEnabled(on);
        }}
      />
      <BoardLiveRegion
        polite={announcer.polite}
        assertive={announcer.assertive}
      />
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </div>
  );

  // Always the same tree, so the live regions stay mounted from page load.
  return <BoardContext.Provider value={api}>{content}</BoardContext.Provider>;
}
