/**
 * Dispatch Board client state (design K14.2, K10.5).
 *
 * A pure reducer over server state. Lanes are authoritative only as the
 * server sends them; the reducer never edits lane content itself (optimistic
 * placement lives in `useBoardCommands` through `useOptimistic`). Rules:
 *
 * - **Higher version wins.** A lane payload replaces the held lane when its
 *   `version` is greater than or equal to the held one; an equal version can
 *   carry fresher checks (the server re-validates stale lanes without a
 *   version bump, K5). A lower version is ignored.
 * - **Pending lanes hold events (R15.7).** While a command touching a lane is
 *   pending, incoming payloads for that lane are queued. When the last
 *   pending command on the lane settles, the higher version of the server
 *   answer and the queued payload is applied.
 * - **Refusal restores origin.** A refused command never changed lanes here,
 *   so dropping its pending entry is enough; a conflict's returned lanes are
 *   applied like any other server payload.
 */
import type {
  BoardSnapshot,
  CandidateResult,
  DragItem,
  LaneView,
} from "../../../services/dispatchBoardApi";

/** Max undo/redo depth (R11.1). */
export const UNDO_LIMIT = 50;

/** `"{kind}:{id}"`, e.g. `order:O1`, `stop:O1`, `load:L1`, `driver:D1`. */
export type ItemKey = string;

export interface PendingCommand {
  commandId: string;
  type: string;
  truckIds: string[];
}

export interface UndoEntry {
  commandId: string;
  truckIds: string[];
}

export interface LaneActor {
  userId: string;
  name: string;
  /** `Date.now()` when the event arrived; drives "Ana is editing" (R14.4). */
  at: number;
}

export interface PlaceTarget {
  item: DragItem;
  fromTruckId?: string | null;
}

export interface BoardState {
  /** Last full snapshot (trays, mode, shifts, suggestions). Lanes live in `lanesById`. */
  snapshot: BoardSnapshot | null;
  lanesById: Record<string, LaneView>;
  laneOrder: string[];
  draftVersion: number;
  pending: Record<string, PendingCommand>;
  queuedLaneEvents: Record<string, LaneView>;
  laneActors: Record<string, LaneActor>;
  selection: ItemKey[];
  placeMode: PlaceTarget | null;
  candidateResults: Record<string, CandidateResult>;
  undoStack: UndoEntry[];
  redoStack: UndoEntry[];
}

export type LaneSource = "fetch" | "socket" | "command";

export type BoardAction =
  | { type: "snapshotLoaded"; snapshot: BoardSnapshot }
  | {
      type: "lanesReceived";
      lanes: LaneView[];
      source: LaneSource;
      draftVersion?: number;
      actor?: { user_id: string; name: string | null } | null;
      at?: number;
    }
  | {
      type: "commandStarted";
      commandId: string;
      commandType: string;
      truckIds: string[];
    }
  | {
      type: "commandSettled";
      commandId: string;
      commandType: string;
      lanes: LaneView[];
      draftVersion?: number;
      /** Push onto the undo stack (own committed command). */
      undoable: boolean;
      /** For revert/reapply: the command id they target. */
      targetCommandId?: string;
    }
  | { type: "commandFailed"; commandId: string; lanes?: LaneView[] }
  | { type: "selectionChanged"; keys: ItemKey[] }
  | { type: "placeModeEntered"; target: PlaceTarget }
  | { type: "placeModeExited" }
  | { type: "candidatesReceived"; results: Record<string, CandidateResult> }
  | { type: "candidatesCleared" }
  | { type: "lanesPublished"; truckIds: string[] };

