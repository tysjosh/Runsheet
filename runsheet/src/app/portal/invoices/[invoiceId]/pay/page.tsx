"use client";

/** ACH payment for one invoice (R6, design §6.4). */

import Link from "next/link";
import { useParams } from "next/navigation";
import { formatMoney } from "../../../../../components/portal/format";
import { INVOICES_UNAVAILABLE_MESSAGE } from "../../../../../components/portal/messages";
import {
  PortalLoadError,
  PortalLoading,
} from "../../../../../components/portal/PageState";
import PaymentForm from "../../../../../components/portal/PaymentForm";
import { usePortalMe } from "../../../../../components/portal/PortalContext";
import { pageHeading, textLink } from "../../../../../components/portal/styles";
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

  const back = (
    <p>
      <Link
        href={`/portal/invoices/${encodeURIComponent(invoiceId)}`}
        className={textLink}
      >
        Back to the invoice
      </Link>
    </p>
  );

  if (!me.invoices_available) {
    return (
      <div className="space-y-3">
        {back}
        <h1 className={pageHeading}>Pay invoice</h1>
        <p className="text-sm text-gray-800">{INVOICES_UNAVAILABLE_MESSAGE}</p>
      </div>
    );
  }

  // The form keeps its own state; only the first load shows the spinner, so a
  // background reload after a terminal result doesn't unmount it.
  if (invoice.loading && !invoice.data) {
    return <PortalLoading label="Loading the invoice…" />;
  }
  if (!invoice.data) {
    return (
      <div className="space-y-3">
        {back}
        <h1 className={pageHeading}>Pay invoice</h1>
        <PortalLoadError
          message={portalErrorMessage(invoice.error, {
            notFound: "We couldn't find that invoice.",
            fallback: "We couldn't load the invoice.",
          })}
          onRetry={invoice.reload}
        />
      </div>
    );
  }

  const inv = invoice.data.data;
  return (
    <div className="space-y-6">
      {back}
      <div>
        <h1 className={pageHeading}>
          Pay {inv.invoice_number ? `invoice ${inv.invoice_number}` : "invoice"}
        </h1>
        <p className="mt-1 text-sm text-gray-800">
          Balance due: {formatMoney(inv.remaining_cents)}. Payments are made by
          bank transfer (ACH) and can take a few business days to clear.
        </p>
      </div>
      <PaymentForm
        invoice={inv}
        paymentsAvailable={me.payments_available}
        onSettled={invoice.reload}
      />
    </div>
  );
}
