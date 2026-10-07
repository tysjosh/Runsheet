/**
 * One load on a lane (R5.2, R7.2, R7.4, R8.4, R18.3). The terminal chip is
 * the load's drag handle and its menu button; dropping a load on another
 * load's chip puts it in that position. The body is a drop target whose slot
 * comes from the pointer x over the stop cards (snap to the nearest gap).
 * In Place mode, slot buttons appear between cards. While a drag hovers a
 * slot, an insertion line shows the position check from the server.
 */
import { Plus } from "lucide-react";
import { type KeyboardEvent, useRef } from "react";
import type { LaneView, Load } from "../../../services/dispatchBoardApi";
import { focusKey, useBoard } from "../BoardContext";
import { CheckChip } from "../CheckChip";
import { resultReason } from "../dialogs/AssignToMenu";
import { useDraggableItem, useDropTarget } from "../dnd/adapter";
import { previewLabel } from "../dnd/dragData";
import { useRovingItem } from "../keyboard/roving";
import { loadMenu } from "../menus";
import { CARD_WIDTH, CHIP_WIDTH, type LoadBox } from "./layout";
import { CELL_SCROLL_MARGIN, GhostCard, StopCard } from "./StopCard";

export interface LoadBlockProps {
  lane: LaneView;
  load: Load;
  number: number;
  box: LoadBox;
  /** Place mode with an order or stop selected: show slot buttons. */
  placingStops: boolean;
}

function LoadChip({
  lane,
  load,
  number,
}: {
  lane: LaneView;
  load: Load;
  number: number;
}) {
  const api = useBoard();
  const ref = useRef<HTMLButtonElement>(null);
  const roving = useRovingItem(focusKey.load(load.load_id));
  const item = {
    kind: "load" as const,
    ids: [load.load_id],
    fromTruckId: lane.truck_id,
  };
  const index = lane.loads.findIndex((l) => l.load_id === load.load_id);
  useDraggableItem(ref, {
    item,
    enabled: !api.readOnly,
    previewLabel: () => previewLabel(item, null),
  });
  const over = useDropTarget(ref, {
    target: { target: "load-position", truckId: lane.truck_id, index },
    enabled: true,
    readOnly: api.readOnly,
  });
  const placingLoad = api.placeItem?.kind === "load";
  const terminal = load.terminal_id ?? "No terminal";
  const openMenu = () =>
    api.showMenu({
      label: `Load ${number} actions`,
      items: loadMenu(api, lane, load.load_id),
      anchor: ref.current,
    });
  const onKeyDown = (e: KeyboardEvent) => {
    if ((e.key === "F10" && e.shiftKey) || e.key === "ContextMenu") {
      e.preventDefault();
      e.stopPropagation();
      openMenu();
    } else if (e.key === " ") {
      e.preventDefault();
      e.stopPropagation();
      api.select(item);
    }
  };
  return (
    <div role="gridcell" tabIndex={-1} className="h-full">
      <button
        ref={ref}
        type="button"
        {...roving}
        aria-label={
          placingLoad
            ? `Place the load at position ${index + 1} on Truck ${lane.truck_id}`
            : `Load ${number}, terminal ${terminal}, ${load.stops.length} ${load.stops.length === 1 ? "stop" : "stops"}`
        }
        aria-pressed={api.isSelected("load", load.load_id)}
        onClick={(e) => {
          e.stopPropagation();
          if (placingLoad && !api.isSelected("load", load.load_id)) {
            api.placeOn({
              target: "load-position",
              truckId: lane.truck_id,
              index,
            });
          } else {
            api.select(item);
          }
        }}
        onKeyDown={onKeyDown}
        onContextMenu={(e) => {
          e.preventDefault();
          openMenu();
        }}
        style={{ width: CHIP_WIDTH, ...CELL_SCROLL_MARGIN }}
        className={`flex h-full cursor-grab flex-col items-start justify-center rounded-md border bg-gray-50 px-1.5 text-left text-[11px] leading-tight outline-none focus-visible:ring-2 focus-visible:ring-primary ${
          api.isSelected("load", load.load_id)
            ? "border-primary bg-primary-soft"
            : "border-gray-300"
        } ${over ? "ring-2 ring-primary" : ""}`}
      >
        <span className="font-semibold text-gray-900">Load {number}</span>
        <span className="truncate text-gray-600">{terminal}</span>
      </button>
    </div>
  );
}

