"use client";

/**
 * The customer's orders from every channel, newest first (R4.11, PD11,
 * R14.9): two-line rows below 1024 px, a table from 1024 px.
 *
 * Rows with `cancellable` get "Cancel request" (R4.10, PD8): no confirm step,
 * like staff Decline. Success, or a 409 ORDER_NOT_CANCELLABLE, reloads the
 * list in place, announces the outcome in a polite live region and puts focus
 * back on the row (or the list if the row is gone). Other errors show in the
 * alert banner. Names and announcements use the tank and date, never the
 * order id (D29).
 *
 * `/portal/orders/new?tank=` renders this page with the request dialog open
 * (D24); closing it returns to `/portal/orders` and focuses the `<h1>`.
 */
import { ClipboardList, Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelPortalOrder,
  hasErrorCode,
  listPortalOrders,
  type PortalOrder,
} from "../../services/portalApi";
import { ProductCap } from "../ui/ProductChip";
import LiveRegion from "./LiveRegion";
import { REQUEST_CHANGED_MESSAGE, requestCancelledMessage } from "./messages";
import OrderRow, { deliveryText, orderRef, quantityText } from "./OrderRow";
import {
  PortalBanner,
  PortalEmpty,
  PortalLoading,
  PortalSectionError,
} from "./PageState";
import { usePortalMe } from "./PortalContext";
import PortalStatus from "./PortalStatus";
import PortalTable, { type PortalColumn } from "./PortalTable";
import PortalTitleRow from "./PortalTitleRow";
import { productName } from "./portalFormat";
import RequestDeliveryDialog from "./RequestDeliveryDialog";
import {
  listSection,
  primaryButton,
  rowButton,
  secondaryButton,
  space,
  titleActionButton,
} from "./styles";
import { orderTankTitle } from "./tankTitle";
import { PORTAL_TABLES, useMediaQuery } from "./useMediaQuery";
import { usePagedList } from "./usePagedList";
import { portalErrorMessage } from "./usePortalData";
import { usePortalTanks, useRequestDialog } from "./usePortalTanks";

