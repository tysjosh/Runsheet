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
  dateTimeZone,
  EMPTY,
  date as formatDate,
  window as formatWindow,
  volume,
} from "./portalFormat";
import { rowButton, space } from "./styles";

/**
 * One pattern for every order quantity (portal-fixes A6): whole gallons,
 * then a muted qualifier: "1,600 gal requested", "1,450 gal delivered",
 * "Fill to full requested". Invoices keep the billed precision instead.
 */
export function quantityParts(
  order: PortalOrder,
  unit = "gal",
): { value: string; qualifier: "delivered" | "requested" | null } {
  if (order.delivered_gallons !== null && order.status_code === "delivered") {
    return {
      value: volume(Math.round(order.delivered_gallons), unit),
      qualifier: "delivered",
    };
  }
  if (order.fill_to_full) return { value: "Fill to full", qualifier: null };
  if (order.gallons_requested === null)
    return { value: EMPTY, qualifier: null };
  return {
    value: volume(Math.round(order.gallons_requested), unit),
    qualifier: "requested",
  };
}

export function quantityText(order: PortalOrder, unit = "gal"): string {
  const { value, qualifier } = quantityParts(order, unit);
  return qualifier ? `${value} ${qualifier}` : value;
}

/** Number first, the qualifier muted after it. */
export function QuantityCell({
  order,
  unit = "gal",
}: {
  order: PortalOrder;
  unit?: string;
}) {
  const { value, qualifier } = quantityParts(order, unit);
  return (
    <span className="tabular-nums">
      {value}
      {qualifier && (
        <span className="font-normal text-text-muted"> {qualifier}</span>
      )}
    </span>
  );
}

/** The delivered moment or the window, both with the zone (portal-fixes A5). */
export function deliveryText(order: PortalOrder): string {
  if (order.delivered_at) return dateTimeZone(order.delivered_at);
  return formatWindow(order.window_start, order.window_end);
}

/**
 * `deliveryText` split for the two-line table cell: the day, then the rest
 * ("Fri 9 Oct" / "9:00 AM – 1:00 PM CDT"). A whole-day window has no rest.
 */
export function deliveryParts(order: PortalOrder): {
  day: string;
  rest: string;
} {
  const text = deliveryText(order);
  const at = text.indexOf(", ");
  if (at < 0) return { day: text, rest: "" };
  return { day: text.slice(0, at), rest: text.slice(at + 2) };
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
    <p
      className="min-w-0 truncate text-[15px] font-semibold leading-5 text-text"
      title={`${title} · ${quantityText(order, unit)}`}
    >
      {title} · <QuantityCell order={order} unit={unit} />
    </p>
    <PortalStatus
      kind="order"
      code={order.status_code}
      label={order.status_label}
    />
    <p
      className="col-span-2 col-start-2 min-w-0 truncate text-sm leading-5 text-text-muted"
      title={secondaryLine(order)}
    >
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
