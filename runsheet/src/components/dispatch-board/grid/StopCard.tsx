/**
 * A stop on a lane (R5.2, R7.1, R13.2, R18, R19.4). A grid cell in the lane
 * row: click or Enter selects it (Place mode), Shift/Cmd-click or Space adds
 * it to the selection, Shift+F10 / right-click opens its menu. It is a drop
 * target whose closest edge picks the slot before or after it (hitbox), and
 * it drags by its grip. Started stops are pinned: no grip, menu moves off.
 */
import { GripVertical, Lock } from "lucide-react";
import { type KeyboardEvent, type MouseEvent, memo, useRef } from "react";
import type { LaneView, Stop } from "../../../services/dispatchBoardApi";
import { focusKey, useBoard } from "../BoardContext";
import { matchClass, matchSuffix, stopMatch } from "../boardMatch";
import { formatTime } from "../boardTime";
import { CheckChip } from "../CheckChip";
import { useDraggableItem, useDropTarget } from "../dnd/adapter";
import { previewLabel } from "../dnd/dragData";
import { PINNED_STATUSES } from "../intents";
import { useRovingItem } from "../keyboard/roving";
import { stopMenu } from "../menus";
import { worstCheck } from "../state/announce";
import { quantityText } from "../trays/OrderCard";
import { AXIS_HEIGHT, CARD_WIDTH, HEADER_WIDTH } from "./layout";

export interface StopCardProps {
  lane: LaneView;
  loadId: string | null;
  stop: Stop;
  /** Position in the load (0-based); `null` on the shelf. */
  index: number | null;
  left?: number;
}

/** Scroll margins so focus is never hidden under the sticky axis / header (SC 2.4.11). */
export const CELL_SCROLL_MARGIN = {
  scrollMarginTop: AXIS_HEIGHT + 8,
  scrollMarginLeft: HEADER_WIDTH + 8,
};

