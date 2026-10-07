/**
 * Board interaction controller (design K14.2–K14.6, R5, R6, R7, R8.3–R8.6,
 * R18, R19). Builds the `BoardApi` the trays, lanes, menus and dialogs use:
 *
 * - **One command path.** Drag drops, card menus, Place mode and shortcuts
 *   all call `perform(item, target, modality)`, which turns the pair into a
 *   command with `intentFor` and sends it with `useBoardCommands.run`.
 *   Payloads are therefore identical; only `input_modality` differs.
 * - **Drag feedback.** On drag start, one batch `validateBoard` call for the
 *   rendered lanes (R8.3); while hovering an exact slot, a position validate
 *   debounced 150 ms, earlier calls aborted (R8.4). Cleared on drop.
 * - **Focus return (R18.6).** After a command settles, focus moves to the
 *   item in its new place, or back to where it started when refused.
 */
import {
  type Dispatch,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  type BoardSnapshot,
  type CandidateResult,
  type InputModality,
  isAbortError,
  validateBoard,
} from "../../services/dispatchBoardApi";
import { releaseHoldOrder } from "../../services/ordersApi";
import type {
  AssignMode,
  BoardApi,
  MenuRequest,
  PositionResult,
} from "./BoardContext";
import { focusKey } from "./BoardContext";
import type { MatchContext } from "./boardMatch";
import { useBoardMonitor } from "./dnd/adapter";
import {
  type BoardItem,
  type BoardTarget,
  engineIndex,
  intentFor,
} from "./intents";
import {
  type BoardAction,
  type BoardState,
  laneOfLoad,
  laneOfOrder,
} from "./state/boardReducer";
import type {
  BoardCommands,
  CommandIntent,
  CommandOutcome,
} from "./state/useBoardCommands";
import type { BoardView } from "./viewState";

export const POSITION_DEBOUNCE_MS = 150;
/** "Ana is editing" lasts 30 s after their last change (R14.4). */
const EDITING_MS = 30_000;
const FOCUS_WAIT_MS = 3_000;

export interface AssignRequest {
  mode: AssignMode;
  item: BoardItem;
  originKey: string | null;
}

export interface ControllerOptions {
  state: BoardState;
  dispatch: Dispatch<BoardAction>;
  snapshot: BoardSnapshot | null;
  commands: BoardCommands;
  view: BoardView;
  serviceDate: string;
  timezone: string;
  today: string;
  readOnly: boolean;
  readOnlyText: string;
  announce: (text: string, assertive?: boolean) => void;
  toast: (text: string, kind: "success" | "error") => void;
  refresh: () => void;
  rootRef: React.RefObject<HTMLElement | null>;
  /** Scrolls the windowed grid so a lane is rendered. */
  ensureLaneVisible: (truckId: string) => void;
  /** Lanes currently rendered by the grid (batch validate candidates). */
  visibleLanes: () => string[];
  validate?: typeof validateBoard;
  releaseHold?: typeof releaseHoldOrder;
}

function keyOf(kind: BoardItem["kind"], id: string) {
  return `${kind}:${id}`;
}

/** Where focus goes after a command commits. */
export function focusAfter(intent: CommandIntent): string[] {
  switch (intent.type) {
    case "assign_orders":
    case "move_stops":
      return [focusKey.stop(intent.order_ids[0])];
    case "unassign_orders":
      return [
        focusKey.stop(intent.order_ids[0]),
        focusKey.order(intent.order_ids[0]),
      ];
    case "pair_driver":
      return [focusKey.driverSlot(intent.truck_id)];
    case "move_load":
      return [focusKey.load(intent.load_id)];
    case "add_lane":
      return [focusKey.lane(intent.truck_id)];
    default:
      return [];
  }
}

/** Focus key of the card an item was taken from. */
export function originOf(item: BoardItem): string | null {
  const id = item.ids[0];
  if (!id) return null;
  switch (item.kind) {
    case "order":
      return focusKey.order(id);
    case "stop":
      return focusKey.stop(id);
    case "load":
      return focusKey.load(id);
    case "driver":
      return focusKey.driver(id);
    case "truck":
      return focusKey.truck(id);
  }
}

