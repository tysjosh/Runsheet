/**
 * Truck tray (R3.5): trucks with compartments and no lane on this day. Drag
 * one by its grip onto the board's "add lane" area, or press "Add lane";
 * both send `add_lane`.
 */
import { GripVertical } from "lucide-react";
import { useRef } from "react";
import type { TrayTruck } from "../../../services/dispatchBoardApi";
import { focusKey, useBoard } from "../BoardContext";
import { formatGallons, litersToGallons } from "../boardTime";
import { useDraggableItem } from "../dnd/adapter";
import { previewLabel } from "../dnd/dragData";

function TruckRow({ truck }: { truck: TrayTruck }) {
  const api = useBoard();
  const ref = useRef<HTMLLIElement>(null);
  const grip = useRef<HTMLSpanElement>(null);
  const item = { kind: "truck" as const, ids: [truck.truck_id] };
  useDraggableItem(ref, {
    item,
    handleRef: grip,
    enabled: !api.readOnly,
    previewLabel: () => previewLabel(item, null),
  });
  const capacity = formatGallons(litersToGallons(truck.capacity_l));
  return (
    <li
      ref={ref}
      className="flex items-center gap-2 rounded-md border border-gray-200 bg-white p-2 text-sm"
    >
      {!api.readOnly && (
        <span
          ref={grip}
          aria-hidden="true"
          data-drag-handle=""
          className="flex min-h-6 min-w-6 cursor-grab touch-none items-center justify-center text-gray-400 pointer-coarse:min-h-11 pointer-coarse:min-w-11"
        >
          <GripVertical className="h-4 w-4" />
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="font-medium text-gray-900">Truck {truck.truck_id}</div>
        <div className="text-xs text-gray-600">
          {truck.compartment_count}{" "}
          {truck.compartment_count === 1 ? "compartment" : "compartments"} ·{" "}
          {capacity}
        </div>
      </div>
      <button
        type="button"
        data-focus-key={focusKey.truck(truck.truck_id)}
        aria-label={`Add lane for Truck ${truck.truck_id}`}
        aria-disabled={api.readOnly || undefined}
        onClick={() =>
          api.readOnly
            ? api.announce(api.readOnlyText)
            : api.addLane(truck.truck_id, "menu")
        }
        className="min-h-8 rounded-md border border-gray-300 px-2 text-xs font-medium text-gray-700 hover:bg-gray-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
      >
        Add lane
      </button>
    </li>
  );
}

export function TruckTray() {
  const api = useBoard();
  const trucks = api.snapshot.trays.trucks;
  return (
    <section aria-label="Truck tray" className="flex min-h-0 flex-1 flex-col">
      <h2 className="pb-2 text-sm font-semibold text-gray-900">
        Trucks without a lane ({trucks.length})
      </h2>
      {trucks.length === 0 ? (
        <p className="text-sm text-gray-600">Every truck has a lane.</p>
      ) : (
        <ul className="min-h-0 flex-1 space-y-2 overflow-auto pr-1">
          {trucks.map((t) => (
            <TruckRow key={t.truck_id} truck={t} />
          ))}
        </ul>
      )}
    </section>
  );
}

export default TruckTray;