export default function PortalOrdersView({
  initialDialog,
}: {
  /** Set by `/portal/orders/new`: open the dialog over the list. */
  initialDialog?: { open: boolean; tankId?: string | null };
} = {}) {
  const me = usePortalMe();
  const router = useRouter();
  const unit = me.measurement_units.volume;
  const wide = useMediaQuery(PORTAL_TABLES);
  const tanks = usePortalTanks();
  const dialog = useRequestDialog(initialDialog);
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
  const listRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const rowRefs = useRef(new Map<string, HTMLElement>());
  const setRowRef = useCallback((id: string, el: HTMLElement | null) => {
    if (el) rowRefs.current.set(id, el);
    else rowRefs.current.delete(id);
  }, []);

  const titleOf = useCallback(
    (o: PortalOrder) => orderTankTitle(o.tank, o.product_code, tanks.titles),
    [tanks.titles],
  );

  // After a cancel reload, put focus back on the row, or on the list if the
  // row is no longer on the first page.
  useEffect(() => {
    if (focusId === null) return;
    (rowRefs.current.get(focusId) ?? listRef.current)?.focus();
    setFocusId(null);
  }, [focusId]);

  const handleCancel = useCallback(
    async (order: PortalOrder) => {
      if (pendingId !== null) return;
      const orderId = order.order_id;
      setPendingId(orderId);
      setNotice(null);
      setCancelError(null);
      let message: string;
      try {
        await cancelPortalOrder(orderId);
        message = requestCancelledMessage(orderRef(order, titleOf(order)));
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
    [pendingId, refresh, titleOf],
  );

  const closeDialog = useCallback(() => {
    dialog.close();
    if (initialDialog?.open) {
      // Deep link: drop `/new?tank=` and land on the list heading.
      router.replace("/portal/orders");
      setTimeout(() => headingRef.current?.focus(), 0);
    }
  }, [dialog, initialDialog?.open, router]);

  const onCreated = useCallback(() => {
    void refresh().catch(() => list.reload());
  }, [refresh, list]);

  const columns: PortalColumn<PortalOrder>[] = [
    {
      key: "tank",
      header: "Tank and product",
      cell: (o) => (
        <span className="inline-flex items-center gap-2">
          {o.product_code && <ProductCap code={o.product_code} decorative />}
          <span className="font-semibold text-text">{titleOf(o)}</span>
          {o.product_code && (
            <span className="text-text-muted">
              · {productName(o.product_code)}
            </span>
          )}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (o) => (
        <PortalStatus
          kind="order"
          code={o.status_code}
          label={o.status_label}
        />
      ),
    },
    { key: "delivery", header: "Delivery", cell: (o) => deliveryText(o) },
    {
      key: "quantity",
      header: "Quantity",
      align: "right",
      cell: (o) => quantityText(o, unit),
    },
    { key: "po", header: "PO", cell: (o) => o.po_number ?? "—" },
    { key: "ticket", header: "Ticket", cell: (o) => o.ticket_number ?? "—" },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      cell: (o) =>
        o.cancellable ? (
          <button
            type="button"
            className={rowButton}
            aria-label={`Cancel request for ${orderRef(o, titleOf(o))}`}
            aria-disabled={pendingId !== null ? true : undefined}
            onClick={() => void handleCancel(o)}
          >
            Cancel request
          </button>
        ) : null,
    },
  ];

  return (
    <>
      <PortalTitleRow
        ref={headingRef}
        title="Orders"
        action={
          <button
            type="button"
            className={titleActionButton}
            onClick={() => dialog.openFor(null)}
          >
            <Plus aria-hidden="true" className="h-4 w-4" />
            Request delivery
          </button>
        }
      />
      <div className="space-y-3">
        <LiveRegion message={notice} />
        {cancelError && (
          <PortalBanner tone="critical">{cancelError}</PortalBanner>
        )}
        {list.loading ? (
          <div data-portal-first className={`${listSection} ${space.inset}`}>
            <PortalLoading label="Loading your orders…" rows={5} />
          </div>
        ) : list.error && list.items.length === 0 ? (
          <div data-portal-first className={`${listSection} ${space.inset}`}>
            <PortalSectionError
              message={portalErrorMessage(list.error, {
                fallback: "We couldn't load your orders.",
              })}
              onRetry={list.reload}
            />
          </div>
        ) : list.items.length === 0 ? (
          <div data-portal-first className={listSection}>
            <PortalEmpty
              icon={<ClipboardList className="h-8 w-8" />}
              title="No orders yet."
              description="Your deliveries will show here."
              action={
                <button
                  type="button"
                  className={primaryButton}
                  onClick={() => dialog.openFor(null)}
                >
                  Request delivery
                </button>
              }
            />
          </div>
        ) : (
          <div
            ref={listRef}
            tabIndex={-1}
            data-portal-first
            className={`${listSection} md:overflow-clip focus:outline-none focus-visible:ring-2 focus-visible:ring-focus`}
          >
            {wide ? (
              <PortalTable
                caption="Your orders"
                columns={columns}
                rows={list.items}
                rowKey={(o) => o.order_id}
                rowRef={setRowRef}
              />
            ) : (
              <ul aria-label="Your orders">
                {list.items.map((order) => (
                  <OrderRow
                    key={order.order_id}
                    ref={(el) => setRowRef(order.order_id, el)}
                    order={order}
                    title={titleOf(order)}
                    unit={unit}
                    onCancel={(o) => void handleCancel(o)}
                    cancelDisabled={pendingId !== null}
                  />
                ))}
              </ul>
            )}
          </div>
        )}
        {Boolean(list.error) && list.items.length > 0 && (
          <PortalBanner tone="critical">
            {portalErrorMessage(list.error, {
              fallback: "We couldn't load more orders.",
            })}
          </PortalBanner>
        )}
        {list.hasMore && !list.loading && (
          <div className="flex justify-center">
            <button
              type="button"
              className={secondaryButton}
              onClick={list.loadMore}
              aria-disabled={list.loadingMore ? true : undefined}
            >
              {list.loadingMore ? "Loading…" : "Show more orders"}
            </button>
          </div>
        )}
      </div>

      <RequestDeliveryDialog
        open={dialog.open}
        onClose={closeDialog}
        tanks={tanks.tanks}
        titles={tanks.titles}
        tanksLoading={tanks.loading}
        tanksError={tanks.error}
        onRetryTanks={tanks.reload}
        orderingAvailable={me.ordering_available}
        supplierName={me.supplier_name}
        unit={unit}
        initialTankId={dialog.tankId}
        onCreated={onCreated}
      />
    </>
  );
}
