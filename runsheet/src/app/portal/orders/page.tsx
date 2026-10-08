"use client";

/** The customer's orders from every channel, newest first (R4.11, PD11). */

import Link from "next/link";
import { useCallback } from "react";
import {
  formatDateTime,
  formatVolume,
  formatWindow,
} from "../../../components/portal/format";
import {
  PortalLoadError,
  PortalLoading,
} from "../../../components/portal/PageState";
import { usePortalMe } from "../../../components/portal/PortalContext";
import StatusText from "../../../components/portal/StatusText";
import {
  card,
  pageHeading,
  primaryButton,
  secondaryButton,
} from "../../../components/portal/styles";
import { usePagedList } from "../../../components/portal/usePagedList";
import { portalErrorMessage } from "../../../components/portal/usePortalData";
import {
  listPortalOrders,
  type PortalOrder,
} from "../../../services/portalApi";

function quantityText(order: PortalOrder, unit: string): string {
  if (order.fill_to_full) return "Fill to full";
  return formatVolume(order.gallons_requested, unit);
}

export default function PortalOrdersPage() {
  const me = usePortalMe();
  const unit = me.measurement_units.volume;
  const fetchPage = useCallback(
    (cursor: string | null) => listPortalOrders({ cursor, limit: 25 }),
    [],
  );
  const list = usePagedList(fetchPage, [fetchPage]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className={pageHeading}>Your orders</h1>
        <Link href="/portal/orders/new" className={primaryButton}>
          Request delivery
        </Link>
      </div>

      {list.loading ? (
        <PortalLoading label="Loading your orders…" />
      ) : list.error && list.items.length === 0 ? (
        <PortalLoadError
          message={portalErrorMessage(list.error, {
            fallback: "We couldn't load your orders.",
          })}
          onRetry={list.reload}
        />
      ) : list.items.length === 0 ? (
        <p className="text-sm text-gray-700">No orders yet.</p>
      ) : (
        <ul className="space-y-3">
          {list.items.map((order) => (
            <li key={order.order_id} className={card}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-medium text-gray-900">
                  {order.tank?.label ?? "Tank"} · {order.product_code ?? "—"}
                </p>
                <StatusText
                  code={order.status_code}
                  label={order.status_label}
                />
              </div>
              <dl className="mt-2 grid grid-cols-1 gap-1 text-sm sm:grid-cols-2">
                <div>
                  <dt className="inline font-medium text-gray-700">
                    Quantity:{" "}
                  </dt>
                  <dd className="inline">{quantityText(order, unit)}</dd>
                </div>
                <div>
                  <dt className="inline font-medium text-gray-700">
                    Delivery:{" "}
                  </dt>
                  <dd className="inline">
                    {formatWindow(order.window_start, order.window_end)}
                  </dd>
                </div>
                {order.po_number && (
                  <div>
                    <dt className="inline font-medium text-gray-700">PO: </dt>
                    <dd className="inline">{order.po_number}</dd>
                  </div>
                )}
                <div>
                  <dt className="inline font-medium text-gray-700">
                    Requested:{" "}
                  </dt>
                  <dd className="inline">{formatDateTime(order.created_at)}</dd>
                </div>
                {order.delivered_at && (
                  <div>
                    <dt className="inline font-medium text-gray-700">
                      Delivered:{" "}
                    </dt>
                    <dd className="inline">
                      {formatDateTime(order.delivered_at)}
                      {order.delivered_gallons !== null
                        ? `, ${formatVolume(order.delivered_gallons, unit)}`
                        : ""}
                      {order.ticket_number
                        ? ` (ticket ${order.ticket_number})`
                        : ""}
                    </dd>
                  </div>
                )}
              </dl>
            </li>
          ))}
        </ul>
      )}

      {Boolean(list.error) && list.items.length > 0 && (
        <p role="alert" className="text-sm text-gray-900">
          {portalErrorMessage(list.error, {
            fallback: "We couldn't load more orders.",
          })}
        </p>
      )}
      {list.hasMore && !list.loading && (
        <button
          type="button"
          className={secondaryButton}
          onClick={list.loadMore}
          aria-disabled={list.loadingMore ? true : undefined}
        >
          {list.loadingMore ? "Loading…" : "Show more orders"}
        </button>
      )}
    </div>
  );
}
