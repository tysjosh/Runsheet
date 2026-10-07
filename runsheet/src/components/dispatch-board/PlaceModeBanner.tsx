/**
 * Place mode banner (design K14.4, R18.3): "Placing Order 1042. Choose a
 * truck or press Escape." It also offers the selection's actions menu, so a
 * pointer user reaches every card action with single clicks.
 */
import { useRef } from "react";
import { useBoard } from "./BoardContext";
import type { BoardItem } from "./intents";
import { driverMenu, loadMenu, orderMenu, stopMenu } from "./menus";
import { laneOfLoad, laneOfOrder } from "./state/boardReducer";

export function placingText(item: BoardItem): string {
  const n = item.ids.length;
  switch (item.kind) {
    case "order":
    case "stop":
      return `Placing ${n === 1 ? `Order ${item.ids[0]}` : `${n} orders`}. Choose a truck or press Escape.`;
    case "load":
      return "Placing the load. Choose a truck or a load position, or press Escape.";
    case "driver":
      return `Placing Driver ${item.ids[0]}. Choose a driver slot or press Escape.`;
    case "truck":
      return `Placing Truck ${item.ids[0]}. Press Escape to cancel.`;
  }
}

export function PlaceModeBanner() {
  const api = useBoard();
  const actionsRef = useRef<HTMLButtonElement>(null);
  const item = api.placeItem;
  if (!item) return null;

  const openActions = () => {
    const id = item.ids[0];
    let items = null;
    if (item.kind === "order") items = orderMenu(api, id, false);
    else if (item.kind === "stop") {
      const lane = laneOfOrder(api.state, id);
      if (lane) items = stopMenu(api, lane, id, lane.shelf.includes(id));
    } else if (item.kind === "load") {
      const lane = laneOfLoad(api.state, id);
      if (lane) items = loadMenu(api, lane, id);
    } else if (item.kind === "driver") items = driverMenu(api, id);
    if (!items) return;
    api.showMenu({
      label: "Selection actions",
      items,
      anchor: actionsRef.current,
    });
  };

  return (
    <div className="flex items-center gap-3 border-b border-primary bg-primary-soft px-4 py-2 text-sm text-gray-900">
      <p role="status" className="flex-1">
        {placingText(item)}
      </p>
      {item.kind !== "truck" && (
        <button
          ref={actionsRef}
          type="button"
          aria-haspopup="menu"
          onClick={openActions}
          className="min-h-8 rounded-md border border-gray-300 bg-white px-3 text-xs font-medium text-gray-800 hover:bg-gray-50"
        >
          Actions
        </button>
      )}
      <button
        type="button"
        onClick={() => api.clearSelection()}
        className="min-h-8 rounded-md border border-gray-300 bg-white px-3 text-xs font-medium text-gray-800 hover:bg-gray-50"
      >
        Cancel
      </button>
    </div>
  );
}

export default PlaceModeBanner;
