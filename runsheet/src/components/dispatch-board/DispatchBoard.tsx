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
  getBoard,
  isAbortError,
  type LaneView,
} from "../../services/dispatchBoardApi";
import { getCurrentUserId } from "../../utils/auth";
import { EmptyState, LoadErrorState, ToastContainer, useToasts } from "../ui";
import { BoardBanners } from "./BoardBanners";
import { BoardContext } from "./BoardContext";
import { BoardLiveRegion, useAnnouncer } from "./BoardLiveRegion";
import { BoardToolbar } from "./BoardToolbar";
import { AssignToMenu } from "./dialogs/AssignToMenu";
import { CardMenu } from "./dialogs/CardMenu";
import {
  readSingleKeyEnabled,
  ShortcutHelpDialog,
  writeSingleKeyEnabled,
} from "./dialogs/ShortcutHelpDialog";
import { BoardGrid } from "./grid/BoardGrid";
import { handleShortcut } from "./keyboard/useBoardShortcuts";
import { PlaceModeBanner } from "./PlaceModeBanner";
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
import { TrayPanel } from "./trays/TrayPanel";
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
  const [singleKey, setSingleKey] = useState(() => readSingleKeyEnabled());
  const rootRef = useRef<HTMLDivElement>(null);

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
  // Own echoed events must not name the current user as "the other dispatcher".
  const announced = useRef<Set<string>>(new Set());
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
      onPublishProgress: (e) => {
        // Publish result per lane, once (R12.8): failures are assertive.
        for (const l of e.lanes) {
          const key = `${e.publish_id}:${l.truck_id}:${l.state}`;
          if (announced.current.has(key)) continue;
          const writes = l.last_result?.writes_made as
            | boolean
            | "unknown"
            | null
            | undefined;
          const msg = announcePublishResult(l.truck_id, l.state, writes);
          if (!msg) continue;
          announced.current.add(key);
          announce(msg.text, msg.assertive);
        }
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
  const onVisibleChange = useCallback((ids: string[]) => {
    visibleRef.current = ids;
  }, []);
  const readOnlyText =
    READ_ONLY_TEXT[readOnlyReason ?? ""] ?? "The board is read-only.";
  const controller = useBoardController({
    state,
    dispatch,
    snapshot:
      snapshot && snapshot.service_date === serviceDate ? snapshot : null,
    commands,
    view,
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
  });
  const { api } = controller;

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
      canUndo={canUndo}
      canRedo={canRedo}
      onViewChange={updateView}
      onDateChange={(date) => updateView({ date: clampDate(date, today) })}
      onUndo={() => void commands.undo()}
      onRedo={() => void commands.redo()}
      onHelp={() => setHelpOpen(true)}
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
  } else {
    body = (
      <div className="flex min-h-0 flex-1">
        <TrayPanel
          plannedCount={stopCount(lanes)}
          onGoToOrders={() => router.push("/dashboard/orders")}
        />
        <BoardGrid
          lanes={lanes}
          zone={timezone ? zoneAbbreviation(timezone) : ""}
          focusTruckId={view.truck}
          registerEnsureVisible={registerEnsureVisible}
          onVisibleChange={onVisibleChange}
        />
      </div>
    );
  }

  const content = (
    <div
      ref={rootRef}
      className="flex h-full min-h-0 flex-col"
      data-density={view.density}
      data-zoom={view.zoom}
      onKeyDown={onKeyDown}
    >
      {toolbar}
      {snapshot && currentDay && (
        <BoardBanners
          readOnlyReason={readOnly ? readOnlyReason : null}
          paused={socket.paused}
          degradedSources={snapshot.degraded_sources}
          truncated={false}
        />
      )}
      {api && currentDay && <PlaceModeBanner />}
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
