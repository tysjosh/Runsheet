/**
 * Board command senders shared by drag, menu, Place mode and keyboard
 * (design K14.2, K4.5, R8.6).
 *
 * - Each call builds a typed command with a fresh `client_command_id` and the
 *   expected versions of every lane it touches, then sends it inside
 *   `startTransition` with a `useOptimistic` entry, so the moved card can be
 *   drawn at its target with a "Checking…" chip until the server answers.
 *   When the transition ends the optimistic entry is dropped: on success the
 *   reducer already holds the new lanes, on refusal the card is back at its
 *   origin.
 * - Commands are sent one at a time (serialize instead of racing): the next
 *   command reads lane versions after the previous one settled, so two quick
 *   moves on one lane don't conflict with each other.
 * - A network failure or timeout keeps the exact command (same
 *   `client_command_id`, same body) for `retry`, so a retry is idempotent.
 * - `already_applied: true`, and a `BOARD_LANE_CONFLICT` whose returned lanes
 *   already contain the intended result (`isAlreadyApplied`), settle as
 *   success.
 */
import {
  type Dispatch,
  startTransition,
  useCallback,
  useOptimistic,
  useRef,
} from "react";
import { ApiTimeoutError } from "../../../services/api";
import {
  type BoardCommand,
  boardErrorCode,
  boardErrorReason,
  boardErrorStatus,
  type Check,
  type CommandResponse,
  type CommandTarget,
  type CompartmentShare,
  type DragItem,
  type InputModality,
  type LaneView,
  newClientId,
  type ShiftId,
  sendBoardCommand,
} from "../../../services/dispatchBoardApi";
import {
  type BoardAction,
  type BoardState,
  laneOfDriver,
  laneOfLoad,
  laneOfOrder,
} from "./boardReducer";

/** Command body without the three fields this hook fills in. */
type Distribute<T> = T extends unknown
  ? Omit<T, "client_command_id" | "expected_lane_versions" | "input_modality">
  : never;
export type CommandIntent = Distribute<BoardCommand>;

/** What the UI shows while a command is pending (R8.6). */
export interface OptimisticEntry {
  commandId: string;
  type: BoardCommand["type"];
  item: DragItem | null;
  /** Lane the item is drawn on; `null` = back to a tray. */
  toTruckId: string | null;
  fromTruckIds: string[];
  target: CommandTarget | null;
}

export type CommandOutcome =
  | {
      kind: "committed";
      command: BoardCommand;
      response: CommandResponse;
      alreadyApplied: boolean;
    }
  | {
      kind: "conflict";
      command: BoardCommand;
      lanes: LaneView[];
      reason?: string;
    }
  | { kind: "blocked"; command: BoardCommand; checks: Check[]; reason?: string }
  | { kind: "undo_stale"; command: BoardCommand; reason?: string }
  | { kind: "network"; command: BoardCommand; error: unknown }
  | {
      kind: "error";
      command: BoardCommand;
      error: unknown;
      code?: string;
      reason?: string;
    };

export interface UseBoardCommandsOptions {
  serviceDate: string;
  state: BoardState;
  dispatch: Dispatch<BoardAction>;
  send?: typeof sendBoardCommand;
  onOutcome?: (outcome: CommandOutcome) => void;
}

const NOT_UNDOABLE = new Set(["revert", "reapply", "acknowledge_warning"]);

// ─── Pure helpers ────────────────────────────────────────────────────────────

function stopsOf(lane: LaneView | undefined): string[] {
  if (!lane) return [];
  return lane.loads.flatMap((l) => l.stops.map((s) => s.order_id));
}

