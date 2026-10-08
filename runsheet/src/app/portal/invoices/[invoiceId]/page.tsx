"use client";

/** Invoice detail with PDF download and Pay (R5.2, R5.3, R6.13). */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import {
  formatCalendarDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatVolume,
} from "../../../../components/portal/format";
import LiveRegion from "../../../../components/portal/LiveRegion";
import {
  INVOICES_UNAVAILABLE_MESSAGE,
  PAYMENTS_UNAVAILABLE_MESSAGE,
} from "../../../../components/portal/messages";
import {
  PortalLoadError,
  PortalLoading,
} from "../../../../components/portal/PageState";
import { usePortalMe } from "../../../../components/portal/PortalContext";
import StatusText from "../../../../components/portal/StatusText";
import {
  card,
  pageHeading,
  primaryButton,
  secondaryButton,
  sectionHeading,
  textLink,
} from "../../../../components/portal/styles";
import {
  portalErrorMessage,
  usePortalData,
} from "../../../../components/portal/usePortalData";
import {
  downloadPortalInvoicePdf,
  getPortalInvoice,
  isRateLimited,
  rateLimitMessage,
} from "../../../../services/portalApi";

export default function PortalInvoiceDetailPage() {
  const params = useParams<{ invoiceId: string }>();
  const invoiceId = params?.invoiceId ?? "";
  const me = usePortalMe();
  const unit = me.measurement_units.volume;
  const invoice = usePortalData(() => getPortalInvoice(invoiceId), [invoiceId]);
  const [pdfMessage, setPdfMessage] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  const back = (
    <p>
      <Link href="/portal/invoices" className={textLink}>
        Back to your invoices
      </Link>
    </p>
  );

  if (!me.invoices_available) {
    return (
      <div className="space-y-3">
        {back}
        <h1 className={pageHeading}>Invoice</h1>
        <p className="text-sm text-gray-800">{INVOICES_UNAVAILABLE_MESSAGE}</p>
      </div>
    );
  }

  if (invoice.loading) return <PortalLoading label="Loading the invoice…" />;
  if (invoice.error || !invoice.data) {
    return (
      <div className="space-y-3">
        {back}
        <h1 className={pageHeading}>Invoice</h1>
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
  const title = inv.invoice_number
    ? `Invoice ${inv.invoice_number}`
    : "Invoice";

  const handlePdf = async () => {
    if (downloading) return;
    setDownloading(true);
    setPdfMessage("Preparing the PDF…");
    try {
      await downloadPortalInvoicePdf(inv.invoice_id);
      setPdfMessage("PDF downloaded.");
    } catch (error) {
      setPdfMessage(
        isRateLimited(error)
          ? rateLimitMessage(error)
          : "The PDF didn't download. Please try again.",
      );
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="space-y-6">
      {back}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className={pageHeading}>{title}</h1>
        <StatusText code={inv.status_code} label={inv.status_label} />
      </div>

      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          className={secondaryButton}
          onClick={handlePdf}
          aria-disabled={downloading ? true : undefined}
        >
          Download PDF
        </button>
        {inv.payable && me.payments_available && (
          <Link
            href={`/portal/invoices/${encodeURIComponent(inv.invoice_id)}/pay`}
            className={primaryButton}
          >
            Pay
          </Link>
        )}
      </div>
      <LiveRegion message={pdfMessage} />
      {!me.payments_available && inv.payable && (
        <p className="text-sm text-gray-800">{PAYMENTS_UNAVAILABLE_MESSAGE}</p>
      )}

      {inv.payment_attempt && (
        <p className="text-sm text-gray-900">
          Latest payment: {inv.payment_attempt.status_label},{" "}
          {formatMoney(inv.payment_attempt.amount_cents)} on{" "}
          {formatDateTime(inv.payment_attempt.created_at)}
        </p>
      )}

      <section aria-labelledby="summary-heading" className={card}>
        <h2 id="summary-heading" className={sectionHeading}>
          Summary
        </h2>
        <dl className="mt-2 grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
          <div>
            <dt className="font-medium text-gray-700">Account</dt>
            <dd>{inv.account_display_name}</dd>
          </div>
          <div>
            <dt className="font-medium text-gray-700">Issued</dt>
            <dd>{formatCalendarDate(inv.issued_at)}</dd>
          </div>
          <div>
            <dt className="font-medium text-gray-700">Due</dt>
            <dd>{formatCalendarDate(inv.due_date)}</dd>
          </div>
          <div>
            <dt className="font-medium text-gray-700">Subtotal</dt>
            <dd>{formatMoney(inv.subtotal_cents)}</dd>
          </div>
          <div>
            <dt className="font-medium text-gray-700">Tax</dt>
            <dd>{formatMoney(inv.tax_cents)}</dd>
          </div>
          <div>
            <dt className="font-medium text-gray-700">Total</dt>
            <dd>{formatMoney(inv.total_cents)}</dd>
          </div>
          <div>
            <dt className="font-medium text-gray-700">Paid</dt>
            <dd>{formatMoney(inv.amount_paid_cents)}</dd>
          </div>
          <div>
            <dt className="font-medium text-gray-700">Balance due</dt>
            <dd className="font-semibold">
              {formatMoney(inv.remaining_cents)}
            </dd>
          </div>
        </dl>
      </section>

      {inv.delivery && (
        <section aria-labelledby="delivery-heading" className={card}>
          <h2 id="delivery-heading" className={sectionHeading}>
            Delivery
          </h2>
          <dl className="mt-2 grid grid-cols-1 gap-2 text-sm sm:grid-cols-3">
            <div>
              <dt className="font-medium text-gray-700">Delivered</dt>
              <dd>{formatDateTime(inv.delivery.delivered_at)}</dd>
            </div>
            <div>
              <dt className="font-medium text-gray-700">Volume</dt>
              <dd>{formatVolume(inv.delivery.actual_gallons, unit)}</dd>
            </div>
            <div>
              <dt className="font-medium text-gray-700">Ticket</dt>
              <dd>{inv.delivery.ticket_number ?? "—"}</dd>
            </div>
          </dl>
        </section>
      )}

      <section aria-labelledby="lines-heading">
        <h2 id="lines-heading" className={sectionHeading}>
          Line items
        </h2>
        {inv.line_items.length === 0 ? (
          <p className="mt-2 text-sm text-gray-700">No line items.</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {inv.line_items.map((line, index) => (
              <li key={index} className={`${card} text-sm`}>
                <p className="font-medium text-gray-900">
                  {line.product_code ?? "Product"}
                </p>
                <p className="text-gray-800">
                  {formatNumber(line.quantity_gallons)} {unit} at{" "}
                  {formatMoney(line.unit_price_cents)} per {unit} ={" "}
                  {formatMoney(line.subtotal_cents)}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
