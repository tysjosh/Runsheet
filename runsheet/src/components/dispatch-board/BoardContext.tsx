/**
 * What trays, lanes, menus and dialogs share (design K14.1–K14.5): the held
 * state, view facts, selection / Place mode, and the action functions built
 * by `useBoardController`. One context keeps the prop chains short; every
 * action goes through the same `perform` → `intentFor` → `useBoardCommands`
 * path, whatever the input modality.
 */
import { createContext, useContext } from "react";
import type {
  BoardSnapshot,
  CandidateResult,
  InputModality,
  LaneView,
} from "../../services/dispatchBoardApi";
import type { MatchContext } from "./boardMatch";
import type { BoardItem, BoardTarget } from "./intents";
import type { BoardState } from "./state/boardReducer";
import type {
  CommandIntent,
  CommandOutcome,
  OptimisticEntry,
} from "./state/useBoardCommands";
import type { BoardView } from "./viewState";

export interface MenuItem {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  /** Shown next to a disabled item and read with it. */
  reason?: string;
}

export interface MenuRequest {
  label: string;
  items: MenuItem[];
  anchor: HTMLElement | null;
}

export type AssignMode = "assign" | "move" | "pair" | "load";

export interface PositionResult {
  truckId: string;
  loadId: string;
  index: number;
  result: CandidateResult | null;
}

export type DrawerTab = "checks" | "compartments" | "map" | "history";

/** What the detail drawer shows: a lane, one of its loads, or one stop (R8.7). */
export interface DrawerTarget {
  truckId: string;
  loadId?: string | null;
  orderId?: string | null;
  tab?: DrawerTab;
}

/** Board-level actions owned by `DispatchBoard` (Phase 6: drawer, publish, suggestions, map). */
export interface BoardUiActions {
  openDetails: (target: DrawerTarget) => void;
  /** Opens the Publish review for these lanes (R12.1, R12.2). */
  openPublish: (truckIds: string[]) => void;
  /** Failed lane: review again; Recovering lane: resend the whole group (R12.6, R13.10). */
  retryPublish: (truckId: string) => void;
  openSuggestions: (scope?: {
    truckId?: string;
    suggestionId?: string;
  }) => void;
  /** Minutes behind the route ETA beyond the threshold, else `null` (R15.4). */
  lateBy: (truckId: string) => number | null;
}

export interface BoardApi extends BoardUiActions {
  state: BoardState;
  snapshot: BoardSnapshot;
  lanesById: Record<string, LaneView>;
  view: BoardView;
  serviceDate: string;
  timezone: string;
  isToday: boolean;
  readOnly: boolean;
  /** Why the board can't change ("Preview mode, changes are not saved."). */
  readOnlyText: string;
  optimistic: OptimisticEntry[];
  match: MatchContext;
  collapsed: Set<string>;
  toggleCollapsed: (truckId: string) => void;

  // Selection and Place mode (R18.3, R5.7)
  isSelected: (kind: BoardItem["kind"], id: string) => boolean;
  /** Click / Enter: select (and enter Place mode). `additive`: Shift/Cmd-click or Space. */
  select: (item: BoardItem, additive?: boolean) => void;
  clearSelection: () => void;
  placeItem: BoardItem | null;
  /** The selection as one item (same kind), with the given card as fallback. */
  itemFor: (card: BoardItem) => BoardItem;

  // Actions
  perform: (
    item: BoardItem,
    target: BoardTarget,
    modality: InputModality,
    originKey?: string | null,
  ) => void;
  placeOn: (target: BoardTarget, originKey?: string | null) => void;
  unpair: (truckId: string) => void;
  removeLane: (truckId: string) => void;
  addLane: (truckId: string, modality: InputModality) => void;
  showMenu: (request: MenuRequest) => void;
  openAssign: (
    mode: AssignMode,
    item: BoardItem,
    originKey?: string | null,
  ) => void;
  releaseHold: (orderId: string) => void;
  /** Any command intent through the shared path; `null` when the board is read-only. */
  command: (
    intent: CommandIntent,
    modality?: InputModality,
  ) => Promise<CommandOutcome | null>;
  /** "Discard changes" on a Modified lane (R10.4). */
  discard: (truckId: string) => void;
  /** A lane being published or in recovery takes no edits (K14.7). */
  laneLocked: (truckId: string) => boolean;
  /** Board selection without Place mode (map ↔ board sync, R17.2). */
  selectStops: (orderIds: string[]) => void;
  requestFocus: (keys: string[]) => void;
  announce: (text: string, assertive?: boolean) => void;

  // Drag feedback (R8.3, R8.4)
  dragItem: BoardItem | null;
  candidate: (truckId: string) => CandidateResult | undefined;
  position: PositionResult | null;
  laneEditor: (truckId: string) => string | null;
}

export const BoardContext = createContext<BoardApi | null>(null);

export function useBoard(): BoardApi {
  const api = useContext(BoardContext);
  if (!api) throw new Error("useBoard outside the Dispatch Board");
  return api;
}

/** `data-focus-key` values (focus return, R18.6). */
export const focusKey = {
  order: (id: string) => `order:${id}`,
  stop: (id: string) => `stop:${id}`,
  load: (id: string) => `load:${id}`,
  driver: (id: string) => `driver:${id}`,
  truck: (id: string) => `truck:${id}`,
  lane: (truckId: string) => `lane:${truckId}`,
  driverSlot: (truckId: string) => `driver-slot:${truckId}`,
};
