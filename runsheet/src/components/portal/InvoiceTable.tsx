/**
 * Invoice list (R5.1). A real table from 640 px up; below that each invoice
 * is a stacked definition list, so nothing scrolls sideways at 320 px.
 * Exactly one of the two is displayed at a time (the other is
 * `display: none`, which also hides it from assistive technology).
 */

import Link from "next/link";
import type { PortalInvoice } from "../../services/portalApi";
import { formatCalendarDate, formatMoney } from "./format";
import StatusText from "./StatusText";
import { textLink } from "./styles";

function invoiceName(invoice: PortalInvoice): string {
  return invoice.invoice_number
    ? `Invoice ${invoice.invoice_number}`
    : "Invoice";
}

function InvoiceLink({ invoice }: { invoice: PortalInvoice }) {
  return (
    <Link
      href={`/portal/invoices/${encodeURIComponent(invoice.invoice_id)}`}
      className={textLink}
    >
      {invoiceName(invoice)}
    </Link>
  );
}

export default function InvoiceTable({
  invoices,
  caption = "Invoices",
}: {
  invoices: PortalInvoice[];
  caption?: string;
}) {
  if (invoices.length === 0) {
    return <p className="text-sm text-gray-700">No invoices to show.</p>;
  }
  return (
    <>
      <table className="hidden w-full border-collapse text-left text-sm sm:table">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-gray-200 text-gray-700">
            <th scope="col" className="py-2 pr-3 font-medium">
              Invoice
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Issued
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Due
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Status
            </th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">
              Total
            </th>
            <th scope="col" className="py-2 text-right font-medium">
              Balance due
            </th>
          </tr>
        </thead>
        <tbody>
          {invoices.map((invoice) => (
            <tr key={invoice.invoice_id} className="border-b border-gray-100">
              <td className="py-2 pr-3">
                <InvoiceLink invoice={invoice} />
                <span className="block text-gray-700">
                  {invoice.account_display_name}
                </span>
              </td>
              <td className="py-2 pr-3">
                {formatCalendarDate(invoice.issued_at)}
              </td>
              <td className="py-2 pr-3">
                {formatCalendarDate(invoice.due_date)}
              </td>
              <td className="py-2 pr-3">
                <StatusText
                  code={invoice.status_code}
                  label={invoice.status_label}
                />
              </td>
              <td className="py-2 pr-3 text-right">
                {formatMoney(invoice.total_cents)}
              </td>
              <td className="py-2 text-right">
                {formatMoney(invoice.remaining_cents)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <ul className="space-y-3 sm:hidden" aria-label={caption}>
        {invoices.map((invoice) => (
          <li
            key={invoice.invoice_id}
            className="rounded-xl border border-gray-200 bg-white p-4"
          >
            <InvoiceLink invoice={invoice} />
            <dl className="mt-2 grid grid-cols-1 gap-1 text-sm">
              <div>
                <dt className="inline font-medium text-gray-700">Account: </dt>
                <dd className="inline">{invoice.account_display_name}</dd>
              </div>
              <div>
                <dt className="inline font-medium text-gray-700">Issued: </dt>
                <dd className="inline">
                  {formatCalendarDate(invoice.issued_at)}
                </dd>
              </div>
              <div>
                <dt className="inline font-medium text-gray-700">Due: </dt>
                <dd className="inline">
                  {formatCalendarDate(invoice.due_date)}
                </dd>
              </div>
              <div>
                <dt className="inline font-medium text-gray-700">Status: </dt>
                <dd className="inline">
                  <StatusText
                    code={invoice.status_code}
                    label={invoice.status_label}
                  />
                </dd>
              </div>
              <div>
                <dt className="inline font-medium text-gray-700">Total: </dt>
                <dd className="inline">{formatMoney(invoice.total_cents)}</dd>
              </div>
              <div>
                <dt className="inline font-medium text-gray-700">
                  Balance due:{" "}
                </dt>
                <dd className="inline">
                  {formatMoney(invoice.remaining_cents)}
                </dd>
              </div>
            </dl>
          </li>
        ))}
      </ul>
    </>
  );
}