/** Lanes a command touches (K4.1 "Lanes touched"), from the held state. */
export function touchedLanes(
  state: Pick<BoardState, "lanesById" | "snapshot" | "undoStack" | "redoStack">,
  intent: CommandIntent,
): string[] {
  const out: string[] = [];
  const add = (t: string | undefined | null) => {
    if (t && !out.includes(t)) out.push(t);
  };
  switch (intent.type) {
    case "add_lane":
    case "remove_lane":
    case "discard_lane_changes":
    case "acknowledge_warning":
      add(intent.truck_id);
      break;
    case "pair_driver":
      add(intent.truck_id);
      if (intent.driver_id)
        add(laneOfDriver(state, intent.driver_id)?.truck_id);
      break;
    case "assign_orders":
    case "move_stops":
      for (const id of intent.order_ids) add(laneOfOrder(state, id)?.truck_id);
      add(intent.truck_id);
      break;
    case "unassign_orders":
      for (const id of intent.order_ids) add(laneOfOrder(state, id)?.truck_id);
      break;
    case "move_load":
      add(laneOfLoad(state, intent.load_id)?.truck_id);
      add(intent.truck_id);
      break;
    case "set_terminal":
    case "set_allocation":
    case "set_load_shift":
      add(laneOfLoad(state, intent.load_id)?.truck_id);
      break;
    case "accept_suggestion": {
      const s = state.snapshot?.suggestions.find(
        (x) => x.suggestion_id === intent.suggestion_id,
      );
      add(s?.truck_id);
      for (const load of s?.loads ?? []) {
        add(typeof load.truck_id === "string" ? load.truck_id : undefined);
        const ids = Array.isArray(load.order_ids) ? load.order_ids : [];
        for (const id of ids) {
          if (typeof id === "string") add(laneOfOrder(state, id)?.truck_id);
        }
      }
      break;
    }
    case "revert":
    case "reapply": {
      const entry = [...state.undoStack, ...state.redoStack].find(
        (e) => e.commandId === intent.target_command_id,
      );
      for (const t of entry?.truckIds ?? []) add(t);
      break;
    }
  }
  return out;
}

/**
 * True when a conflict's returned lanes already contain the command's
 * intended result, so the command (or an identical one) already landed
 * (K4.5, covers ids pruned after 24 h). Commands whose result can't be read
 * off the lanes return `false`.
 */
export function isAlreadyApplied(
  command: BoardCommand,
  lanes: LaneView[],
  missingLanes: string[] = [],
): boolean {
  const lane = (t: string) => lanes.find((l) => l.truck_id === t);
  switch (command.type) {
    case "add_lane":
      return lane(command.truck_id) !== undefined;
    case "remove_lane":
      return (
        lane(command.truck_id) === undefined &&
        missingLanes.includes(command.truck_id)
      );
    case "pair_driver": {
      const l = lane(command.truck_id);
      return l !== undefined && l.driver_id === command.driver_id;
    }
    case "assign_orders":
    case "move_stops": {
      const l = lane(command.truck_id);
      if (!l) return false;
      const loadId = command.target.load_id;
      const pool =
        loadId && loadId !== "new"
          ? (l.loads.find((x) => x.load_id === loadId)?.stops ?? []).map(
              (s) => s.order_id,
            )
          : stopsOf(l);
      return command.order_ids.every((id) => pool.includes(id));
    }
    case "unassign_orders":
      return (
        lanes.length > 0 &&
        command.order_ids.every((id) =>
          lanes.every((l) => !stopsOf(l).includes(id) && !l.shelf.includes(id)),
        )
      );
    case "move_load": {
      const l = lane(command.truck_id);
      return l?.loads[command.index]?.load_id === command.load_id;
    }
    case "set_terminal": {
      const load = lanes
        .flatMap((l) => l.loads)
        .find((x) => x.load_id === command.load_id);
      return load !== undefined && load.terminal_id === command.terminal_id;
    }
    case "set_load_shift": {
      const load = lanes
        .flatMap((l) => l.loads)
        .find((x) => x.load_id === command.load_id);
      return load !== undefined && load.shift_id === command.shift_id;
    }
    case "set_allocation": {
      const load = lanes
        .flatMap((l) => l.loads)
        .find((x) => x.load_id === command.load_id);
      if (!load) return false;
      const held = load.allocation_overrides[command.order_id];
      if (command.shares === null) return held === undefined;
      return JSON.stringify(held ?? null) === JSON.stringify(command.shares);
    }
    default:
      return false;
  }
}

