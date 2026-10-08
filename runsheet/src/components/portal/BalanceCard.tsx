"use client";

/**
 * Invoice detail lead card and the phone pay bar (D28, R14.13).
 *
 * - Balance due first: remaining amount, due date, status badge, primary Pay.
 * - While the latest payment attempt is creating, created or pending, Pay is
 *   replaced by the payment status (no tap that can only answer 409).
 * - Payments off on a payable invoice: the PD15 text instead of Pay.
 * - Below 640 px a sticky bar above the tab bar holds "Pay {amount}".
 */
import Link from "next/link";
import type { PortalInvoice } from "../../services/portalApi";
import { PAYMENTS_UNAVAILABLE_MESSAGE } from "./messages";
import PortalStatus from "./PortalStatus";
import { dateTime, date as formatDate, money } from "./portalFormat";
import { PAYMENT_IN_FLIGHT } from "./portalStatusMap";
import { card, primaryButton } from "./styles";

export type PayState = "pay" | "processing" | "unavailable" | "none";

export function payState(
  inv: PortalInvoice,
  paymentsAvailable: boolean,
): PayState {
  if (
    inv.payment_attempt &&
    PAYMENT_IN_FLIGHT.has(inv.payment_attempt.status_code)
  ) {
    return "processing";
  }
  if (!inv.payable) return "none";
  return paymentsAvailable ? "pay" : "unavailable";
}

export function payHref(inv: PortalInvoice) {
  return `/portal/invoices/${encodeURIComponent(inv.invoice_id)}/pay`;
}

export default function BalanceCard({
  invoice,
  paymentsAvailable,
}: {
  invoice: PortalInvoice;
  paymentsAvailable: boolean;
}) {
  const state = payState(invoice, paymentsAvailable);
  const attempt = invoice.payment_attempt;
  return (
    <section
      aria-labelledby="balance-due-heading"
      data-portal-first
      className={`${card} flex flex-wrap items-center gap-x-4 gap-y-2 p-4`}
    >
      <div className="min-w-0">
        <h2
          id="balance-due-heading"
          className="text-sm font-semibold text-text-muted"
        >
          Balance due
        </h2>
        <p className="text-[28px] font-extrabold leading-tight tracking-tight text-text">
          {money(invoice.remaining_cents)}
        </p>
        <p className="text-sm text-text-muted">
          Due {formatDate(invoice.due_date)} · of {money(invoice.total_cents)}
        </p>
      </div>
      <div className="ml-auto flex flex-col items-end gap-1.5">
        {state === "pay" && (
          <Link href={payHref(invoice)} className={primaryButton}>
            Pay
          </Link>
        )}
        {state === "processing" && attempt && (
          <>
            <PortalStatus
              kind="payment"
              code={attempt.status_code}
              label={attempt.status_label}
            />
            <p className="text-sm text-text-muted">
              {money(attempt.amount_cents)} started{" "}
              {dateTime(attempt.created_at)}
            </p>
          </>
        )}
      </div>
      {state === "unavailable" && (
        <p className="w-full text-sm text-text">
          {PAYMENTS_UNAVAILABLE_MESSAGE}
        </p>
      )}
    </section>
  );
}

/** Sticky "Pay {amount}" bar on phones (< 640 px), above the tab bar. */
export function PayBar({ invoice }: { invoice: PortalInvoice }) {
  return (
    <div
      data-portal-paybar
      className="fixed inset-x-0 bottom-[calc(64px+env(safe-area-inset-bottom))] z-30 border-t border-border bg-surface px-4 py-2"
    >
      <Link href={payHref(invoice)} className={`${primaryButton} w-full`}>
        Pay {money(invoice.remaining_cents)}
      </Link>
    </div>
  );
}