function SlotButton({
  lane,
  loadId,
  index,
  left,
  label,
}: {
  lane: LaneView;
  loadId: string;
  index: number;
  left: number;
  label: string;
}) {
  const api = useBoard();
  const roving = useRovingItem(`slot:${loadId}:${index}`);
  return (
    <div
      role="gridcell"
      tabIndex={-1}
      className="absolute top-1/2 z-10 -translate-y-1/2"
      style={{ left: left - 12 }}
    >
      <button
        type="button"
        {...roving}
        aria-label={label}
        onClick={(e) => {
          e.stopPropagation();
          api.placeOn({
            target: "load",
            truckId: lane.truck_id,
            loadId,
            index,
          });
        }}
        style={CELL_SCROLL_MARGIN}
        className="flex h-6 w-6 items-center justify-center rounded-full border border-primary bg-white text-primary shadow outline-none hover:bg-primary-soft focus-visible:ring-2 focus-visible:ring-primary"
      >
        <Plus className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}

export function LoadBlock({
  lane,
  load,
  number,
  box,
  placingStops,
}: LoadBlockProps) {
  const api = useBoard();
  const bodyRef = useRef<HTMLDivElement>(null);
  const over = useDropTarget(bodyRef, {
    target: {
      target: "load",
      truckId: lane.truck_id,
      loadId: load.load_id,
      index: null,
    },
    enabled: true,
    readOnly: api.readOnly,
    slotSelector: "[data-stop-card]",
  });
  const cardW = CARD_WIDTH[api.view.density];
  const stopsById = new Map(load.stops.map((s) => [s.order_id, s]));
  const pos =
    api.position &&
    api.position.truckId === lane.truck_id &&
    api.position.loadId === load.load_id
      ? api.position
      : null;
  const realCards = box.cards.filter((c) => !c.ghost);
  const slotLeft = (i: number) => {
    if (realCards.length === 0) return CHIP_WIDTH + 12;
    if (i === 0) return realCards[0].left - 3;
    const prev = realCards[i - 1];
    return prev.left + cardW + 3;
  };
  return (
    <div
      ref={bodyRef}
      data-load={load.load_id}
      className={`absolute inset-y-0 rounded-md ${over ? "bg-primary-soft/60" : "bg-gray-50/60"}`}
      style={{ left: box.left, width: box.width }}
    >
      <div className="absolute inset-y-0 left-0">
        <LoadChip lane={lane} load={load} number={number} />
      </div>
      {box.cards.map((c) => {
        if (c.ghost) {
          return (
            <GhostCard
              key={`ghost-${c.orderId}`}
              orderId={c.orderId}
              left={c.left}
            />
          );
        }
        const stop = stopsById.get(c.orderId);
        if (!stop) return null;
        const index = load.stops.indexOf(stop);
        return (
          <StopCard
            key={c.orderId}
            lane={lane}
            loadId={load.load_id}
            stop={stop}
            index={index}
            left={c.left}
          />
        );
      })}
      {placingStops &&
        Array.from({ length: realCards.length + 1 }, (_, i) => (
          <SlotButton
            key={i}
            lane={lane}
            loadId={load.load_id}
            index={i}
            left={slotLeft(i)}
            label={`Place at stop ${i + 1} of load ${number} on Truck ${lane.truck_id}`}
          />
        ))}
      {pos && (
        <div
          data-testid="insertion-indicator"
          className="pointer-events-none absolute inset-y-0 z-20 w-0.5 bg-primary"
          style={{ left: slotLeft(pos.index) }}
        >
          <div className="absolute -top-1 left-1 whitespace-nowrap">
            <CheckChip
              outcome={pos.result ? pos.result.outcome : "checking"}
              label={resultReason(pos.result ?? undefined)}
            />
          </div>
        </div>
      )}
    </div>
  );
}

export default LoadBlock;
