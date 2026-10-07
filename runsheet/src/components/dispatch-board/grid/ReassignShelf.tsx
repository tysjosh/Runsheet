/**
 * "To reassign" shelf (R13.5): dispatched orders taken off a published load
 * wait here, not in the order tray, until placed on a load. It is a drop
 * target (dropping a stop here unassigns it, which the server shelves) and
 * its cards move by drag or "Move to…".
 */
import { useRef } from "react";
import type { LaneView, Stop } from "../../../services/dispatchBoardApi";
import { useBoard } from "../BoardContext";
import { useDropTarget } from "../dnd/adapter";
import { StopCard } from "./StopCard";

function shelfStop(lane: LaneView, orderId: string): Stop {
  const published = lane.publish.published_content?.loads
    .flatMap((l) => l.stops)
    .find((s) => s.order_id === orderId);
  return (
    published ?? {
      order_id: orderId,
      snapshot: {
        product_code: null,
        customer_id: null,
        customer_tank_id: null,
        gallons_requested: null,
        fill_to_full: false,
        window: { start: null, end: null },
        call_type: null,
        status: "dispatched",
      },
      location: null,
      eta: null,
    }
  );
}

export function ReassignShelf({
  lane,
  left,
}: {
  lane: LaneView;
  left: number;
}) {
  const api = useBoard();
  const ref = useRef<HTMLDivElement>(null);
  const over = useDropTarget(ref, {
    target: { target: "shelf", truckId: lane.truck_id },
    enabled: true,
    readOnly: api.readOnly,
  });
  return (
    <div
      ref={ref}
      data-shelf={lane.truck_id}
      className={`absolute inset-y-0 flex items-stretch gap-1 rounded-md border border-dashed px-1 ${
        over
          ? "border-primary bg-primary-soft"
          : "border-warning bg-warning-light/40"
      }`}
      style={{ left }}
    >
      <div
        role="gridcell"
        tabIndex={-1}
        className="flex w-16 shrink-0 flex-col justify-center text-[11px] font-medium leading-tight text-warning-dark"
      >
        To reassign ({lane.shelf.length})
      </div>
      {lane.shelf.map((id) => (
        <StopCard
          key={id}
          lane={lane}
          loadId={null}
          stop={shelfStop(lane, id)}
          index={null}
        />
      ))}
    </div>
  );
}

export default ReassignShelf;
