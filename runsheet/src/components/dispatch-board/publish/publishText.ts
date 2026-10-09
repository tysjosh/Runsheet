/**
 * Publish wording (R12.2, R12.3, R12.6, R13.7, R13.10): not-ready reasons,
 * load classes, load info and failure results. Fixed templates only; reason
 * codes come from the server, messages never from exception text.
 */
import type {
  LaneView,
  PublishResult,
} from "../../../services/dispatchBoardApi";

const NOT_READY_TEXT: Record<string, string> = {
  no_driver: "No driver is paired.",
  no_loads: "There are no loads to publish.",
  unplaced_dispatched_order:
    "Orders on the To reassign shelf need a load, or cancel them.",
  warning_unacknowledged: "Each open warning needs a reason.",
  publishing: "This truck is already publishing.",
  recovery_pending: "A publish is recovering on this truck. Use Retry.",
  retry_whole_group: "Retry must include every truck of the publish group.",
  version_changed: "This truck changed after you opened the review.",
  lane_not_found: "This truck is no longer on the board.",
  check_unavailable: "Order data is unavailable, try again shortly.",
  order_identity_changed:
    "An order's product, customer or tank changed. Remove and re-add it.",
  override_stale: "Reset or update the compartment split.",
};

/** Not-ready reason text; block reason codes use the lane's check message. */
export function notReadyText(reason: string, lane?: LaneView): string {
  const fixed = NOT_READY_TEXT[reason];
  if (fixed) return fixed;
  const check = lane?.checks.find(
    (c) => c.reason_code === reason && c.outcome === "block",
  );
  if (check) {
    return check.message.endsWith(".") ? check.message : `${check.message}.`;
  }
  return `Blocked: ${reason.replace(/_/g, " ")}.`;
}

export const LOAD_CLASS_TEXT: Record<string, string> = {
  unchanged: "No change",
  new: "New load",
  new_revision: "Re-issued to the driver",
  amend: "Updated in place (already started)",
  removed: "Removed",
};

export const LOAD_INFO_TEXT: Record<string, string> = {
  driver_may_be_loading:
    "The driver may already be loading at the terminal. Call before re-publishing.",
};

const STAGE_TEXT: Record<string, string> = {
  preflight: "while checking",
  claim: "while starting",
  apply: "while assigning orders",
  dispatch: "while sending to the driver",
  retire: "while retiring the old route",
  stage_relink: "while moving orders",
  amend: "while updating the started route",
  notify: "while notifying drivers",
  finalize: "while finishing",
};

/** "Publish failed on Truck T1 while assigning orders. Nothing was changed." */
export function failureText(
  truckIds: string[],
  r: PublishResult | null,
): string {
  const trucks = trucksText(truckIds);
  const stage = r?.stage ? STAGE_TEXT[r.stage] : undefined;
  const writes =
    r?.writes_made === false
      ? " Nothing was changed."
      : r?.writes_made === true
        ? " Some changes were saved. Retry finishes them."
        : " Some changes may have been saved. Retry finishes them.";
  return `Publish failed on ${trucks}${stage ? ` ${stage}` : ""}.${writes}`;
}

export function trucksText(truckIds: string[]): string {
  if (truckIds.length === 1) return `Truck ${truckIds[0]}`;
  return `Trucks ${truckIds.slice(0, -1).join(", ")} and ${truckIds.at(-1)}`;
}

/** Lanes a Retry sends: the whole recorded group, else the lane (K7.2, E30). */
export function retryGroup(lane: LaneView): string[] {
  const group =
    lane.publish.attempt?.group_truck_ids ??
    lane.publish.last_result?.group_truck_ids ??
    [];
  return group.length > 0 ? [...group] : [lane.truck_id];
}

/** Lane states the Publish review can take (Published and in-flight lanes can't). */
export const PUBLISHABLE_STATES = new Set(["draft", "modified", "failed"]);

/** Client-side guess for "Publish all ready"; the dry run decides (R12.3). */
export function looksReady(lane: LaneView): boolean {
  return (
    PUBLISHABLE_STATES.has(lane.state) &&
    Boolean(lane.driver_id) &&
    lane.loads.some((l) => l.stops.length > 0) &&
    lane.outcome !== "block" &&
    lane.shelf.length === 0
  );
}
