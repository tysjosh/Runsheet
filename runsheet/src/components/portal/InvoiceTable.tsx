"use client";

/**
 * Invoice list (R5.1, R14.9, D27): two-line `InvoiceRow`s below 1024 px, a
 * `PortalTable` from 1024 px. Exactly one of the two is rendered.
 */
import Link from "next/link";
import type { PortalInvoice } from "../../services/portalApi";
import PortalStatus from "./PortalStatus";
import PortalTable, { type PortalColumn } from "./PortalTable";
import { date as formatDate, money } from "./portalFormat";
import { textLink } from "./styles";
import { PORTAL_TABLES, useMediaQuery } from "./useMediaQuery";

export function invoiceName(invoice: PortalInvoice): string {
  return invoice.invoice_number
    ? `Invoice ${invoice.invoice_number}`
    : "Invoice";
}

function href(invoice: PortalInvoice) {
  return `/portal/invoices/${encodeURIComponent(invoice.invoice_id)}`;
}

export function InvoiceRow({ invoice }: { invoice: PortalInvoice }) {
  return (
    <li
      data-invoice-row
      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-0.5 border-t border-slate-100 px-3.5 py-2.5 first:border-t-0"
    >
      <span className="flex min-w-0 items-center gap-2">
        <Link
          href={href(invoice)}
          className={`${textLink} min-h-6 truncate text-[15px] text-text`}
        >
          {invoiceName(invoice)}
        </Link>
        <PortalStatus
          kind="invoice"
          code={invoice.status_code}
          label={invoice.status_label}
        />
      </span>
      <span className="text-right text-[15px] font-semibold tabular-nums text-text">
        {money(invoice.remaining_cents)}
      </span>
      <span className="min-w-0 truncate text-sm text-text-muted">
        Due {formatDate(invoice.due_date)} · {invoice.account_display_name}
      </span>
      <span className="text-right text-xs text-text-muted">Balance due</span>
    </li>
  );
}

const COLUMNS: PortalColumn<PortalInvoice>[] = [
  {
    key: "invoice",
    header: "Invoice",
    cell: (i) => (
      <span className="flex flex-col">
        <Link href={href(i)} className={`${textLink} text-text`}>
          {invoiceName(i)}
        </Link>
        <span className="text-xs text-text-muted">
          {i.account_display_name}
        </span>
      </span>
    ),
  },
  { key: "issued", header: "Issued", cell: (i) => formatDate(i.issued_at) },
  { key: "due", header: "Due", cell: (i) => formatDate(i.due_date) },
  {
    key: "status",
    header: "Status",
    cell: (i) => (
      <PortalStatus
        kind="invoice"
        code={i.status_code}
        label={i.status_label}
      />
    ),
  },
  {
    key: "total",
    header: "Total",
    align: "right",
    cell: (i) => money(i.total_cents),
  },
  {
    key: "balance",
    header: "Balance due",
    align: "right",
    cell: (i) => (
      <span className="font-semibold">{money(i.remaining_cents)}</span>
    ),
  },
];

export default function InvoiceTable({
  invoices,
  caption = "Invoices",
  first = false,
}: {
  invoices: PortalInvoice[];
  caption?: string;
  first?: boolean;
}) {
  const wide = useMediaQuery(PORTAL_TABLES);
  if (wide) {
    return (
      <PortalTable
        caption={caption}
        columns={COLUMNS}
        rows={invoices}
        rowKey={(i) => i.invoice_id}
        first={first}
      />
    );
  }
  return (
    <ul aria-label={caption} data-portal-first={first || undefined}>
      {invoices.map((invoice) => (
        <InvoiceRow key={invoice.invoice_id} invoice={invoice} />
      ))}
    </ul>
  );
}
