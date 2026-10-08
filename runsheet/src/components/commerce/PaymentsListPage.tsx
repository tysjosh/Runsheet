"use client";

/**
 * Billing → Payments (UI revamp task 3.4): no nested header, one toolbar
 * (invoice id search, a Filters popover with the account, refresh), and a
 * DataTable whose pager walks the API cursor.
 */
import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  type Column,
  DataTable,
  Field,
  FilterPopover,
  IconButton,
  SearchableSelect,
  type SearchableSelectOption,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate, money } from "../../lib/format";
import {
  getAccounts,
  getPayments,
  type Payment,
  type PaymentFilters,
} from "../../services/commerceApi";
import { PageTitle } from "../ui/PageHeader";
import { PaymentStatusBadge } from "./billingStatus";
import { useCursorPages } from "./useCursorPages";

const PAGE_SIZE = 20;

const METHOD_LABELS: Record<string, string> = {
  card: "Card",
  ach: "ACH",
  wire: "Wire",
  check: "Check",
  credit_balance: "Credit balance",
  other: "Other",
};

const SOURCE_LABELS: Record<string, string> = {
  stripe: "Stripe",
  qbo: "QuickBooks",
  manual: "Manual",
  account_credit: "Account credit",
  void_cascade: "Void cascade",
};

/**
 * AccountFilterSelect — searchable account selector backed by /commerce/accounts.
 *
 * Accounts are a distinct entity from customers, so this uses the generic
 * SearchableSelect with the live account roster rather than CustomerPicker.
 */
function AccountFilterSelect({
  id,
  value,
  onChange,
}: {
  id?: string;
  value: string | null;
  onChange: (accountId: string) => void;
}) {
  const [options, setOptions] = useState<SearchableSelectOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError(false);
      try {
        const res = await getAccounts({ status: "active", limit: 200 });
        if (cancelled) return;
        const rows = Array.isArray(res.data) ? res.data : [];
        setOptions(
          rows.map((a) => ({
            value: a.account_id,
            label: a.display_name || a.account_id,
            sublabel: a.account_id,
          })),
        );
      } catch {
        if (!cancelled) setLoadError(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Keep an already-selected account visible even if not in the loaded set.
  const mergedOptions =
    value && !options.some((o) => o.value === value)
      ? [{ value, label: value }, ...options]
      : options;

  return (
    <SearchableSelect
      id={id}
      aria-label="Account"
      options={mergedOptions}
      value={value}
      onChange={onChange}
      loading={loading}
      allowClear
      placeholder="All accounts"
      searchPlaceholder="Search accounts…"
      emptyMessage={loadError ? "Couldn't load accounts" : "No accounts found"}
    />
  );
}

export default function PaymentsListPage() {
  const [payments, setPayments] = useState<Payment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [invoiceFilter, setInvoiceFilter] = useState<string>("");
  const [accountFilter, setAccountFilter] = useState<string>("");
  const [reload, setReload] = useState(0);
  const pages = useCursorPages(`${invoiceFilter}|${accountFilter}`);
  const { page, cursor, received } = pages;

  const fetchPayments = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: PaymentFilters = { limit: PAGE_SIZE };
      if (invoiceFilter.trim()) filters.invoice_id = invoiceFilter.trim();
      if (accountFilter) filters.account_id = accountFilter;
      if (cursor) filters.cursor = cursor;
      const response = await getPayments(filters);
      setPayments(response.data ?? []);
      received(page, response);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load payments");
    } finally {
      setLoading(false);
    }
    // `reload` forces a refetch from the Refresh button.
  }, [invoiceFilter, accountFilter, cursor, page, received, reload]);

  useEffect(() => {
    fetchPayments();
  }, [fetchPayments]);

  const embedded = usePageChrome({});

  const columns: Column<Payment>[] = [
    {
      key: "received_at",
      header: "Received",
      width: 150,
      className: "text-slate-700",
      cell: (p) => calendarDate(p.received_at),
    },
    {
      key: "amount_cents",
      header: "Amount",
      align: "right",
      width: 130,
      className: "tabular-nums font-semibold text-text",
      cell: (p) => money(p.amount_cents / 100),
    },
    {
      key: "status",
      header: "Status",
      width: 120,
      cell: (p) => <PaymentStatusBadge status={p.status} />,
    },
    {
      key: "method",
      header: "Method",
      width: 120,
      className: "text-slate-700",
      cell: (p) => METHOD_LABELS[p.method] ?? p.method,
    },
    {
      key: "source",
      header: "Source",
      width: 130,
      className: "text-slate-700",
      cell: (p) => SOURCE_LABELS[p.source] ?? p.source,
    },
    {
      key: "invoice_id",
      header: "Invoice",
      truncate: true,
      title: (p) => p.invoice_id,
      className: "font-mono text-xs text-slate-700",
      cell: (p) => p.invoice_id,
    },
    {
      key: "reference",
      header: "Reference",
      truncate: true,
      title: (p) => p.reference ?? undefined,
      className: "text-slate-700",
      cell: (p) => p.reference || "—",
    },
  ];

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Payments
          </PageTitle>
        </div>
      )}
      <Toolbar
        label="Payments"
        search={
          <input
            type="search"
            value={invoiceFilter}
            onChange={(e) => setInvoiceFilter(e.target.value)}
            placeholder="Invoice ID"
            aria-label="Invoice ID"
            className="h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          />
        }
        filters={
          <FilterPopover
            count={accountFilter ? 1 : 0}
            label="Payment filters"
            onClear={() => setAccountFilter("")}
          >
            <Field label="Account" id="payments-account-filter">
              <AccountFilterSelect
                id="payments-account-filter"
                value={accountFilter || null}
                onChange={(value) => setAccountFilter(value)}
              />
            </Field>
          </FilterPopover>
        }
        end={
          <IconButton
            label="Refresh"
            size="sm"
            onClick={() => setReload((n) => n + 1)}
            icon={
              <RefreshCw
                className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
              />
            }
          />
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<Payment>
          ariaLabel="Payments"
          columns={columns}
          data={loading || error ? [] : payments}
          loading={loading}
          error={error ? { message: error, onRetry: fetchPayments } : null}
          getRowId={(p) => p.payment_id}
          pagination={
            pages.totalPages > 1
              ? {
                  page: pages.page,
                  totalPages: pages.totalPages,
                  onPageChange: pages.goTo,
                }
              : undefined
          }
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No payments found</p>
              <p className="mt-1 text-xs">
                No payment transactions match these filters.
              </p>
            </div>
          }
        />
      </div>
    </div>
  );
}
