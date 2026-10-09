"use client";

/**
 * Billing → Invoices (UI revamp task 3.4): no nested header (Export CSV goes
 * to the hub's title row), one toolbar (status chips, a Filters popover with
 * the customer, refresh), and a DataTable whose pager walks the API cursor.
 */
import { Eye, RefreshCw } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type Column,
  DataTable,
  ExportCsvButton,
  Field,
  FilterChips,
  FilterPopover,
  IconButton,
  LoadErrorState,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate, money } from "../../lib/format";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import {
  getInvoices,
  type Invoice,
  type InvoiceFilters,
  type InvoiceStatus,
} from "../../services/commerceApi";
import CustomerPicker from "../ops/CustomerPicker";
import { PageTitle } from "../ui/PageHeader";
import {
  INVOICE_STATUS,
  InvoiceStatusBadge,
  QboStateBadge,
} from "./billingStatus";
import { useCursorPages } from "./useCursorPages";

const PAGE_SIZE = 20;

const STATUS_CHIPS: { id: "" | InvoiceStatus; label: string }[] = [
  { id: "", label: "All" },
  { id: "draft", label: "Draft" },
  { id: "open", label: "Open" },
  { id: "partial", label: "Partial" },
  { id: "overdue", label: "Overdue" },
  { id: "paid", label: "Paid" },
  { id: "void", label: "Void" },
];

interface InvoicesListPageProps {
  onSelectInvoice?: (invoiceId: string) => void;
}

export default function InvoicesListPage({
  onSelectInvoice,
}: InvoicesListPageProps) {
  const router = useRouter();
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [moduleDisabled, setModuleDisabled] = useState<LoadFailure | null>(
    null,
  );
  const [statusFilter, setStatusFilter] = useState<InvoiceStatus | "">("");
  const [customerFilter, setCustomerFilter] = useState<string>("");
  const [reload, setReload] = useState(0);
  const pages = useCursorPages(`${statusFilter}|${customerFilter}`);
  const { page, cursor, received } = pages;

  const fetchInvoices = useCallback(async () => {
    setLoading(true);
    setError(null);
    setModuleDisabled(null);
    try {
      const filters: InvoiceFilters = { limit: PAGE_SIZE };
      if (statusFilter) filters.status = statusFilter;
      if (customerFilter) filters.customer_id = customerFilter;
      if (cursor) filters.cursor = cursor;
      const response = await getInvoices(filters);
      setInvoices(response.data ?? []);
      received(page, response);
    } catch (err) {
      const failure = classifyLoadError(err, "Failed to load invoices");
      if (failure.kind === "module_disabled") setModuleDisabled(failure);
      else setError(failure.message);
    } finally {
      setLoading(false);
    }
    // `reload` forces a refetch from the Refresh button.
  }, [statusFilter, customerFilter, cursor, page, received, reload]);

  useEffect(() => {
    fetchInvoices();
  }, [fetchInvoices]);

  const actions = useMemo(
    () => (
      <ExportCsvButton
        type="invoices"
        params={{ status: statusFilter, customer_id: customerFilter }}
        subject="invoices"
        allowedRoles={["admin"]}
      />
    ),
    [statusFilter, customerFilter],
  );
  const embedded = usePageChrome({ actions: moduleDisabled ? null : actions });

  const open = (inv: Invoice) => onSelectInvoice?.(inv.invoice_id);

  const columns: Column<Invoice>[] = [
    {
      key: "invoice_number",
      header: "Invoice",
      width: 160,
      className: "font-mono text-xs text-text",
      cell: (i) => i.invoice_number,
    },
    {
      key: "status",
      header: "Status",
      width: 150,
      cell: (i) => <InvoiceStatusBadge status={i.status} />,
    },
    {
      key: "total_cents",
      header: "Total",
      align: "right",
      width: 130,
      className: "tabular-nums text-text",
      cell: (i) => money(i.total_cents / 100),
    },
    {
      key: "remaining_cents",
      header: "Remaining",
      align: "right",
      width: 130,
      className: "tabular-nums text-slate-700",
      cell: (i) => money(i.remaining_cents / 100),
    },
    {
      key: "due_date",
      header: "Due",
      width: 150,
      className: "text-slate-700",
      cell: (i) => calendarDate(i.due_date),
    },
    {
      key: "qbo_push_state",
      header: "QuickBooks",
      width: 150,
      cell: (i) => <QboStateBadge state={i.qbo_push_state} />,
    },
  ];

  const titleRow = embedded ? null : (
    <div className="flex h-11 items-center border-b border-slate-200 px-4">
      <PageTitle className="text-base font-semibold text-text">
        Invoices
      </PageTitle>
      {!moduleDisabled && <div className="ml-auto">{actions}</div>}
    </div>
  );

  if (moduleDisabled) {
    return (
      <div className="flex h-full flex-col">
        {titleRow}
        <div className="p-4">
          <LoadErrorState
            failure={moduleDisabled}
            entityLabel="Invoices"
            onBack={() => router.push("/dashboard")}
            backLabel="Back to Today"
            embedded
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-surface">
      {titleRow}
      <Toolbar
        label="Invoices"
        filters={
          <>
            <FilterChips
              label="Invoice status"
              options={STATUS_CHIPS.map((c) => ({
                id: c.id || "all",
                label: c.label,
                status: c.id ? INVOICE_STATUS[c.id].status : undefined,
              }))}
              value={statusFilter || "all"}
              onChange={(v) =>
                setStatusFilter(v === "all" ? "" : (v as InvoiceStatus))
              }
            />
            <FilterPopover
              count={customerFilter ? 1 : 0}
              label="Invoice filters"
              onClear={() => setCustomerFilter("")}
            >
              <Field label="Customer" id="invoices-filter-customer">
                <CustomerPicker
                  id="invoices-filter-customer"
                  aria-label="Customer"
                  value={customerFilter || null}
                  onChange={setCustomerFilter}
                  allowClear
                  placeholder="All customers"
                />
              </Field>
            </FilterPopover>
          </>
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
        <DataTable<Invoice>
          ariaLabel="Invoices"
          columns={columns}
          data={loading || error ? [] : invoices}
          loading={loading}
          error={error ? { message: error, onRetry: fetchInvoices } : null}
          getRowId={(i) => i.invoice_id}
          rowLabel={(i) => `Invoice ${i.invoice_number}`}
          onRowClick={onSelectInvoice ? open : undefined}
          rowMenu={
            onSelectInvoice
              ? (i) => [
                  {
                    id: "view",
                    label: "View invoice",
                    icon: <Eye className="h-3.5 w-3.5" />,
                    onSelect: () => open(i),
                  },
                ]
              : undefined
          }
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
              <p className="text-sm font-medium">No invoices found</p>
              <p className="mt-1 text-xs">Try adjusting your filters</p>
            </div>
          }
        />
      </div>
    </div>
  );
}
