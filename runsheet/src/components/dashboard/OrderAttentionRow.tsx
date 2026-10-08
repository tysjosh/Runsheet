"use client";

/**
 * An order in "Needs attention" (moved from the Today cockpit, UI revamp
 * §7.1). The customer name opens the order; the action depends on state:
 *
 * - on hold → Release (re-runs the intake hooks server-side);
 * - placed, board enabled → "Assign on board" (the board opens with the order
 *   selected, ready to place on a truck);
 * - placed, no board → inline Assign with the driver picker (as before).
 */
import Link from "next/link";
import { useState } from "react";
import { gallons, productName, window as timeWindow } from "../../lib/format";
import { ApiError } from "../../services/api";
import {
  assignDriver,
  type FuelOrder,
  releaseHoldOrder,
} from "../../services/ordersApi";
import DriverPicker from "../ops/DriverPicker";
import { Button, ProductCap, StatusBadge } from "../ui";
import { notify } from "../ui/toast/notify";
import { LINKS } from "./dashboardModel";

export interface OrderAttentionRowProps {
  order: FuelOrder;
  /** Board deep link for "Assign on board"; null without the board. */
  boardHref: string | null;
  onActioned: (orderId: string) => void;
  onReload: () => void;
}

function orderDetail(o: FuelOrder): string {
  const parts: string[] = [];
  if (o.fill_to_full) parts.push("Fill");
  else if (typeof o.gallons_requested === "number")
    parts.push(gallons(o.gallons_requested));
  if (o.product_code) parts.push(productName(o.product_code));
  if (o.status === "on_hold" && o.hold_reason) parts.push(o.hold_reason);
  else if (o.delivery_window_start || o.delivery_window_end)
    parts.push(
      `window ${timeWindow(o.delivery_window_start, o.delivery_window_end)}`,
    );
  return parts.join(" · ");
}

export function OrderAttentionRow({
  order,
  boardHref,
  onActioned,
  onReload,
}: OrderAttentionRowProps) {
  const [assigning, setAssigning] = useState(false);
  const [driverId, setDriverId] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const isOnHold = order.status === "on_hold";
  const canAssign = !isOnHold && !order.assigned_driver_id;
  const name = order.customer_name || order.customer_id;

  const handleAssign = async () => {
    if (!driverId) return;
    setWorking(true);
    try {
      await assignDriver(order.order_id, { driver_id: driverId });
      notify({ type: "success", message: "Driver assigned" });
      onActioned(order.order_id);
    } catch (err) {
      notify({
        type: "error",
        message:
          err instanceof ApiError ? err.message : "Failed to assign driver",
      });
    } finally {
      setWorking(false);
    }
  };

  const handleRelease = async () => {
    setWorking(true);
    try {
      const res = await releaseHoldOrder(order.order_id);
      if (res.status === "on_hold") {
        notify({
          type: "error",
          message: `Still on hold: ${res.hold_reason ?? "intake check failed"}`,
        });
        onReload();
      } else {
        notify({ type: "success", message: "Hold released" });
        onActioned(order.order_id);
      }
    } catch (err) {
      notify({
        type: "error",
        message:
          err instanceof ApiError ? err.message : "Failed to release hold",
      });
    } finally {
      setWorking(false);
    }
  };

  return (
    <li data-feed-row className="border-b border-slate-100 last:border-b-0">
      <div className="flex min-h-11 items-center gap-2.5 px-3">
        {order.product_code ? (
          <ProductCap code={order.product_code} />
        ) : (
          <span className="h-5 w-5 shrink-0" aria-hidden="true" />
        )}
        {isOnHold ? (
          <StatusBadge status="delayed" label="On hold" />
        ) : (
          <StatusBadge status="draft" label="Placed" />
        )}
        <span className="min-w-0 flex-1 truncate text-sm">
          <Link
            href={LINKS.order(order.order_id)}
            className="font-semibold text-slate-900 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            {name}
          </Link>
          <span className="text-xs text-text-muted">
            {" "}
            · {orderDetail(order)}
          </span>
        </span>
        {isOnHold ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={handleRelease}
            loading={working}
          >
            Release
          </Button>
        ) : canAssign && boardHref ? (
          <Link
            href={boardHref}
            className="inline-flex h-7 shrink-0 items-center rounded-lg border border-slate-300 bg-surface px-2.5 text-xs font-semibold text-slate-800 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            Assign on board
          </Link>
        ) : canAssign ? (
          <Button
            variant="secondary"
            size="sm"
            aria-expanded={assigning}
            onClick={() => setAssigning((v) => !v)}
          >
            Assign
          </Button>
        ) : null}
      </div>
      {assigning && canAssign && !boardHref && (
        <div className="flex items-center gap-2 px-3 pb-2 pl-10">
          <div className="flex-1">
            <DriverPicker
              value={driverId}
              onChange={setDriverId}
              aria-label="Driver"
            />
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={handleAssign}
            loading={working}
            disabled={!driverId}
          >
            Confirm
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setAssigning(false)}>
            Cancel
          </Button>
        </div>
      )}
    </li>
  );
}

export default OrderAttentionRow;