export const initialBoardState: BoardState = {
  snapshot: null,
  lanesById: {},
  laneOrder: [],
  draftVersion: 0,
  pending: {},
  queuedLaneEvents: {},
  laneActors: {},
  selection: [],
  placeMode: null,
  candidateResults: {},
  undoStack: [],
  redoStack: [],
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** True when `next` may replace `current` (higher or equal version wins). */
export function supersedes(
  next: LaneView,
  current: LaneView | undefined,
): boolean {
  return current === undefined || next.version >= current.version;
}

function newer(a: LaneView | undefined, b: LaneView | undefined) {
  if (a === undefined) return b;
  if (b === undefined) return a;
  return b.version > a.version ? b : a;
}

function pendingTrucks(pending: Record<string, PendingCommand>): Set<string> {
  const out = new Set<string>();
  for (const p of Object.values(pending))
    for (const t of p.truckIds) out.add(t);
  return out;
}

function omit<T>(record: Record<string, T>, keys: Iterable<string>) {
  const out = { ...record };
  for (const k of keys) delete out[k];
  return out;
}

function withLane(
  state: BoardState,
  lane: LaneView,
): Pick<BoardState, "lanesById" | "laneOrder"> {
  const current = state.lanesById[lane.truck_id];
  if (!supersedes(lane, current)) {
    return { lanesById: state.lanesById, laneOrder: state.laneOrder };
  }
  return {
    lanesById: { ...state.lanesById, [lane.truck_id]: lane },
    laneOrder: current ? state.laneOrder : [...state.laneOrder, lane.truck_id],
  };
}

function intersects(a: string[], b: Set<string>) {
  return a.some((x) => b.has(x));
}

/**
 * Settles lanes after a command finished: each touched lane gets the newer of
 * the server answer and any queued event, unless another command is still
 * pending on it, in which case the answer joins the queue.
 */
function settleLanes(
  state: BoardState,
  pending: Record<string, PendingCommand>,
  touched: string[],
  answers: LaneView[],
  removed: string[],
): BoardState {
  const stillPending = pendingTrucks(pending);
  let next: BoardState = { ...state, pending };
  const queued = { ...state.queuedLaneEvents };
  const byTruck = new Map(answers.map((l) => [l.truck_id, l]));
  const trucks = new Set([...touched, ...byTruck.keys()]);
  for (const truckId of trucks) {
    const candidate = newer(byTruck.get(truckId), queued[truckId]);
    if (stillPending.has(truckId)) {
      if (candidate) queued[truckId] = candidate;
      continue;
    }
    delete queued[truckId];
    if (candidate) {
      next = { ...next, ...withLane(next, candidate) };
    } else if (removed.includes(truckId) && next.lanesById[truckId]) {
      next = {
        ...next,
        lanesById: omit(next.lanesById, [truckId]),
        laneOrder: next.laneOrder.filter((t) => t !== truckId),
      };
    }
  }
  return { ...next, queuedLaneEvents: queued };
}

function pushBounded(stack: UndoEntry[], entry: UndoEntry): UndoEntry[] {
  const out = [...stack.filter((e) => e.commandId !== entry.commandId), entry];
  return out.length > UNDO_LIMIT ? out.slice(out.length - UNDO_LIMIT) : out;
}

function stacksAfter(
  state: BoardState,
  action: Extract<BoardAction, { type: "commandSettled" }>,
  truckIds: string[],
): Pick<BoardState, "undoStack" | "redoStack"> {
  const { undoStack, redoStack } = state;
  const target = action.targetCommandId;
  if (action.commandType === "revert" && target) {
    const entry = undoStack.find((e) => e.commandId === target);
    if (!entry) return { undoStack, redoStack };
    return {
      undoStack: undoStack.filter((e) => e.commandId !== target),
      redoStack: pushBounded(redoStack, entry),
    };
  }
  if (action.commandType === "reapply" && target) {
    const entry = redoStack.find((e) => e.commandId === target);
    if (!entry) return { undoStack, redoStack };
    return {
      undoStack: pushBounded(undoStack, entry),
      redoStack: redoStack.filter((e) => e.commandId !== target),
    };
  }
  if (!action.undoable) return { undoStack, redoStack };
  // A new command clears the redo stack (K6).
  return {
    undoStack: pushBounded(undoStack, {
      commandId: action.commandId,
      truckIds,
    }),
    redoStack: [],
  };
}

// ─── Reducer ─────────────────────────────────────────────────────────────────

export function boardReducer(
  state: BoardState,
  action: BoardAction,
): BoardState {
  switch (action.type) {
    case "snapshotLoaded": {
      const { snapshot } = action;
      const sameDay = state.snapshot?.service_date === snapshot.service_date;
      const base: BoardState = sameDay
        ? state
        : { ...initialBoardState, selection: [], placeMode: null };
      const busy = pendingTrucks(base.pending);
      const lanesById: Record<string, LaneView> = {};
      const laneOrder: string[] = [];
      const queued = { ...base.queuedLaneEvents };
      for (const lane of snapshot.lanes) {
        const current = base.lanesById[lane.truck_id];
        laneOrder.push(lane.truck_id);
        if (busy.has(lane.truck_id)) {
          queued[lane.truck_id] = newer(queued[lane.truck_id], lane) ?? lane;
          lanesById[lane.truck_id] = current ?? lane;
        } else {
          lanesById[lane.truck_id] = supersedes(lane, current)
            ? lane
            : (current as LaneView);
        }
      }
      // A lane missing from a full snapshot was removed, unless a command on it is pending.
      for (const truckId of base.laneOrder) {
        if (
          !lanesById[truckId] &&
          busy.has(truckId) &&
          base.lanesById[truckId]
        ) {
          lanesById[truckId] = base.lanesById[truckId];
          laneOrder.push(truckId);
        }
      }
      return {
        ...base,
        snapshot,
        lanesById,
        laneOrder,
        queuedLaneEvents: queued,
        draftVersion: Math.max(base.draftVersion, snapshot.draft_version),
      };
    }

    case "lanesReceived": {
      const busy = pendingTrucks(state.pending);
      let next = state;
      const queued = { ...state.queuedLaneEvents };
      for (const lane of action.lanes) {
        if (busy.has(lane.truck_id)) {
          queued[lane.truck_id] = newer(queued[lane.truck_id], lane) ?? lane;
        } else {
          next = { ...next, ...withLane(next, lane) };
        }
      }
      let laneActors = state.laneActors;
      if (action.actor && action.source === "socket") {
        laneActors = { ...laneActors };
        for (const lane of action.lanes) {
          laneActors[lane.truck_id] = {
            userId: action.actor.user_id,
            name: action.actor.name ?? "",
            at: action.at ?? Date.now(),
          };
        }
      }
      return {
        ...next,
        queuedLaneEvents: queued,
        laneActors,
        draftVersion: Math.max(state.draftVersion, action.draftVersion ?? 0),
      };
    }

    case "commandStarted":
      return {
        ...state,
        pending: {
          ...state.pending,
          [action.commandId]: {
            commandId: action.commandId,
            type: action.commandType,
            truckIds: action.truckIds,
          },
        },
      };

    case "commandSettled": {
      const entry = state.pending[action.commandId];
      // Unknown id: the board moved to another day while it was in flight.
      if (!entry) return state;
      const touched = entry.truckIds;
      const pending = omit(state.pending, [action.commandId]);
      const answered = new Set(action.lanes.map((l) => l.truck_id));
      const removed =
        action.commandType === "remove_lane"
          ? touched.filter((t) => !answered.has(t))
          : [];
      const settled = settleLanes(
        state,
        pending,
        touched,
        action.lanes,
        removed,
      );
      return {
        ...settled,
        ...stacksAfter(state, action, touched),
        draftVersion: Math.max(state.draftVersion, action.draftVersion ?? 0),
      };
    }

    case "commandFailed": {
      const entry = state.pending[action.commandId];
      if (!entry) return state;
      const pending = omit(state.pending, [action.commandId]);
      return settleLanes(
        state,
        pending,
        entry.truckIds,
        action.lanes ?? [],
        [],
      );
    }

    case "selectionChanged":
      return { ...state, selection: action.keys };

    case "placeModeEntered":
      return { ...state, placeMode: action.target };

    case "placeModeExited":
      return { ...state, placeMode: null };

    case "candidatesReceived":
      return { ...state, candidateResults: action.results };

    case "candidatesCleared":
      return { ...state, candidateResults: {} };

    case "lanesPublished": {
      // Publish clears undo history for the published lanes (R11.3).
      const published = new Set(action.truckIds);
      return {
        ...state,
        undoStack: state.undoStack.filter(
          (e) => !intersects(e.truckIds, published),
        ),
        redoStack: state.redoStack.filter(
          (e) => !intersects(e.truckIds, published),
        ),
      };
    }

    default:
      return state;
  }
}

// ─── Selectors ───────────────────────────────────────────────────────────────

export function lanesInOrder(state: BoardState): LaneView[] {
  return state.laneOrder
    .map((t) => state.lanesById[t])
    .filter((l): l is LaneView => l !== undefined);
}

export function isLanePending(state: BoardState, truckId: string): boolean {
  return pendingTrucks(state.pending).has(truckId);
}

/** The lane holding an order as a stop or on its shelf. */
export function laneOfOrder(
  state: Pick<BoardState, "lanesById">,
  orderId: string,
): LaneView | undefined {
  return Object.values(state.lanesById).find(
    (lane) =>
      lane.shelf.includes(orderId) ||
      lane.loads.some((load) => load.stops.some((s) => s.order_id === orderId)),
  );
}

/** The lane holding a load. */
export function laneOfLoad(
  state: Pick<BoardState, "lanesById">,
  loadId: string,
): LaneView | undefined {
  return Object.values(state.lanesById).find((lane) =>
    lane.loads.some((load) => load.load_id === loadId),
  );
}

/** The lane a driver is paired to. */
export function laneOfDriver(
  state: Pick<BoardState, "lanesById">,
  driverId: string,
): LaneView | undefined {
  return Object.values(state.lanesById).find((l) => l.driver_id === driverId);
}