function optimisticFor(
  command: BoardCommand,
  touched: string[],
): OptimisticEntry {
  const base = {
    commandId: command.client_command_id,
    type: command.type,
    fromTruckIds: touched,
    target: null,
  };
  switch (command.type) {
    case "assign_orders":
      return {
        ...base,
        item: { kind: "order", ids: command.order_ids },
        toTruckId: command.truck_id,
        target: command.target,
      };
    case "move_stops":
      return {
        ...base,
        item: { kind: "stop", ids: command.order_ids },
        toTruckId: command.truck_id,
        target: command.target,
      };
    case "unassign_orders":
      return {
        ...base,
        item: { kind: "stop", ids: command.order_ids },
        toTruckId: null,
      };
    case "pair_driver":
      return {
        ...base,
        item: command.driver_id
          ? { kind: "driver", ids: [command.driver_id] }
          : null,
        toTruckId: command.truck_id,
      };
    case "move_load":
      return {
        ...base,
        item: { kind: "load", ids: [command.load_id] },
        toTruckId: command.truck_id,
        target: { index: command.index },
      };
    case "add_lane":
      return {
        ...base,
        item: { kind: "truck", ids: [command.truck_id] },
        toTruckId: command.truck_id,
      };
    default:
      return { ...base, item: null, toTruckId: touched[0] ?? null };
  }
}

function isNetworkFailure(err: unknown): boolean {
  return err instanceof ApiTimeoutError || boardErrorStatus(err) === 0;
}

function detailsOf(err: unknown): Record<string, unknown> {
  const d = (err as { details?: unknown } | null)?.details;
  return d && typeof d === "object" && !Array.isArray(d)
    ? (d as Record<string, unknown>)
    : {};
}

// ─── Hook ────────────────────────────────────────────────────────────────────

