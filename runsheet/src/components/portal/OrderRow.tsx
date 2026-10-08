"use client";

/**
 * One order as a two-line row (D27, R14.9, ≤ 72 px on phones): product cap,
 * "{tank title} · {quantity}", the status badge, and one secondary line
 * (window or delivered time, PO, ticket). Cancellable rows get Cancel
 * request, named with the tank and date, never the order id (D29).
 */
import { forwardRef } from "react";
import type { PortalOrder } from "../../services/portalApi";
import { ProductCap } from "../ui/ProductChip";
import { orderReference } from "./messages";
import PortalStatus from "./PortalStatus";
import {
  dateTime,
  deliveredVolume,
  date as formatDate,
  window as formatWindow,
  volume,
} from "./portalFormat";
import { rowButton, space } from "./styles";

export function quantityText(order: PortalOrder, unit = "gal"): string {
  if (order.delivered_gallons !== null && order.status_code === "delivered") {
    return `${deliveredVolume(order.delivered_gallons, unit)} delivered`;
  }
  if (order.fill_to_full) return "Fill to full";
  return volume(order.gallons_requested, unit);
}

export function deliveryText(order: PortalOrder): string {
  if (order.delivered_at) return dateTime(order.delivered_at);
  return formatWindow(order.window_start, order.window_end);
}

export function secondaryLine(order: PortalOrder): string {
  return [
    deliveryText(order),
    order.po_number ? order.po_number : null,
    order.ticket_number ? `ticket ${order.ticket_number}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** "{tank title}, Thu 9 Oct" for buttons and announcements. */
export function orderRef(order: PortalOrder, title: string): string {
  return orderReference(
    title,
    formatDate(order.window_start ?? order.created_at),
  );
}

const OrderRow = forwardRef<
  HTMLLIElement,
  {
    order: PortalOrder;
    title: string;
    unit?: string;
    onCancel?: (order: PortalOrder) => void;
    cancelDisabled?: boolean;
  }
>(({ order, title, unit = "gal", onCancel, cancelDisabled }, ref) => (
  <li
    ref={ref}
    tabIndex={-1}
    data-order-row
    className={`grid grid-cols-[22px_minmax(0,1fr)_auto] items-center gap-x-3 border-t border-slate-100 first:border-t-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus ${space.inset} ${space.listRowY}`}
  >
    <span className="row-span-2 self-start pt-0.5">
      {order.product_code ? (
        <ProductCap code={order.product_code} />
      ) : (
        <span aria-hidden="true" />
      )}
    </span>
    <p className="min-w-0 truncate text-[15px] font-semibold leading-5 text-text">
      {title} · {quantityText(order, unit)}
    </p>
    <PortalStatus
      kind="order"
      code={order.status_code}
      label={order.status_label}
    />
    <p className="col-span-2 col-start-2 min-w-0 truncate text-sm leading-5 text-text-muted">
      {secondaryLine(order)}
    </p>
    {order.cancellable && onCancel && (
      <div className="col-span-2 col-start-2 pt-2">
        <button
          type="button"
          className={rowButton}
          aria-label={`Cancel request for ${orderRef(order, title)}`}
          aria-disabled={cancelDisabled ? true : undefined}
          onClick={() => onCancel(order)}
        >
          Cancel request
        </button>
      </div>
    )}
  </li>
));
OrderRow.displayName = "OrderRow";

export default OrderRow;
