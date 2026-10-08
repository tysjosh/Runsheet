"use client";

/**
 * Portal overview: account name, capability notices, tank summary, open
 * balance and recent requests (design §10.2).
 */

import Link from "next/link";
import { formatMoney, formatWindow } from "../../components/portal/format";
import {
  INVOICES_UNAVAILABLE_MESSAGE,
  ORDERING_UNAVAILABLE_MESSAGE,
  PAYMENTS_UNAVAILABLE_MESSAGE,
} from "../../components/portal/messages";
import {
  PortalLoadError,
  PortalLoading,
} from "../../components/portal/PageState";
import { usePortalMe } from "../../components/portal/PortalContext";
import StatusText from "../../components/portal/StatusText";
import {
  card,
  pageHeading,
  primaryButton,
  sectionHeading,
  textLink,
} from "../../components/portal/styles";
import { levelText } from "../../components/portal/TankCard";
import {
  portalErrorMessage,
  usePortalData,
} from "../../components/portal/usePortalData";
import {
  listPortalInvoices,
  listPortalOrders,
  listPortalTanks,
  type PortalInvoiceStatus,
} from "../../services/portalApi";

const BALANCE_STATUSES: PortalInvoiceStatus[] = ["open", "partial", "overdue"];

export default function PortalOverviewPage() {
  const me = usePortalMe();
  const unit = me.measurement_units.volume;

  const tanks = usePortalData(() => listPortalTanks(), []);
  const orders = usePortalData(() => listPortalOrders({ limit: 5 }), []);
  const balance = usePortalData(async () => {
    if (!me.invoices_available) return null;
    const pages = await Promise.all(
      BALANCE_STATUSES.map((status) =>
        listPortalInvoices({ status, limit: 50 }),
      ),
    );
    const cents = pages
      .flatMap((p) => p.data)
      .reduce((sum, inv) => sum + inv.remaining_cents, 0);
    // A status with more than one page means the total is a lower bound.
    return { cents, partial: pages.some((p) => p.next_cursor) };
  }, [me.invoices_available]);

  return (
    <div className="space-y-8">
      <div>
        <h1 className={pageHeading}>{me.customer_display_name}</h1>
        <p className="mt-1 text-sm text-gray-700">Signed in as {me.email}</p>
      </div>

      {(!me.ordering_available ||
        !me.invoices_available ||
        (me.invoices_available && !me.payments_available)) && (
        <section aria-labelledby="notices-heading" className={card}>
          <h2 id="notices-heading" className={sectionHeading}>
            Notices
          </h2>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-gray-800">
            {!me.ordering_available && <li>{ORDERING_UNAVAILABLE_MESSAGE}</li>}
            {!me.invoices_available && <li>{INVOICES_UNAVAILABLE_MESSAGE}</li>}
            {me.invoices_available && !me.payments_available && (
              <li>{PAYMENTS_UNAVAILABLE_MESSAGE}</li>
            )}
          </ul>
        </section>
      )}

      {me.invoices_available && (
        <section aria-labelledby="balance-heading">
          <h2 id="balance-heading" className={sectionHeading}>
            Open balance
          </h2>
          {balance.loading ? (
            <PortalLoading label="Loading your balance…" />
          ) : balance.error ? (
            <PortalLoadError
              message={portalErrorMessage(balance.error, {
                fallback: "We couldn't load your balance.",
              })}
              onRetry={balance.reload}
            />
          ) : (
            <p className="mt-2 text-2xl font-semibold text-gray-900">
              {balance.data?.partial ? "At least " : ""}
              {formatMoney(balance.data?.cents ?? 0)}
            </p>
          )}
          <p className="mt-2">
            <Link href="/portal/invoices" className={textLink}>
              View invoices
            </Link>
          </p>
        </section>
      )}

      <section aria-labelledby="tanks-heading">
        <h2 id="tanks-heading" className={sectionHeading}>
          Tanks
        </h2>
        {tanks.loading ? (
          <PortalLoading label="Loading your tanks…" />
        ) : tanks.error ? (
          <PortalLoadError
            message={portalErrorMessage(tanks.error, {
              fallback: "We couldn't load your tanks.",
            })}
            onRetry={tanks.reload}
          />
        ) : (tanks.data?.data.length ?? 0) === 0 ? (
          <p className="mt-2 text-sm text-gray-700">No tanks are set up yet.</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {tanks.data?.data.map((tank) => (
              <li key={tank.customer_tank_id} className="text-sm">
                <Link
                  href={`/portal/tanks/${encodeURIComponent(tank.customer_tank_id)}`}
                  className={textLink}
                >
                  {tank.label}
                </Link>
                {": "}
                {levelText(tank, unit)}
                {tank.reading_stale ? " (reading out of date)" : ""}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="recent-heading">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="recent-heading" className={sectionHeading}>
            Recent requests
          </h2>
          <Link href="/portal/orders/new" className={primaryButton}>
            Request delivery
          </Link>
        </div>
        {orders.loading ? (
          <PortalLoading label="Loading your orders…" />
        ) : orders.error ? (
          <PortalLoadError
            message={portalErrorMessage(orders.error, {
              fallback: "We couldn't load your orders.",
            })}
            onRetry={orders.reload}
          />
        ) : (orders.data?.data.length ?? 0) === 0 ? (
          <p className="mt-2 text-sm text-gray-700">No orders yet.</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {orders.data?.data.map((order) => (
              <li key={order.order_id} className="text-sm text-gray-900">
                <StatusText
                  code={order.status_code}
                  label={order.status_label}
                />{" "}
                {order.tank?.label ?? "Tank"},{" "}
                {formatWindow(order.window_start, order.window_end)}
              </li>
            ))}
          </ul>
        )}
        <p className="mt-2">
          <Link href="/portal/orders" className={textLink}>
            View all orders
          </Link>
        </p>
      </section>
    </div>
  );
}
