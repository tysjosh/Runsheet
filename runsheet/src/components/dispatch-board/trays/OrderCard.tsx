/**
 * Order card in the tray (R3.2, R3.6, R18.3). A listbox option: click or
 * Enter selects it for Place mode, Shift/Cmd-click or Space adds it to the
 * selection, Shift+F10 / right-click opens its menu. Only the grip starts a
 * drag (R20.1); on-hold cards have no grip and can't be dragged.
 */
import { GripVertical } from "lucide-react";
import { type KeyboardEvent, type MouseEvent, memo, useRef } from "react";
import { productName } from "../../../lib/format";
import type { TrayOrder } from "../../../services/dispatchBoardApi";
import { ProductCap, StatusBadge, statusKeyFor } from "../../ui";
import { focusKey, useBoard } from "../BoardContext";
import { matchClass, matchSuffix, trayOrderMatch } from "../boardMatch";
import { formatGallons, formatWindow } from "../boardTime";
import { useDraggableItem } from "../dnd/adapter";
import { previewLabel } from "../dnd/dragData";
import { useRovingItem } from "../keyboard/roving";
import { orderMenu } from "../menus";

export const CALL_TYPE_LABEL: Record<string, string> = {
  keep_full: "Keep full",
  auto_fill: "Auto fill",
  will_call: "Will call",
  one_off: "One off",
};

export function quantityText(o: {
  fill_to_full: boolean;
  gallons_requested: number | null;
}): string {
  if (o.fill_to_full) return "Fill";
  return typeof o.gallons_requested === "number"
    ? formatGallons(o.gallons_requested)
    : "Gallons not set";
}

/** The card's text, also its accessible name (customer names stay off logs, not off screen). */
export function orderCardText(order: TrayOrder, timeZone: string): string[] {
  const parts = [
    `Order ${order.order_id}`,
    order.customer_name ?? order.customer_id ?? "Customer unknown",
    order.product_code ?? "Product unknown",
    quantityText(order),
    formatWindow(
      order.delivery_window_start,
      order.delivery_window_end,
      timeZone,
    ),
  ];
  if (order.call_type)
    parts.push(CALL_TYPE_LABEL[order.call_type] ?? order.call_type);
  if (order.priority_bucket) parts.push(`priority ${order.priority_bucket}`);
  if (order.block_reason === "on_hold") parts.push("on hold");
  if (order.missing_window) parts.push("missing window");
  if (order.dyed) parts.push("dyed diesel");
  return parts;
}

export interface OrderCardProps {
  order: TrayOrder;
}

function Badge({ children }: { children: string }) {
  return (
    <span className="rounded border border-slate-300 bg-white px-1 text-[11px] font-medium text-slate-700">
      {children}
    </span>
  );
}

const ORDER_STATUS_LABEL: Record<string, string> = {
  placed: "Placed",
  confirmed: "Confirmed",
  scheduled: "Scheduled",
  on_hold: "On hold",
};

export const OrderCard = memo(function OrderCard({ order }: OrderCardProps) {
  const api = useBoard();
  const ref = useRef<HTMLDivElement>(null);
  const grip = useRef<HTMLSpanElement>(null);
  const onHold = order.block_reason === "on_hold" || order.draggable === false;
  const key = focusKey.order(order.order_id);
  const roving = useRovingItem(key);
  const selected = api.isSelected("order", order.order_id);
  const card = { kind: "order" as const, ids: [order.order_id] };
  const dragging = useDraggableItem(ref, {
    item: api.itemFor(card),
    handleRef: grip,
    enabled: !onHold && !api.readOnly,
    previewLabel: () => {
      const item = api.itemFor(card);
      const gallons = item.ids.reduce((sum, id) => {
        const o = api.snapshot.trays.orders.find((x) => x.order_id === id);
        return sum + (o?.gallons_requested ?? 0);
      }, 0);
      return previewLabel(item, gallons);
    },
  });
  const state = trayOrderMatch(order, api.match);
  const text = orderCardText(order, api.timezone);

  const openMenu = () =>
    api.showMenu({
      label: `Order ${order.order_id} actions`,
      items: orderMenu(api, order.order_id, onHold),
      anchor: ref.current,
    });

  const onClick = (e: MouseEvent) => {
    if (onHold) {
      openMenu();
      return;
    }
    api.select(card, e.shiftKey || e.metaKey || e.ctrlKey);
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (onHold) openMenu();
      else api.select(card);
    } else if (e.key === " ") {
      e.preventDefault();
      if (!onHold) api.select(card, true);
    } else if ((e.key === "F10" && e.shiftKey) || e.key === "ContextMenu") {
      e.preventDefault();
      openMenu();
    }
  };

  return (
    <div
      ref={ref}
      role="option"
      aria-selected={selected}
      aria-label={`${text.join(", ")}${matchSuffix(state)}`}
      {...roving}
      tabIndex={roving.tabIndex}
      onClick={onClick}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => {
        e.preventDefault();
        openMenu();
      }}
      data-match={state}
      className={`flex scroll-my-2 gap-1 rounded-lg border bg-white p-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-primary ${
        selected ? "border-primary bg-primary-soft" : "border-slate-200"
      } ${dragging ? "opacity-50" : ""} ${matchClass(state)}`}
    >
      {!onHold && !api.readOnly && (
        <span
          ref={grip}
          aria-hidden="true"
          data-drag-handle=""
          className="flex min-h-6 min-w-6 cursor-grab touch-none items-center justify-center text-slate-500 pointer-coarse:min-h-11 pointer-coarse:min-w-11"
        >
          <GripVertical className="h-4 w-4" />
        </span>
      )}
      <OrderFace order={order} onHold={onHold} timeZone={api.timezone} />
    </div>
  );
});

/**
 * The card's visible text, memoised so the board's per-frame context
 * re-renders during a drag skip it (N1 budget with the added caps/badges).
 */
const OrderFace = memo(function OrderFace({
  order,
  onHold,
  timeZone,
}: {
  order: TrayOrder;
  onHold: boolean;
  timeZone: string;
}) {
  return (
    <div aria-hidden="true" className="min-w-0 flex-1">
      <div className="flex items-center gap-2">
        {order.product_code && (
          <ProductCap code={order.product_code} decorative />
        )}
        <span className="min-w-0 flex-1 truncate font-semibold text-slate-900">
          {order.customer_name ?? order.customer_id ?? "Customer unknown"}
        </span>
        {onHold ? (
          <StatusBadge status="delayed" label="On hold" />
        ) : (
          <StatusBadge
            status={statusKeyFor(order.status) ?? "draft"}
            label={ORDER_STATUS_LABEL[order.status ?? ""] ?? "Placed"}
          />
        )}
      </div>
      <div className="truncate text-xs text-slate-700">
        {order.product_code ? productName(order.product_code) : "—"} ·{" "}
        {quantityText(order)}
      </div>
      <div className="text-xs text-slate-600">
        {formatWindow(
          order.delivery_window_start,
          order.delivery_window_end,
          timeZone,
        )}
        {order.call_type &&
          ` · ${CALL_TYPE_LABEL[order.call_type] ?? order.call_type}`}
      </div>
      <div className="mt-1 flex flex-wrap gap-1">
        <span className="text-[11px] text-slate-600">#{order.order_id}</span>
        {order.priority_bucket && <Badge>{order.priority_bucket}</Badge>}
        {order.missing_window && <Badge>No window</Badge>}
        {order.dyed && <Badge>Dyed</Badge>}
      </div>
    </div>
  );
});

export default OrderCard;
