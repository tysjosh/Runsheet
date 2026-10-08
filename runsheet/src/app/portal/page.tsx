"use client";

/**
 * Home (D19, R14.7, design §11.2): what the customer came for, in order.
 *
 * 1. Your tanks, ranked by level status (Order now, Order soon, OK), each with
 *    its level bar, forecast, next delivery and its own Request delivery.
 * 2. Balance due (when invoicing is on) with Pay when an invoice is payable.
 * 3. Active orders (not delivered, not delivered-failed, not cancelled).
 *
 * The customer name is only in the top bar. Each capability notice sits in
 * the panel it affects. Every panel loads, fails and retries on its own.
 */
import { Plus } from "lucide-react";
import Link from "next/link";
import { useCallback, useMemo } from "react";
import {
  ORDERING_UNAVAILABLE_MESSAGE,
  PAYMENTS_UNAVAILABLE_MESSAGE,
} from "../../components/portal/messages";
import OrderRow from "../../components/portal/OrderRow";
import {
  PortalBanner,
  PortalEmpty,
  PortalLoading,
  PortalSectionError,
} from "../../components/portal/PageState";
import { usePortalMe } from "../../components/portal/PortalContext";
import PortalPanel, { PANEL_ACCENT } from "../../components/portal/PortalPanel";
import PortalTitleRow from "../../components/portal/PortalTitleRow";
import { money } from "../../components/portal/portalFormat";
import { PAST_ORDER_CODES } from "../../components/portal/portalStatusMap";
import RequestDeliveryDialog from "../../components/portal/RequestDeliveryDialog";
import {
  primaryButton,
  space,
  titleActionButton,
} from "../../components/portal/styles";
import TankRow from "../../components/portal/TankRow";
import { sortTanks } from "../../components/portal/tankLevel";
import { orderTankTitle } from "../../components/portal/tankTitle";
import {
  portalErrorMessage,
  usePortalData,
} from "../../components/portal/usePortalData";
import {
  usePortalTanks,
  useRequestDialog,
} from "../../components/portal/usePortalTanks";
import {
  listPortalInvoices,
  listPortalOrders,
  type PortalInvoice,
  type PortalInvoiceStatus,
  type PortalMe,
} from "../../services/portalApi";

const BALANCE_STATUSES: PortalInvoiceStatus[] = ["open", "partial", "overdue"];

