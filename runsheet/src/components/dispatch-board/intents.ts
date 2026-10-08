/**
 * One translation from "this item onto that target" to a board command
 * (design K14.4). Drag drops, card menus, Place mode and keyboard shortcuts
 * all call `intentFor`, so they send identical command bodies; only
 * `input_modality` differs (R18.1, SC 2.5.7).
 *
 * Targets use positions as the dispatcher sees them (the slot between two
 * cards, counted with the moved cards still in place). `intentFor` converts
 * them to the engine's index, which counts positions after the moved stops
 * are taken out (`dispatch_board_engine._place`).
 */
import type { LaneView } from "../../services/dispatchBoardApi";
import type { CommandIntent } from "./state/useBoardCommands";

export type ItemKind = "order" | "stop" | "load" | "driver" | "truck";

export interface BoardItem {
  kind: ItemKind;
  ids: string[];
  /** Lane the item is on (stops, loads, paired drivers). */
  fromTruckId?: string | null;
}

export type BoardTarget =
  /** Lane header: best fit for orders/stops, pair for drivers, append for loads. */
  | { target: "lane"; truckId: string }
  /** A slot inside a load; `index` counted with the moved stops in place, `null` = best position in that load. */
  | { target: "load"; truckId: string; loadId: string; index: number | null }
  | { target: "new-load"; truckId: string }
  | { target: "driver-slot"; truckId: string }
  /** Load order inside a lane; `index` counted with the moved load in place. */
  | { target: "load-position"; truckId: string; index: number }
  | { target: "tray" }
  | { target: "shelf"; truckId: string }
  | { target: "new-lane" };

export type TargetKind = BoardTarget["target"];

/** Which target kinds accept which item kinds (the `canDrop` table, K14.3). */
const ACCEPTS: Record<ItemKind, TargetKind[]> = {
  order: ["lane", "load", "new-load"],
  stop: ["lane", "load", "new-load", "tray", "shelf"],
  load: ["lane", "load-position"],
  driver: ["lane", "driver-slot"],
  truck: ["new-lane"],
};

export function accepts(item: Pick<BoardItem, "kind">, target: TargetKind) {
  return ACCEPTS[item.kind].includes(target);
}

/** Engine index for a slot, given which stops are moving. */
export function engineIndex(
  lane: LaneView | undefined,
  loadId: string,
  slot: number,
  movingIds: string[],
): number {
  const stops = lane?.loads.find((l) => l.load_id === loadId)?.stops ?? [];
  const moving = new Set(movingIds);
  let before = 0;
  for (let i = 0; i < Math.min(slot, stops.length); i++) {
    if (moving.has(stops[i].order_id)) before += 1;
  }
  return Math.max(0, slot - before);
}

function loadEngineIndex(
  lane: LaneView | undefined,
  loadId: string,
  slot: number,
): number {
  const pos = lane?.loads.findIndex((l) => l.load_id === loadId) ?? -1;
  return pos >= 0 && pos < slot ? slot - 1 : slot;
}

/**
 * The command for dropping or placing `item` on `target`, or `null` when the
 * target doesn't take that item. `lanes` are the held server lanes.
 */
export function intentFor(
  item: BoardItem,
  target: BoardTarget,
  lanes: Record<string, LaneView>,
): CommandIntent | null {
  if (item.ids.length === 0 || !accepts(item, target.target)) return null;
  const ids = [...item.ids];
  switch (item.kind) {
    case "order":
    case "stop": {
      const type = item.kind === "order" ? "assign_orders" : "move_stops";
      if (target.target === "tray" || target.target === "shelf") {
        return { type: "unassign_orders", order_ids: ids };
      }
      if (target.target === "lane") {
        return { type, order_ids: ids, truck_id: target.truckId, target: {} };
      }
      if (target.target === "new-load") {
        return {
          type,
          order_ids: ids,
          truck_id: target.truckId,
          target: { load_id: "new" },
        };
      }
      if (target.target === "load") {
        const index =
          target.index === null
            ? undefined
            : engineIndex(
                lanes[target.truckId],
                target.loadId,
                target.index,
                item.kind === "stop" ? ids : [],
              );
        return {
          type,
          order_ids: ids,
          truck_id: target.truckId,
          target:
            index === undefined
              ? { load_id: target.loadId }
              : { load_id: target.loadId, index },
        };
      }
      return null;
    }
    case "load": {
      const loadId = ids[0];
      const lane = lanes[getTruck(target)];
      if (target.target === "lane") {
        const count = lane?.loads.length ?? 0;
        const sameLane = lane?.loads.some((l) => l.load_id === loadId);
        return {
          type: "move_load",
          load_id: loadId,
          truck_id: target.truckId,
          index: sameLane ? count - 1 : count,
        };
      }
      if (target.target === "load-position") {
        return {
          type: "move_load",
          load_id: loadId,
          truck_id: target.truckId,
          index: loadEngineIndex(lane, loadId, target.index),
        };
      }
      return null;
    }
    case "driver":
      if (target.target === "lane" || target.target === "driver-slot") {
        return {
          type: "pair_driver",
          truck_id: target.truckId,
          driver_id: ids[0],
        };
      }
      return null;
    case "truck":
      return target.target === "new-lane"
        ? { type: "add_lane", truck_id: ids[0] }
        : null;
    default:
      return null;
  }
}

function getTruck(target: BoardTarget): string {
  return "truckId" in target ? target.truckId : "";
}

/** Where a stop sits: its load and position, or the shelf. */
export function stopPosition(
  lane: LaneView | undefined,
  orderId: string,
): { loadId: string; index: number; count: number } | null {
  for (const load of lane?.loads ?? []) {
    const index = load.stops.findIndex((s) => s.order_id === orderId);
    if (index >= 0) {
      return { loadId: load.load_id, index, count: load.stops.length };
    }
  }
  return null;
}

export type StopMove = "earlier" | "later" | "first" | "last";

/**
 * The slot target for "Move earlier / later / to first / to last stop"
 * (R18.1), as the slot a drag would use; `null` when the stop can't go that way.
 */
export function stopMoveTarget(
  lane: LaneView | undefined,
  orderId: string,
  move: StopMove,
): BoardTarget | null {
  if (!lane) return null;
  const pos = stopPosition(lane, orderId);
  if (!pos) return null;
  const { loadId, index, count } = pos;
  const slot = (n: number): BoardTarget => ({
    target: "load",
    truckId: lane.truck_id,
    loadId,
    index: n,
  });
  switch (move) {
    case "earlier":
      return index > 0 ? slot(index - 1) : null;
    case "first":
      return index > 0 ? slot(0) : null;
    case "later":
      // The slot after the next stop.
      return index < count - 1 ? slot(index + 2) : null;
    case "last":
      return index < count - 1 ? slot(count) : null;
  }
}

/** The load-order target for "Move load earlier / later". */
export function loadMoveTarget(
  lane: LaneView | undefined,
  loadId: string,
  move: "earlier" | "later",
): BoardTarget | null {
  if (!lane) return null;
  const pos = lane.loads.findIndex((l) => l.load_id === loadId);
  if (pos < 0) return null;
  if (move === "earlier") {
    return pos > 0
      ? { target: "load-position", truckId: lane.truck_id, index: pos - 1 }
      : null;
  }
  return pos < lane.loads.length - 1
    ? { target: "load-position", truckId: lane.truck_id, index: pos + 2 }
    : null;
}

/** Order statuses that pin a stop (R13.2). */
export const PINNED_STATUSES = new Set(["in_transit", "delivered", "failed"]);
