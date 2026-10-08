"use client";

/**
 * Invoice detail (R5.2, R5.3, R6.13, R14.13, D28): Balance due first, then
 * the facts, the delivery and the line items. PDF is a secondary action.
 */
import { Download } from "lucide-react";
import { useParams } from "next/navigation";
import { useState } from "react";
import BalanceCard, {
  PayBar,
  payState,
} from "../../../../components/portal/BalanceCard";
import { invoiceName } from "../../../../components/portal/InvoiceTable";
import LiveRegion from "../../../../components/portal/LiveRegion";
import { INVOICES_UNAVAILABLE_MESSAGE } from "../../../../components/portal/messages";
import {
  PortalBanner,
  PortalLoading,
  PortalSectionError,
} from "../../../../components/portal/PageState";
import { usePortalMe } from "../../../../components/portal/PortalContext";
import PortalStatus from "../../../../components/portal/PortalStatus";
import PortalTitleRow from "../../../../components/portal/PortalTitleRow";
import {
  dateTime,
  deliveredVolume,
  date as formatDate,
  money,
  unitPrice,
} from "../../../../components/portal/portalFormat";
import {
  card,
  listSection,
  secondaryButton,
  sectionHeading,
  space,
} from "../../../../components/portal/styles";
import {
  PORTAL_PHONE,
  useMediaQuery,
} from "../../../../components/portal/useMediaQuery";
import {
  portalErrorMessage,
  usePortalData,
} from "../../../../components/portal/usePortalData";
import { ProductChip } from "../../../../components/ui/ProductChip";
import {
  downloadPortalInvoicePdf,
  getPortalInvoice,
  isRateLimited,
  type PortalInvoice,
  rateLimitMessage,
} from "../../../../services/portalApi";

const BACK = { href: "/portal/invoices", label: "Back to your invoices" };

function Fact({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-slate-100 py-2 sm:block sm:border-0 sm:py-0">
      <dt className="text-sm text-text-muted">{term}</dt>
      <dd className="text-[15px] font-medium text-text sm:mt-0.5">
        {children}
      </dd>
    </div>
  );
}

function linePrice(line: PortalInvoice["line_items"][number]): string {
  if (line.unit_price_dollars) return unitPrice(line.unit_price_dollars);
  return money(line.unit_price_cents);
}

export default function PortalInvoiceDetailPage() {
  const params = useParams<{ invoiceId: string }>();
  const invoiceId = params?.invoiceId ?? "";
  const me = usePortalMe();
  const unit = me.measurement_units.volume;
  const phone = useMediaQuery(PORTAL_PHONE);
  const invoice = usePortalData(() => getPortalInvoice(invoiceId), [invoiceId]);
  const [pdfMessage, setPdfMessage] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  if (!me.invoices_available) {
    return (
      <>
        <PortalTitleRow title="Invoice" back={BACK} />
        <div data-portal-first>
          <PortalBanner tone="info">
            {INVOICES_UNAVAILABLE_MESSAGE}
          </PortalBanner>
        </div>
      </>
    );
  }
  if (invoice.loading) {
    return (
      <>
        <PortalTitleRow title="Invoice" back={BACK} />
        <div data-portal-first className={`${card} px-4`}>
          <PortalLoading label="Loading the invoice…" rows={4} />
        </div>
      </>
    );
  }
  if (invoice.error || !invoice.data) {
    return (
      <>
        <PortalTitleRow title="Invoice" back={BACK} />
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
  const showPayBar = phone && payState(inv, me.payments_available) === "pay";

  return (
    <>
      <PortalTitleRow
        title={invoiceName(inv)}
        wrapTitle
        back={BACK}
        badge={
          <PortalStatus
            kind="invoice"
            code={inv.status_code}
            label={inv.status_label}
          />
        }
        action={
          <button
            type="button"
            className={secondaryButton}
            onClick={() => void handlePdf()}
            aria-disabled={downloading ? true : undefined}
          >
            <Download aria-hidden="true" className="h-4 w-4" />
            <span className="max-sm:sr-only">Download PDF</span>
          </button>
        }
      />
      <div className={`space-y-4 ${showPayBar ? "pb-16" : ""}`}>
        <LiveRegion message={pdfMessage} />
        <BalanceCard invoice={inv} paymentsAvailable={me.payments_available} />

        <section aria-labelledby="facts-heading" className={`${card} p-4`}>
          <h2 id="facts-heading" className="sr-only">
            Invoice details
          </h2>
          <dl className="grid grid-cols-1 gap-x-6 sm:grid-cols-2 sm:gap-y-3">
            <Fact term="Account">{inv.account_display_name}</Fact>
            <Fact term="Issued">{formatDate(inv.issued_at)}</Fact>
            <Fact term="Due">{formatDate(inv.due_date)}</Fact>
            <Fact term="Subtotal">{money(inv.subtotal_cents)}</Fact>
            <Fact term="Tax">{money(inv.tax_cents)}</Fact>
            <Fact term="Total">{money(inv.total_cents)}</Fact>
            <Fact term="Paid">{money(inv.amount_paid_cents)}</Fact>
            {inv.payment_attempt && (
              <Fact term="Latest payment">
                {inv.payment_attempt.status_label},{" "}
                {money(inv.payment_attempt.amount_cents)},{" "}
                {dateTime(inv.payment_attempt.created_at)}
              </Fact>
            )}
          </dl>
        </section>

        {inv.delivery && (
          <section aria-labelledby="delivery-heading" className={`${card} p-4`}>
            <h2 id="delivery-heading" className={sectionHeading}>
              Delivery
            </h2>
            <dl className="mt-2 grid grid-cols-1 gap-x-6 sm:grid-cols-3">
              <Fact term="Delivered">
                {dateTime(inv.delivery.delivered_at)}
              </Fact>
              <Fact term="Volume">
                {deliveredVolume(inv.delivery.actual_gallons, unit)}
              </Fact>
              <Fact term="Ticket">{inv.delivery.ticket_number ?? "—"}</Fact>
            </dl>
          </section>
        )}

        <section aria-labelledby="lines-heading" className={listSection}>
          <h2
            id="lines-heading"
            className={`${sectionHeading} md:px-4 md:pt-3`}
          >
            Line items
          </h2>
          {inv.line_items.length === 0 ? (
            <p className="py-3 text-sm text-text-muted md:px-4">
              No line items.
            </p>
          ) : (
            <ul className="mt-1">
              {inv.line_items.map((line, index) => (
                <li
                  // Line items have no id in the projection; the order is stable.
                  key={index}
                  className={`flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-slate-100 first:border-t-0 md:px-4 ${space.rowY}`}
                >
                  {line.product_code ? (
                    <ProductChip code={line.product_code} size="md" />
                  ) : (
                    <span className="text-[15px] font-medium text-text">
                      Product
                    </span>
                  )}
                  <span className="text-sm text-text-muted">
                    {deliveredVolume(line.quantity_gallons, unit)} ×{" "}
                    {linePrice(line)}
                  </span>
                  <span className="ml-auto text-[15px] font-semibold tabular-nums text-text">
                    {money(line.subtotal_cents)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      {showPayBar && <PayBar invoice={inv} />}
    </>
  );
}