function plural(n: number, one: string, many: string) {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * The balance: the server's `open_balance_cents` (PE4) when sent, else the
 * client-side sum over the first 50 of each open status ("At least …").
 */
function useBalance(me: PortalMe) {
  const server = typeof me.open_balance_cents === "number";
  return usePortalData(async () => {
    if (!me.invoices_available) return null;
    const pages = await Promise.all(
      BALANCE_STATUSES.map((status) =>
        listPortalInvoices({ status, limit: server ? 10 : 50 }),
      ),
    );
    const seen = new Set<string>();
    const invoices: PortalInvoice[] = pages
      .flatMap((p) => p.data)
      .filter(
        (i) => !seen.has(i.invoice_id) && Boolean(seen.add(i.invoice_id)),
      );
    const payable = invoices.filter((i) => i.payable);
    if (server) {
      return {
        cents: me.open_balance_cents ?? 0,
        lowerBound: false,
        count: me.open_invoice_count ?? invoices.length,
        overdue: me.overdue_count ?? 0,
        payable,
      };
    }
    return {
      cents: invoices.reduce((sum, inv) => sum + inv.remaining_cents, 0),
      // A status with more than one page means the total is a lower bound.
      lowerBound: pages.some((p) => p.next_cursor),
      count: invoices.length,
      overdue: invoices.filter((i) => i.status_code === "overdue").length,
      payable,
    };
  }, [
    me.invoices_available,
    me.open_balance_cents,
    me.open_invoice_count,
    me.overdue_count,
  ]);
}

export default function PortalHomePage() {
  const me = usePortalMe();
  const unit = me.measurement_units.volume;
  const tanks = usePortalTanks();
  const dialog = useRequestDialog();
  const orders = usePortalData(
    () => listPortalOrders({ status_group: "active", limit: 5 }),
    [],
  );
  const balance = useBalance(me);
  const { reload: reloadOrders } = orders;
  const { reload: reloadTanks } = tanks;

  const ranked = useMemo(
    () =>
      sortTanks(
        tanks.tanks,
        (t) => tanks.titles.get(t.customer_tank_id) ?? t.label,
      ),
    [tanks.tanks, tanks.titles],
  );
  // PE7 filters on the server; the client filter keeps older APIs right.
  const active = (orders.data?.data ?? []).filter(
    (o) => !PAST_ORDER_CODES.has(o.status_code),
  );

  const onCreated = useCallback(() => {
    reloadOrders();
    reloadTanks();
  }, [reloadOrders, reloadTanks]);

  const request = (
    <button
      type="button"
      className={titleActionButton}
      onClick={() => dialog.openFor(null)}
    >
      <Plus aria-hidden="true" className="h-4 w-4" />
      Request delivery
    </button>
  );

  const b = balance.data;
  // A direct Pay link only when the one payable invoice is the whole open
  // set: the lists above are capped, so `count` (server or client) decides.
  const payTarget =
    b && b.payable.length === 1 && b.count <= 1 && !b.lowerBound
      ? {
          href: `/portal/invoices/${encodeURIComponent(b.payable[0].invoice_id)}/pay`,
          label: "Pay",
        }
      : b && b.payable.length > 0
        ? { href: "/portal/invoices", label: "View and pay" }
        : null;

  return (
    <>
      <PortalTitleRow title="Home" action={request} />
      <div
        className={`grid lg:grid-cols-[1.25fr_1fr] lg:items-start ${space.sectionGap}`}
      >
        <PortalPanel
          id="tanks-heading"
          title="Your tanks"
          accent={PANEL_ACCENT.tanks}
          link={{ href: "/portal/tanks", label: "All tanks" }}
          first
        >
          {!me.ordering_available && (
            <div className={`pt-2 ${space.inset}`}>
              <PortalBanner tone="warning">
                {ORDERING_UNAVAILABLE_MESSAGE}
              </PortalBanner>
            </div>
          )}
          {tanks.loading ? (
            <div className={space.inset}>
              <PortalLoading label="Loading your tanks…" />
            </div>
          ) : tanks.error ? (
            <div className={`pb-2 ${space.inset}`}>
              <PortalSectionError
                message={tanks.error}
                onRetry={tanks.reload}
              />
            </div>
          ) : ranked.length === 0 ? (
            <PortalEmpty
              title="No tanks are set up yet."
              description={`Contact ${me.supplier_name} to add one.`}
            />
          ) : (
            <ul className="divide-y divide-slate-100">
              {ranked.map((tank) => (
                <li key={tank.customer_tank_id}>
                  <TankRow
                    tank={tank}
                    title={
                      tanks.titles.get(tank.customer_tank_id) ?? tank.label
                    }
                    unit={unit}
                    onRequest={(id) => dialog.openFor(id)}
                    showProduct
                  />
                </li>
              ))}
            </ul>
          )}
        </PortalPanel>

        <div className={`flex flex-col ${space.sectionGap}`}>
          {me.invoices_available && (
            <PortalPanel
              id="balance-heading"
              title="Balance due"
              accent={PANEL_ACCENT.balance}
              link={{ href: "/portal/invoices", label: "Invoices" }}
            >
              {balance.loading ? (
                <div className={space.inset}>
                  <PortalLoading label="Loading your balance…" rows={2} />
                </div>
              ) : balance.error ? (
                <div className={`pb-2 ${space.inset}`}>
                  <PortalSectionError
                    message={portalErrorMessage(balance.error, {
                      fallback: "We couldn't load your balance.",
                    })}
                    onRetry={balance.reload}
                  />
                </div>
              ) : (
                <div
                  className={`flex flex-wrap items-center gap-3 ${space.inset} ${space.rowY}`}
                >
                  <div className="min-w-0">
                    <p className="text-[26px] font-extrabold leading-tight tracking-tight text-text">
                      {b?.lowerBound ? "At least " : ""}
                      {money(b?.cents ?? 0)}
                    </p>
                    <p className="text-sm text-text-muted">
                      {b && b.count > 0
                        ? `${plural(b.count, "invoice", "invoices")}${
                            b.overdue > 0 ? ` · ${b.overdue} overdue` : ""
                          }`
                        : "Nothing due"}
                    </p>
                  </div>
                  {me.payments_available
                    ? payTarget && (
                        <Link
                          href={payTarget.href}
                          className={`${primaryButton} ml-auto`}
                        >
                          {payTarget.label}
                        </Link>
                      )
                    : (b?.cents ?? 0) > 0 && (
                        <p className="w-full text-sm text-text">
                          {PAYMENTS_UNAVAILABLE_MESSAGE}
                        </p>
                      )}
                </div>
              )}
            </PortalPanel>
          )}

          <PortalPanel
            id="orders-heading"
            title="Active orders"
            accent={PANEL_ACCENT.orders}
            link={{ href: "/portal/orders", label: "All orders" }}
          >
            {orders.loading ? (
              <div className={space.inset}>
                <PortalLoading label="Loading your orders…" rows={2} />
              </div>
            ) : orders.error ? (
              <div className={`pb-2 ${space.inset}`}>
                <PortalSectionError
                  message={portalErrorMessage(orders.error, {
                    fallback: "We couldn't load your orders.",
                  })}
                  onRetry={orders.reload}
                />
              </div>
            ) : active.length === 0 ? (
              <PortalEmpty
                title="No active orders."
                action={
                  me.ordering_available ? (
                    <button
                      type="button"
                      className={primaryButton}
                      onClick={() => dialog.openFor(null)}
                    >
                      Request delivery
                    </button>
                  ) : undefined
                }
              />
            ) : (
              <ul aria-label="Active orders" className="mt-2">
                {active.map((order) => (
                  <OrderRow
                    key={order.order_id}
                    order={order}
                    title={orderTankTitle(
                      order.tank,
                      order.product_code,
                      tanks.titles,
                    )}
                    unit={unit}
                  />
                ))}
              </ul>
            )}
          </PortalPanel>
        </div>
      </div>

      <RequestDeliveryDialog
        open={dialog.open}
        onClose={dialog.close}
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