export function useBoardController(opts: ControllerOptions) {
  const {
    state,
    dispatch,
    snapshot,
    commands,
    view,
    serviceDate,
    timezone,
    today,
    readOnly,
    readOnlyText,
    announce,
    rootRef,
    ensureLaneVisible,
    validate = validateBoard,
    releaseHold: release = releaseHoldOrder,
  } = opts;
  const stateRef = useRef(state);
  stateRef.current = state;
  const latest = useRef(opts);
  latest.current = opts;

  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [menu, setMenu] = useState<MenuRequest | null>(null);
  const [assign, setAssign] = useState<AssignRequest | null>(null);
  const [dragItem, setDragItem] = useState<BoardItem | null>(null);
  const [position, setPosition] = useState<PositionResult | null>(null);

  // ── Focus return (R18.6) ──────────────────────────────────────────────────
  const pendingFocus = useRef<{ keys: string[]; at: number } | null>(null);
  const [focusTick, setFocusTick] = useState(0);
  const requestFocus = useCallback((keys: string[]) => {
    const wanted = keys.filter(Boolean);
    if (wanted.length === 0) return;
    pendingFocus.current = { keys: wanted, at: Date.now() };
    setFocusTick((n) => n + 1);
  }, []);
  useEffect(() => {
    const pending = pendingFocus.current;
    if (!pending) return;
    if (Date.now() - pending.at > FOCUS_WAIT_MS) {
      pendingFocus.current = null;
      return;
    }
    const root = rootRef.current ?? document;
    for (const key of pending.keys) {
      const el = root.querySelector<HTMLElement>(
        `[data-focus-key="${key.replace(/["\\]/g, "\\$&")}"]`,
      );
      if (el) {
        pendingFocus.current = null;
        el.focus();
        return;
      }
    }
    // Not rendered yet: the lane may be outside the windowed range.
    const first = pending.keys[0];
    const cut = first.indexOf(":");
    const kind = first.slice(0, cut);
    const id = first.slice(cut + 1);
    const s = stateRef.current;
    const truck =
      kind === "stop"
        ? laneOfOrder(s, id)?.truck_id
        : kind === "load"
          ? laneOfLoad(s, id)?.truck_id
          : kind === "lane" || kind === "driver-slot"
            ? id
            : undefined;
    if (truck) ensureLaneVisible(truck);
  });

  // ── Selection and Place mode ──────────────────────────────────────────────
  const selection = state.selection;
  const isSelected = useCallback(
    (kind: BoardItem["kind"], id: string) =>
      selection.includes(keyOf(kind, id)),
    [selection],
  );

  const clearSelection = useCallback(() => {
    dispatch({ type: "selectionChanged", keys: [] });
    dispatch({ type: "placeModeExited" });
  }, [dispatch]);

  const select = useCallback(
    (item: BoardItem, additive = false) => {
      const s = stateRef.current;
      const keys = item.ids.map((id) => keyOf(item.kind, id));
      const sameKind = s.selection.every((k) => k.startsWith(`${item.kind}:`));
      let next: string[];
      if (additive && sameKind) {
        const set = new Set(s.selection);
        for (const k of keys) {
          if (set.has(k)) set.delete(k);
          else set.add(k);
        }
        next = [...set];
      } else {
        next = keys;
      }
      dispatch({ type: "selectionChanged", keys: next });
      if (next.length === 0) {
        dispatch({ type: "placeModeExited" });
        return;
      }
      const ids = next.map((k) => k.slice(item.kind.length + 1));
      if (!latest.current.readOnly) {
        dispatch({
          type: "placeModeEntered",
          target: {
            item: { kind: item.kind, ids },
            fromTruckId: item.fromTruckId ?? null,
          },
        });
        const what =
          ids.length === 1
            ? itemName(item.kind, ids[0])
            : `${ids.length} ${plural(item.kind)}`;
        latest.current.announce(
          `${what} selected. Choose a ${item.kind === "driver" ? "driver slot" : item.kind === "truck" ? "place on the board" : "truck"}, or press Escape.`,
        );
      }
    },
    [dispatch],
  );

  /** The selection when the card is part of it, else just the card. */
  const itemFor = useCallback((card: BoardItem): BoardItem => {
    const s = stateRef.current;
    const prefix = `${card.kind}:`;
    const selected = s.selection
      .filter((k) => k.startsWith(prefix))
      .map((k) => k.slice(prefix.length));
    if (card.ids.every((id) => selected.includes(id)) && selected.length > 0) {
      return { ...card, ids: selected };
    }
    return card;
  }, []);

  const placeItem: BoardItem | null = state.placeMode
    ? {
        ...state.placeMode.item,
        fromTruckId: state.placeMode.fromTruckId ?? null,
      }
    : null;

  // ── The one command path ──────────────────────────────────────────────────
  const perform = useCallback(
    (
      item: BoardItem,
      target: BoardTarget,
      modality: InputModality,
      originKey?: string | null,
    ) => {
      const o = latest.current;
      if (o.readOnly) {
        o.announce(o.readOnlyText);
        return;
      }
      const intent = intentFor(item, target, stateRef.current.lanesById);
      if (!intent) return;
      const origin = originKey ?? originOf(item);
      dispatch({ type: "selectionChanged", keys: [] });
      dispatch({ type: "placeModeExited" });
      void o.commands.run(intent, modality).then((outcome: CommandOutcome) => {
        requestFocus(
          outcome.kind === "committed"
            ? [...focusAfter(intent), ...(origin ? [origin] : [])]
            : origin
              ? [origin]
              : [],
        );
      });
    },
    [dispatch, requestFocus],
  );

  const placeOn = useCallback(
    (target: BoardTarget, originKey?: string | null) => {
      const s = stateRef.current;
      if (!s.placeMode) return;
      perform(
        { ...s.placeMode.item, fromTruckId: s.placeMode.fromTruckId },
        target,
        "place",
        originKey,
      );
    },
    [perform],
  );

  /** Commands with no drag twin (or a button twin): same run path, own intent. */
  const runIntent = useCallback(
    (intent: CommandIntent, modality: InputModality, origin: string | null) => {
      const o = latest.current;
      if (o.readOnly) {
        o.announce(o.readOnlyText);
        return;
      }
      void o.commands.run(intent, modality).then((outcome) => {
        requestFocus(
          outcome.kind === "committed"
            ? [...focusAfter(intent), ...(origin ? [origin] : [])]
            : origin
              ? [origin]
              : [],
        );
      });
    },
    [requestFocus],
  );

  const unpair = useCallback(
    (truckId: string) =>
      runIntent(
        { type: "pair_driver", truck_id: truckId, driver_id: null },
        "menu",
        focusKey.driverSlot(truckId),
      ),
    [runIntent],
  );

  const removeLane = useCallback(
    (truckId: string) =>
      runIntent({ type: "remove_lane", truck_id: truckId }, "menu", null),
    [runIntent],
  );

  /** The truck tray's "Add lane" button: the twin of dropping the truck on the grid. */
  const addLane = useCallback(
    (truckId: string, modality: InputModality) =>
      perform(
        { kind: "truck", ids: [truckId] },
        { target: "new-lane" },
        modality,
      ),
    [perform],
  );

  const showMenu = useCallback((request: MenuRequest) => setMenu(request), []);
  const closeMenu = useCallback(() => setMenu(null), []);

  const openAssign = useCallback(
    (mode: AssignMode, item: BoardItem, originKey?: string | null) => {
      setMenu(null);
      setAssign({ mode, item, originKey: originKey ?? originOf(item) });
    },
    [],
  );
  const closeAssign = useCallback(() => {
    setAssign((current) => {
      if (current?.originKey) requestFocus([current.originKey]);
      return null;
    });
  }, [requestFocus]);

  const releaseHold = useCallback(
    (orderId: string) => {
      const o = latest.current;
      if (o.readOnly) {
        o.announce(o.readOnlyText);
        return;
      }
      void release(orderId)
        .then((order) => {
          const text =
            order?.status === "on_hold"
              ? `Order ${orderId} is still on hold.`
              : `Order ${orderId} released from hold.`;
          o.toast(text, order?.status === "on_hold" ? "error" : "success");
          o.announce(text);
          o.refresh();
          requestFocus([focusKey.order(orderId)]);
        })
        .catch(() => {
          const text = `Couldn't release Order ${orderId} from hold.`;
          o.toast(text, "error");
          o.announce(text);
        });
    },
    [release, requestFocus],
  );

  // ── Drag feedback (R8.3, R8.4) ────────────────────────────────────────────
  const batchAbort = useRef<AbortController | null>(null);
  const positionAbort = useRef<AbortController | null>(null);
  const positionTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const candidatesRef = useRef<Record<string, CandidateResult>>({});
  candidatesRef.current = state.candidateResults;

  const stopFeedback = useCallback(() => {
    batchAbort.current?.abort();
    positionAbort.current?.abort();
    if (positionTimer.current) clearTimeout(positionTimer.current);
    positionTimer.current = null;
    setPosition(null);
    dispatch({ type: "candidatesCleared" });
  }, [dispatch]);

  useEffect(() => () => stopFeedback(), [stopFeedback]);

  const onDragStart = useCallback(
    (item: BoardItem) => {
      setDragItem(item);
      const o = latest.current;
      if (o.readOnly || item.kind === "truck") return;
      const candidates = o.visibleLanes().slice(0, 60);
      if (candidates.length === 0) return;
      batchAbort.current?.abort();
      const controller = new AbortController();
      batchAbort.current = controller;
      void validate(
        o.serviceDate,
        { item: { kind: item.kind, ids: item.ids }, candidates },
        controller.signal,
      )
        .then((res) => {
          if (controller.signal.aborted) return;
          dispatch({ type: "candidatesReceived", results: res.results });
        })
        .catch((err) => {
          if (!isAbortError(err)) {
            // Feedback only; the drop is still validated by the server.
          }
        });
    },
    [dispatch, validate],
  );

  const onTargetChange = useCallback(
    (item: BoardItem, target: BoardTarget | null) => {
      if (positionTimer.current) clearTimeout(positionTimer.current);
      positionAbort.current?.abort();
      if (
        !target ||
        target.target !== "load" ||
        target.index === null ||
        latest.current.readOnly
      ) {
        setPosition(null);
        return;
      }
      const lane = stateRef.current.lanesById[target.truckId];
      const index = engineIndex(
        lane,
        target.loadId,
        target.index,
        item.kind === "stop" ? item.ids : [],
      );
      setPosition({
        truckId: target.truckId,
        loadId: target.loadId,
        index: target.index,
        result: null,
      });
      positionTimer.current = setTimeout(() => {
        const controller = new AbortController();
        positionAbort.current = controller;
        void validate(
          latest.current.serviceDate,
          {
            item: { kind: item.kind, ids: item.ids },
            candidates: [target.truckId],
            position: { load_id: target.loadId, index },
          },
          controller.signal,
        )
          .then((res) => {
            if (controller.signal.aborted) return;
            const result = res.results[target.truckId] ?? null;
            setPosition({
              truckId: target.truckId,
              loadId: target.loadId,
              index: target.index as number,
              result,
            });
            if (result) {
              dispatch({
                type: "candidatesReceived",
                results: {
                  ...candidatesRef.current,
                  [target.truckId]: result,
                },
              });
            }
          })
          .catch(() => undefined);
      }, POSITION_DEBOUNCE_MS);
    },
    [dispatch, validate],
  );

  const onDrop = useCallback(
    (item: BoardItem, target: BoardTarget | null) => {
      setDragItem(null);
      stopFeedback();
      if (target) perform(item, target, "drag");
    },
    [perform, stopFeedback],
  );

  useBoardMonitor({ onDragStart, onTargetChange, onDrop });

  // ── Derived ───────────────────────────────────────────────────────────────
  const match: MatchContext = useMemo(
    () => ({
      filters: view.filters,
      search: view.search,
      serviceDate,
      timeZone: timezone,
    }),
    [view.filters, view.search, serviceDate, timezone],
  );

  const laneEditor = useCallback(
    (truckId: string) => {
      const actor = state.laneActors[truckId];
      if (!actor || Date.now() - actor.at > EDITING_MS) return null;
      return actor.name || null;
    },
    [state.laneActors],
  );

  const toggleCollapsed = useCallback((truckId: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(truckId)) next.delete(truckId);
      else next.add(truckId);
      return next;
    });
  }, []);

  const api: BoardApi | null = snapshot
    ? {
        state,
        snapshot,
        lanesById: state.lanesById,
        view,
        serviceDate,
        timezone,
        isToday: serviceDate === today,
        readOnly,
        readOnlyText,
        optimistic: commands.optimistic,
        match,
        collapsed,
        toggleCollapsed,
        isSelected,
        select,
        clearSelection,
        placeItem: readOnly ? null : placeItem,
        itemFor,
        perform,
        placeOn,
        unpair,
        removeLane,
        addLane,
        showMenu,
        openAssign,
        releaseHold,
        requestFocus,
        announce,
        dragItem,
        candidate: (truckId) => state.candidateResults[truckId],
        position,
        laneEditor,
      }
    : null;

  return {
    api,
    menu,
    closeMenu,
    assign,
    closeAssign,
    focusTick,
  };
}

function plural(kind: BoardItem["kind"]): string {
  return kind === "order" ? "orders" : `${kind}s`;
}

function itemName(kind: BoardItem["kind"], id: string): string {
  switch (kind) {
    case "order":
    case "stop":
      return `Order ${id}`;
    case "load":
      return "Load";
    case "driver":
      return `Driver ${id}`;
    case "truck":
      return `Truck ${id}`;
  }
}