export function useBoardCommands({
  serviceDate,
  state,
  dispatch,
  send = sendBoardCommand,
  onOutcome,
}: UseBoardCommandsOptions) {
  const [optimistic, addOptimistic] = useOptimistic<
    OptimisticEntry[],
    OptimisticEntry
  >([], (current, entry) => [...current, entry]);

  // Latest state and lane versions, read when a queued command actually sends.
  const stateRef = useRef(state);
  stateRef.current = state;
  const knownVersions = useRef<Record<string, number>>({});
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const failed = useRef<Map<string, BoardCommand>>(new Map());
  const outcomeRef = useRef(onOutcome);
  outcomeRef.current = onOutcome;

  const versionOf = useCallback((truckId: string) => {
    const held = stateRef.current.lanesById[truckId]?.version ?? 0;
    return Math.max(held, knownVersions.current[truckId] ?? 0);
  }, []);

  const remember = useCallback((lanes: LaneView[]) => {
    for (const l of lanes) {
      knownVersions.current[l.truck_id] = Math.max(
        knownVersions.current[l.truck_id] ?? 0,
        l.version,
      );
    }
  }, []);

  /** Sends one built command and settles it in the reducer. */
  const transmit = useCallback(
    async (
      command: BoardCommand,
      touched: string[],
    ): Promise<CommandOutcome> => {
      const id = command.client_command_id;
      dispatch({
        type: "commandStarted",
        commandId: id,
        commandType: command.type,
        truckIds: touched,
      });
      const target =
        command.type === "revert" || command.type === "reapply"
          ? command.target_command_id
          : undefined;
      const settle = (
        lanes: LaneView[],
        draftVersion: number | undefined,
        undoable: boolean,
      ) => {
        remember(lanes);
        startTransition(() => {
          dispatch({
            type: "commandSettled",
            commandId: id,
            commandType: command.type,
            lanes,
            draftVersion,
            undoable,
            targetCommandId: target,
          });
        });
      };
      const refuse = (lanes?: LaneView[]) => {
        if (lanes) remember(lanes);
        startTransition(() => {
          dispatch({ type: "commandFailed", commandId: id, lanes });
        });
      };
      try {
        const response = await send(serviceDate, command);
        failed.current.delete(id);
        settle(
          response.lanes,
          response.draft_version,
          !NOT_UNDOABLE.has(command.type),
        );
        return {
          kind: "committed",
          command,
          response,
          alreadyApplied: response.already_applied === true,
        };
      } catch (err) {
        const code = boardErrorCode(err);
        const reason = boardErrorReason(err);
        const details = detailsOf(err);
        if (isNetworkFailure(err)) {
          failed.current.set(id, command);
          refuse();
          return { kind: "network", command, error: err };
        }
        failed.current.delete(id);
        if (code === "BOARD_LANE_CONFLICT") {
          const lanes = (
            Array.isArray(details.lanes) ? details.lanes : []
          ) as LaneView[];
          const missing = (
            Array.isArray(details.missing_lanes) ? details.missing_lanes : []
          ) as string[];
          if (isAlreadyApplied(command, lanes, missing)) {
            settle(lanes, undefined, false);
            const response: CommandResponse = {
              draft_version: stateRef.current.draftVersion,
              lanes,
              checks: {},
              already_applied: true,
              audit_degraded: false,
            };
            return {
              kind: "committed",
              command,
              response,
              alreadyApplied: true,
            };
          }
          refuse(lanes);
          return { kind: "conflict", command, lanes, reason };
        }
        refuse();
        if (code === "BOARD_COMMAND_BLOCKED") {
          const byLane = (details.checks ?? {}) as Record<string, Check[]>;
          return {
            kind: "blocked",
            command,
            checks: Object.values(byLane).flat(),
            reason,
          };
        }
        if (code === "BOARD_UNDO_STALE") {
          return { kind: "undo_stale", command, reason };
        }
        return { kind: "error", command, error: err, code, reason };
      }
    },
    [dispatch, remember, send, serviceDate],
  );

  /**
   * Queues a command behind earlier ones. With `stampVersions`, the expected
   * lane versions are read when its turn comes (after earlier commands
   * settled); a retry keeps its original body instead.
   */
  const enqueue = useCallback(
    (
      command: BoardCommand,
      touched: string[],
      stampVersions: boolean,
    ): Promise<CommandOutcome> => {
      let resolveOutcome: (o: CommandOutcome) => void = () => {};
      const result = new Promise<CommandOutcome>((resolve) => {
        resolveOutcome = resolve;
      });
      startTransition(async () => {
        addOptimistic(optimisticFor(command, touched));
        const step = queue.current.then(async () => {
          let toSend = command;
          let lanes = touched;
          if (stampVersions) {
            // Earlier commands may have moved the items; re-read where they are.
            lanes = [
              ...new Set([
                ...touched,
                ...touchedLanes(stateRef.current, command),
              ]),
            ];
            // Acknowledgements carry no versions (K4.4).
            if (command.type !== "acknowledge_warning") {
              const versions: Record<string, number> = {};
              for (const t of lanes) versions[t] = versionOf(t);
              toSend = { ...command, expected_lane_versions: versions };
            }
          }
          const outcome = await transmit(toSend, lanes);
          try {
            outcomeRef.current?.(outcome);
          } finally {
            resolveOutcome(outcome);
          }
        });
        queue.current = step.catch(() => undefined);
        await step.catch(() => undefined);
      });
      return result;
    },
    [addOptimistic, transmit, versionOf],
  );

  /** Builds and sends a command from its intent (any modality). */
  const run = useCallback(
    (intent: CommandIntent, modality: InputModality = "menu") => {
      const touched = touchedLanes(stateRef.current, intent);
      const command = {
        ...intent,
        client_command_id: newClientId(),
        expected_lane_versions: {},
        input_modality: modality,
      } as BoardCommand;
      return enqueue(command, touched, true);
    },
    [enqueue],
  );

  /** Re-sends a command that failed on the network, with the same id and body. */
  const retry = useCallback(
    (commandId: string): Promise<CommandOutcome> | null => {
      const command = failed.current.get(commandId);
      if (!command) return null;
      const keys = Object.keys(command.expected_lane_versions);
      const touched = keys.length
        ? keys
        : touchedLanes(stateRef.current, command);
      return enqueue(command, touched, false);
    },
    [enqueue],
  );

  const undo = useCallback(() => {
    const top = stateRef.current.undoStack.at(-1);
    if (!top) return null;
    return run(
      { type: "revert", target_command_id: top.commandId },
      "keyboard",
    );
  }, [run]);

  const redo = useCallback(() => {
    const top = stateRef.current.redoStack.at(-1);
    if (!top) return null;
    return run(
      { type: "reapply", target_command_id: top.commandId },
      "keyboard",
    );
  }, [run]);

  return {
    optimistic,
    run,
    retry,
    hasFailed: (commandId: string) => failed.current.has(commandId),
    assign: (
      orderIds: string[],
      truckId: string,
      target: CommandTarget = {},
      modality?: InputModality,
    ) =>
      run(
        {
          type: "assign_orders",
          order_ids: orderIds,
          truck_id: truckId,
          target,
        },
        modality,
      ),
    move: (
      orderIds: string[],
      truckId: string,
      target: CommandTarget = {},
      modality?: InputModality,
    ) =>
      run(
        { type: "move_stops", order_ids: orderIds, truck_id: truckId, target },
        modality,
      ),
    unassign: (orderIds: string[], modality?: InputModality) =>
      run({ type: "unassign_orders", order_ids: orderIds }, modality),
    pair: (
      truckId: string,
      driverId: string | null,
      modality?: InputModality,
    ) =>
      run(
        { type: "pair_driver", truck_id: truckId, driver_id: driverId },
        modality,
      ),
    moveLoad: (
      loadId: string,
      truckId: string,
      index: number,
      modality?: InputModality,
    ) =>
      run(
        { type: "move_load", load_id: loadId, truck_id: truckId, index },
        modality,
      ),
    setTerminal: (loadId: string, terminalId: string | null) =>
      run({ type: "set_terminal", load_id: loadId, terminal_id: terminalId }),
    setAllocation: (
      loadId: string,
      orderId: string,
      shares: CompartmentShare[] | null,
    ) =>
      run({
        type: "set_allocation",
        load_id: loadId,
        order_id: orderId,
        shares,
      }),
    setLoadShift: (loadId: string, shiftId: ShiftId) =>
      run({ type: "set_load_shift", load_id: loadId, shift_id: shiftId }),
    acknowledge: (truckId: string, warningId: string, reason: string) =>
      run({
        type: "acknowledge_warning",
        truck_id: truckId,
        warning_id: warningId,
        reason,
      }),
    accept: (suggestionId: string, loadIds?: string[]) =>
      run(
        {
          type: "accept_suggestion",
          suggestion_id: suggestionId,
          load_ids: loadIds ?? null,
        },
        "suggestion",
      ),
    addLane: (truckId: string, modality?: InputModality) =>
      run({ type: "add_lane", truck_id: truckId }, modality),
    removeLane: (truckId: string) =>
      run({ type: "remove_lane", truck_id: truckId }),
    discard: (truckId: string) =>
      run({ type: "discard_lane_changes", truck_id: truckId }),
    revert: (commandId: string) =>
      run({ type: "revert", target_command_id: commandId }),
    reapply: (commandId: string) =>
      run({ type: "reapply", target_command_id: commandId }),
    undo,
    redo,
  };
}

export type BoardCommands = ReturnType<typeof useBoardCommands>;
