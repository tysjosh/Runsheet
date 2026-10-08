"use client";

/**
 * The customer's orders from every channel, newest first (R4.11, PD11).
 * Rows with `cancellable` get a "Cancel request" button (R4.10, PD8): no
 * confirm step, like staff Decline. Success, or a 409 ORDER_NOT_CANCELLABLE,
 * reloads the list in place, announces the outcome in a polite live region
 * and puts focus back on the row (or the list if the row is gone). Other
 * errors show in the standard alert banner.
 */

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  formatDateTime,
  formatVolume,
  formatWindow,
} from "../../../components/portal/format";
import LiveRegion from "../../../components/portal/LiveRegion";
import {
  REQUEST_CHANGED_MESSAGE,
  requestCancelledMessage,
} from "../../../components/portal/messages";
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
  cancelPortalOrder,
  hasErrorCode,
  listPortalOrders,
  type PortalOrder,
} from "../../../services/portalApi";

const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2";

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
  const { refresh } = list;
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const rowRefs = useRef(new Map<string, HTMLLIElement>());

  // After a cancel reload, put focus back on the row, or on the list if the
  // row is no longer on the first page.
  useEffect(() => {
    if (focusId === null) return;
    (rowRefs.current.get(focusId) ?? listRef.current)?.focus();
    setFocusId(null);
  }, [focusId]);

  const handleCancel = useCallback(
    async (orderId: string) => {
      if (pendingId !== null) return;
      setPendingId(orderId);
      setNotice(null);
      setCancelError(null);
      let message: string;
      try {
        await cancelPortalOrder(orderId);
        message = requestCancelledMessage(orderId);
      } catch (error) {
        if (!hasErrorCode(error, "ORDER_NOT_CANCELLABLE")) {
          setCancelError(
            portalErrorMessage(error, {
              fallback: "We couldn't cancel this request. Try again.",
            }),
          );
          setPendingId(null);
          return;
        }
        message = REQUEST_CHANGED_MESSAGE;
      }
      try {
        await refresh();
      } catch (error) {
        setCancelError(
          portalErrorMessage(error, {
            fallback: "We couldn't reload your orders.",
          }),
        );
      }
      setNotice(message);
      setPendingId(null);
      setFocusId(orderId);
    },
    [pendingId, refresh],
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className={pageHeading}>Your orders</h1>
        <Link href="/portal/orders/new" className={primaryButton}>
          Request delivery
        </Link>
      </div>

      <LiveRegion message={notice} />
      {cancelError && (
        <p role="alert" className="text-sm text-gray-900">
          {cancelError}
        </p>
      )}

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
        <ul
          ref={listRef}
          tabIndex={-1}
          aria-label="Your orders"
          className={`space-y-3 ${focusRing}`}
        >
          {list.items.map((order) => (
            <li
              key={order.order_id}
              ref={(el) => {
                if (el) rowRefs.current.set(order.order_id, el);
                else rowRefs.current.delete(order.order_id);
              }}
              tabIndex={-1}
              className={`${card} ${focusRing}`}
            >
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
              {order.cancellable && (
                <div className="mt-3">
                  <button
                    type="button"
                    className={`${secondaryButton} aria-disabled:cursor-not-allowed aria-disabled:opacity-60`}
                    aria-label={`Cancel request ${order.order_id}`}
                    aria-disabled={pendingId !== null ? true : undefined}
                    onClick={() => void handleCancel(order.order_id)}
                  >
                    Cancel request
                  </button>
                </div>
              )}
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
