/**
 * Menu contents for each card kind (R18.1). Every action is the menu twin of
 * a drag: "Move earlier" sends what dropping the stop one slot to the left
 * sends, because both go through `perform` with a `BoardTarget`.
 */
import type { LaneView } from "../../services/dispatchBoardApi";
import type { BoardApi, MenuItem } from "./BoardContext";
import {
  type BoardItem,
  loadMoveTarget,
  PINNED_STATUSES,
  type StopMove,
  stopMoveTarget,
} from "./intents";

export const REMOVE_LANE_REASON = "Move or reassign this truck's stops first";
const PINNED_REASON = "Started stops can't move";

function readOnlyItems(api: BoardApi, items: MenuItem[]): MenuItem[] {
  if (!api.readOnly) return items;
  return items.map((i) => ({
    ...i,
    disabled: true,
    reason: i.reason ?? api.readOnlyText,
  }));
}

export function orderMenu(
  api: BoardApi,
  orderId: string,
  onHold: boolean,
): MenuItem[] {
  const item = api.itemFor({ kind: "order", ids: [orderId] });
  const items: MenuItem[] = [
    {
      label:
        item.ids.length > 1
          ? `Assign ${item.ids.length} orders to truck…`
          : "Assign to truck…",
      onSelect: () => api.openAssign("assign", item),
      disabled: onHold,
      reason: onHold ? "Release the hold first" : undefined,
    },
  ];
  if (onHold) {
    items.push({
      label: "Release hold",
      onSelect: () => api.releaseHold(orderId),
    });
  }
  return readOnlyItems(api, items);
}

export function stopMenu(
  api: BoardApi,
  lane: LaneView,
  orderId: string,
  onShelf = false,
): MenuItem[] {
  const stop = lane.loads
    .flatMap((l) => l.stops)
    .find((s) => s.order_id === orderId);
  const pinned = PINNED_STATUSES.has(stop?.snapshot.status ?? "");
  const item = api.itemFor({
    kind: "stop",
    ids: [orderId],
    fromTruckId: lane.truck_id,
  });
  const move = (label: string, m: StopMove): MenuItem => {
    const target = stopMoveTarget(lane, orderId, m);
    return {
      label,
      onSelect: () => target && api.perform(item, target, "menu"),
      disabled: pinned || !target,
      reason: pinned ? PINNED_REASON : !target ? "Already there" : undefined,
    };
  };
  const items: MenuItem[] = [
    {
      label: "Move to…",
      onSelect: () => api.openAssign("move", item),
      disabled: pinned,
      reason: pinned ? PINNED_REASON : undefined,
    },
  ];
  if (!onShelf) {
    items.push(
      move("Move earlier", "earlier"),
      move("Move later", "later"),
      move("Move to first stop", "first"),
      move("Move to last stop", "last"),
      {
        label: "Unassign",
        onSelect: () => api.perform(item, { target: "tray" }, "menu"),
        disabled: pinned,
        reason: pinned ? PINNED_REASON : undefined,
      },
    );
  }
  const load = lane.loads.find((l) =>
    l.stops.some((s) => s.order_id === orderId),
  );
  return [
    ...readOnlyItems(api, items),
    detailsItem(api, {
      truckId: lane.truck_id,
      loadId: load?.load_id ?? null,
      orderId,
    }),
  ];
}

export function loadMenu(
  api: BoardApi,
  lane: LaneView,
  loadId: string,
): MenuItem[] {
  const item: BoardItem = {
    kind: "load",
    ids: [loadId],
    fromTruckId: lane.truck_id,
  };
  const step = (label: string, m: "earlier" | "later"): MenuItem => {
    const target = loadMoveTarget(lane, loadId, m);
    return {
      label,
      onSelect: () => target && api.perform(item, target, "menu"),
      disabled: !target,
      reason: !target ? "Already there" : undefined,
    };
  };
  return [
    detailsItem(api, { truckId: lane.truck_id, loadId }),
    ...readOnlyItems(api, [
      {
        label: "Move load to…",
        onSelect: () => api.openAssign("load", item),
      },
      step("Move load earlier", "earlier"),
      step("Move load later", "later"),
    ]),
  ];
}

function detailsItem(
  api: BoardApi,
  target: Parameters<BoardApi["openDetails"]>[0],
): MenuItem {
  return { label: "Details", onSelect: () => api.openDetails(target) };
}

export function driverMenu(api: BoardApi, driverId: string): MenuItem[] {
  return readOnlyItems(api, [
    {
      label: "Pair with truck…",
      onSelect: () =>
        api.openAssign("pair", { kind: "driver", ids: [driverId] }),
    },
  ]);
}

const LOCKED_REASON = "Publishing, wait for it to finish";

/** "Publish lane" / "Retry publish" for the lane menu (R12.1, R12.6). */
function publishItem(api: BoardApi, lane: LaneView): MenuItem | null {
  switch (lane.state) {
    case "failed":
    case "recovering":
      return {
        label: "Retry publish",
        onSelect: () => api.retryPublish(lane.truck_id),
      };
    case "published":
      return {
        label: "Publish lane",
        onSelect: () => undefined,
        disabled: true,
        reason: "Already published",
      };
    case "publishing":
      return null;
    default:
      return {
        label: "Publish lane",
        onSelect: () => api.openPublish([lane.truck_id]),
      };
  }
}

export function laneMenu(api: BoardApi, lane: LaneView): MenuItem[] {
  const items: MenuItem[] = [];
  const suggested = lane.suggested_driver;
  if (!lane.driver_id && suggested) {
    items.push({
      label: `Pair ${suggested.name}`,
      onSelect: () =>
        api.perform(
          { kind: "driver", ids: [suggested.driver_id] },
          { target: "driver-slot", truckId: lane.truck_id },
          "menu",
        ),
    });
  }
  if (lane.driver_id) {
    // `pair_driver` with no driver; there is no drag twin for this one.
    items.push({
      label: "Unpair driver",
      onSelect: () => api.unpair(lane.truck_id),
    });
  }
  if (lane.state === "modified") {
    items.push({
      label: "Discard changes",
      onSelect: () => api.discard(lane.truck_id),
    });
  }
  // A lane in recovery keeps Retry as its only action (K14.7).
  const locked = api.laneLocked(lane.truck_id);
  const editable = locked
    ? items.map((i) => ({ ...i, disabled: true, reason: LOCKED_REASON }))
    : items;
  const publish = publishItem(api, lane);
  const mutating = readOnlyItems(api, [
    ...(publish ? [publish] : []),
    ...editable,
  ]);
  const suggestions = api.snapshot.suggestions.filter(
    (s) => s.truck_id === lane.truck_id,
  ).length;
  if (suggestions > 0) {
    mutating.push({
      label: `Review suggestions (${suggestions})`,
      onSelect: () => api.openSuggestions({ truckId: lane.truck_id }),
    });
  }
  mutating.unshift({
    label: "Details",
    onSelect: () => api.openDetails({ truckId: lane.truck_id }),
  });
  const collapsed = api.collapsed.has(lane.truck_id);
  mutating.push({
    label: collapsed ? "Expand lane" : "Collapse lane",
    onSelect: () => api.toggleCollapsed(lane.truck_id),
  });
  const removeBlocked = lane.ever_published;
  mutating.push(
    ...readOnlyItems(api, [
      {
        label: "Remove lane",
        onSelect: () => api.removeLane(lane.truck_id),
        disabled: removeBlocked,
        reason: removeBlocked ? REMOVE_LANE_REASON : undefined,
      },
    ]),
  );
  return mutating;
}
