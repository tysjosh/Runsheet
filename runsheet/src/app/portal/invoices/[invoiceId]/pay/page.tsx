"use client";

/**
 * ACH payment for one invoice (R6, design §6.4, R14.14, D26): stays a page
 * (Stripe Financial Connections opens its own overlay). Only the look
 * changed; `PaymentForm`'s flow, idempotency and polling are unchanged.
 */
import { useParams } from "next/navigation";
import { INVOICES_UNAVAILABLE_MESSAGE } from "../../../../../components/portal/messages";
import {
  PortalBanner,
  PortalLoading,
  PortalSectionError,
} from "../../../../../components/portal/PageState";
import PaymentForm from "../../../../../components/portal/PaymentForm";
import { usePortalMe } from "../../../../../components/portal/PortalContext";
import PortalTitleRow from "../../../../../components/portal/PortalTitleRow";
import {
  date as formatDate,
  money,
} from "../../../../../components/portal/portalFormat";
import { card } from "../../../../../components/portal/styles";
import {
  portalErrorMessage,
  usePortalData,
} from "../../../../../components/portal/usePortalData";
import { getPortalInvoice } from "../../../../../services/portalApi";

export default function PortalPayInvoicePage() {
  const params = useParams<{ invoiceId: string }>();
  const invoiceId = params?.invoiceId ?? "";
  const me = usePortalMe();
  const invoice = usePortalData(() => getPortalInvoice(invoiceId), [invoiceId]);
  const back = {
    href: `/portal/invoices/${encodeURIComponent(invoiceId)}`,
    label: "Back to the invoice",
  };

  if (!me.invoices_available) {
    return (
      <>
        <PortalTitleRow title="Pay invoice" back={back} />
        <div data-portal-first>
          <PortalBanner tone="info">
            {INVOICES_UNAVAILABLE_MESSAGE}
          </PortalBanner>
        </div>
      </>
    );
  }
  // The form keeps its own state; only the first load shows the skeleton, so
  // a background reload after a terminal result doesn't unmount it.
  if (invoice.loading && !invoice.data) {
    return (
      <>
        <PortalTitleRow title="Pay invoice" back={back} />
        <div data-portal-first className={`${card} px-4`}>
          <PortalLoading label="Loading the invoice…" rows={3} />
        </div>
      </>
    );
  }
  if (!invoice.data) {
    return (
      <>
        <PortalTitleRow title="Pay invoice" back={back} />
        <div data-portal-first>
          <PortalSectionError
            message={portalErrorMessage(invoice.error, {
              notFound: "We couldn't find that invoice.",
              fallback: "We couldn't load the invoice.",
            })}
            onRetry={invoice.reload}
          />
        </div>
      </>
    );
  }
  const inv = invoice.data.data;
  return (
    <>
      <PortalTitleRow
        title={
          inv.invoice_number
            ? `Pay invoice ${inv.invoice_number}`
            : "Pay invoice"
        }
        back={back}
      />
      <div className="space-y-4">
        <section
          aria-labelledby="pay-balance"
          data-portal-first
          className={`${card} p-4`}
        >
          <h2
            id="pay-balance"
            className="text-sm font-semibold text-text-muted"
          >
            Balance due
          </h2>
          <p className="text-[28px] font-extrabold leading-tight text-text">
            {money(inv.remaining_cents)}
          </p>
          <p className="text-sm text-text-muted">
            Due {formatDate(inv.due_date)}. Payments are made by bank transfer
            (ACH) and can take a few business days to clear.
          </p>
        </section>
        <section className={`${card} p-4`} aria-label="Payment">
          <PaymentForm
            invoice={inv}
            paymentsAvailable={me.payments_available}
            onSettled={invoice.reload}
          />
        </section>
      </div>
    </>
  );
}