export const StopCard = memo(function StopCard({
  lane,
  loadId,
  stop,
  index,
  left,
}: StopCardProps) {
  const api = useBoard();
  const ref = useRef<HTMLDivElement>(null);
  const grip = useRef<HTMLSpanElement>(null);
  const id = stop.order_id;
  const pinned = PINNED_STATUSES.has(stop.snapshot.status ?? "");
  const locked = api.readOnly || api.laneLocked(lane.truck_id);
  const onShelf = index === null;
  const card = { kind: "stop" as const, ids: [id], fromTruckId: lane.truck_id };
  const roving = useRovingItem(focusKey.stop(id));
  const dragging = useDraggableItem(ref, {
    item: api.itemFor(card),
    handleRef: grip,
    enabled: !pinned && !locked,
    previewLabel: () => {
      const item = api.itemFor(card);
      const gallons = item.ids.reduce((sum, oid) => {
        const s = lane.loads
          .flatMap((l) => l.stops)
          .find((x) => x.order_id === oid);
        return sum + (s?.snapshot.gallons_requested ?? 0);
      }, 0);
      return previewLabel(item, gallons);
    },
  });
  const over = useDropTarget(ref, {
    target: {
      target: "load",
      truckId: lane.truck_id,
      loadId: loadId ?? "",
      index: index ?? 0,
    },
    enabled: !onShelf && loadId !== null,
    readOnly: locked,
    edge: true,
  });
  const selected = api.isSelected("stop", id);
  const state = stopMatch(stop, lane, api.match);
  const worst = worstCheck(
    lane.checks.filter((c) => c.scope.order_id === id),
    { orderId: id },
  );
  const eta = formatTime(stop.eta, api.timezone);
  const density = api.view.density;

  const name = [
    onShelf
      ? `To reassign, Order ${id}`
      : `Stop ${(index ?? 0) + 1}, Order ${id}`,
    stop.snapshot.product_code ?? "product unknown",
    quantityText(stop.snapshot),
    eta ? `ETA ${eta}` : "no ETA",
    pinned ? "started, pinned" : "",
    worst
      ? `${worst.outcome === "block" ? "Blocked" : "Warning"}: ${worst.message}`
      : "",
  ]
    .filter(Boolean)
    .join(", ");

  const openMenu = () =>
    api.showMenu({
      label: `Order ${id} actions`,
      items: stopMenu(api, lane, id, onShelf),
      anchor: ref.current,
    });

  const onClick = (e: MouseEvent) => {
    e.stopPropagation();
    if (pinned) {
      openMenu();
      return;
    }
    api.select(card, e.shiftKey || e.metaKey || e.ctrlKey);
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      if (pinned) openMenu();
      else api.select(card);
    } else if (e.key === " ") {
      e.preventDefault();
      e.stopPropagation();
      if (!pinned) api.select(card, true);
    } else if ((e.key === "F10" && e.shiftKey) || e.key === "ContextMenu") {
      e.preventDefault();
      e.stopPropagation();
      openMenu();
    }
  };

  return (
    <div
      ref={ref}
      role="gridcell"
      aria-selected={selected}
      aria-label={`${name}${matchSuffix(state)}`}
      {...roving}
      tabIndex={roving.tabIndex}
      data-stop-card=""
      data-match={state}
      onClick={onClick}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => {
        e.preventDefault();
        openMenu();
      }}
      style={{
        ...(left !== undefined
          ? { position: "absolute", left, top: 0 }
          : undefined),
        width: CARD_WIDTH[density],
        ...CELL_SCROLL_MARGIN,
      }}
      className={`flex h-full gap-0.5 overflow-hidden rounded-md border bg-white p-1 text-xs outline-none focus-visible:ring-2 focus-visible:ring-primary ${
        selected ? "border-primary bg-primary-soft" : "border-gray-300"
      } ${dragging ? "opacity-50" : ""} ${over ? "ring-2 ring-primary" : ""} ${matchClass(state)}`}
    >
      {!pinned && !locked ? (
        <span
          ref={grip}
          aria-hidden="true"
          data-drag-handle=""
          className="flex min-h-6 min-w-6 shrink-0 cursor-grab touch-none items-start justify-center pt-0.5 text-gray-400 pointer-coarse:min-h-11 pointer-coarse:min-w-11"
        >
          <GripVertical className="h-3.5 w-3.5" />
        </span>
      ) : (
        <span aria-hidden="true" className="shrink-0 pt-0.5 text-gray-500">
          {pinned && <Lock className="h-3.5 w-3.5" />}
        </span>
      )}
      <div aria-hidden="true" className="min-w-0 flex-1 leading-tight">
        <div className="truncate font-medium text-gray-900">#{id}</div>
        <div className="truncate text-gray-700">
          {stop.snapshot.product_code ?? "—"} · {quantityText(stop.snapshot)}
        </div>
        {density === "comfortable" && (
          <div className="truncate text-gray-600">
            {pinned ? "Started" : eta ? `ETA ${eta}` : "No ETA"}
          </div>
        )}
        {worst && density === "comfortable" && (
          <CheckChip
            outcome={worst.outcome}
            label={worst.message}
            className="mt-0.5"
          />
        )}
      </div>
    </div>
  );
});

/** A stop drawn at its target while its command is pending (R8.6). */
export function GhostCard({
  orderId,
  left,
}: {
  orderId: string;
  left?: number;
}) {
  const api = useBoard();
  return (
    <div
      data-ghost=""
      aria-label={`Order ${orderId}, checking`}
      role="gridcell"
      tabIndex={-1}
      style={{
        ...(left !== undefined
          ? { position: "absolute", left, top: 0 }
          : undefined),
        width: CARD_WIDTH[api.view.density],
      }}
      className="flex h-full flex-col gap-0.5 rounded-md border border-dashed border-primary bg-white/80 p-1 text-xs"
    >
      <span className="truncate font-medium text-gray-900">#{orderId}</span>
      <CheckChip outcome="checking" />
    </div>
  );
}

export default StopCard;
