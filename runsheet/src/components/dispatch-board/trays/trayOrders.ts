/**
 * The order tray is the snapshot's unplanned orders minus any order that is
 * now on a lane (R3.1). Lanes change with every command and socket event,
 * while the snapshot's tray only changes on a reload, so an order placed on a
 * truck must leave the tray as soon as its lane says so.
 */
import type {
  BoardCommandType,
  LaneView,
  TrayOrder,
} from "../../../services/dispatchBoardApi";

/** Ids of every order on a lane (stops and the To reassign shelf). */
export function onLaneOrderIds(lanes: Record<string, LaneView>): Set<string> {
  const ids = new Set<string>();
  for (const lane of Object.values(lanes)) {
    for (const load of lane.loads)
      for (const stop of load.stops) ids.add(stop.order_id);
    for (const id of lane.shelf) ids.add(id);
  }
  return ids;
}

export function trayOrders(
  orders: TrayOrder[],
  lanes: Record<string, LaneView>,
): TrayOrder[] {
  const onLane = onLaneOrderIds(lanes);
  return onLane.size ? orders.filter((o) => !onLane.has(o.order_id)) : orders;
}

/**
 * Commands that can take orders off the board. The tray only learns about
 * them from a snapshot, so the board reloads it in the background after one
 * commits (here or from another dispatcher).
 */
export const RETURNS_ORDERS_TO_TRAY: ReadonlySet<BoardCommandType | string> =
  new Set([
    "unassign_orders",
    "remove_lane",
    "discard_lane_changes",
    "revert",
    "reapply",
  ]);
