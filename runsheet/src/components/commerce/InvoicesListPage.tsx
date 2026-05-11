"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  FilterBar,
  PageHeader,
  Table,
} from "@/components/ui";
import type {
  CursorPaginatedResponse,
  Invoice,
} from "../../services/commerceApi";
import { getInvoices, type InvoiceFilters } from "../../services/commerceApi";

interface InvoicesListPageProps {
  onSelectInvoice?: (invoiceId: string) => void;
}

export default function InvoicesListPage({
  onSelectInvoice,
}: InvoicesListPageProps) {
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [customerFilter, _setCustomerFilter] = useState<string>("");

  const fetchInvoices = useCallback(
    async (nextCursor?: string | null) => {
      setLoading(true);
      setError(null);
      try {
        const filters: InvoiceFilters = { limit: 20 };
        if (statusFilter)
          filters.status = statusFilter as InvoiceFilters["status"];
        if (customerFilter) filters.customer_id = customerFilter;
        if (nextCursor) filters.cursor = nextCursor;

        const response: CursorPaginatedResponse<Invoice> =
          await getInvoices(filters);
        setInvoices(response.data);
        setCursor(response.cursor);
        setHasMore(response.has_more);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to load invoices",
        );
      } finally {
        setLoading(false);
      }
    },
    [statusFilter, customerFilter],
  );

  useEffect(() => {
    fetchInvoices();
  }, [fetchInvoices]);

  const formatCents = (cents: number) =>
    `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;

  const getStatusVariant = (
    status: string,
  ): "success" | "info" | "warning" | "error" | "default" => {
    switch (status) {
      case "paid":
        return "success";
      case "open":
        return "info";
      case "partial":
        return "warning";
      case "overdue":
        return "error";
      case "void":
        return "default";
      case "draft":
        return "default";
      default:
        return "default";
    }
  };

  const getQboStateVariant = (
    state: string,
  ): "success" | "error" | "default" => {
    if (state === "pushed") return "success";
    if (state === "dead_letter") return "error";
    return "default";
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Invoices"
        subtitle="View and manage invoices across all accounts."
      />

      <FilterBar
        filters={
          <select
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value);
              fetchInvoices();
            }}
            className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
            aria-label="Status"
          >
            <option value="">All</option>
            <option value="draft">Draft</option>
            <option value="open">Open</option>
            <option value="partial">Partial</option>
            <option value="paid">Paid</option>
            <option value="overdue">Overdue</option>
            <option value="void">Void</option>
          </select>
        }
      />

      {/* Error state */}
      {error && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* Loading state */}
      {loading && (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading invoices...</span>
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      )}

      {/* Invoices table */}
      {!loading &&
        !error &&
        (invoices.length === 0 ? (
          <EmptyState
            icon={<span className="text-4xl">📄</span>}
            title="No invoices found"
            description="Try adjusting your filters"
          />
        ) : (
          <>
            <Table
              columns={[
                {
                  key: "invoice_number",
                  label: "Invoice #",
                  render: (invoice) => (
                    <span className="font-mono text-sm">
                      {invoice.invoice_number}
                    </span>
                  ),
                },
                {
                  key: "status",
                  label: "Status",
                  render: (invoice) => (
                    <Badge variant={getStatusVariant(invoice.status)}>
                      {invoice.status}
                    </Badge>
                  ),
                },
                {
                  key: "total_cents",
                  label: "Total",
                  render: (invoice) => formatCents(invoice.total_cents),
                },
                {
                  key: "remaining_cents",
                  label: "Remaining",
                  render: (invoice) => formatCents(invoice.remaining_cents),
                },
                { key: "due_date", label: "Due Date" },
                {
                  key: "qbo_push_state",
                  label: "QBO State",
                  render: (invoice) => (
                    <Badge variant={getQboStateVariant(invoice.qbo_push_state)}>
                      {invoice.qbo_push_state}
                    </Badge>
                  ),
                },
                {
                  key: "actions",
                  label: "Actions",
                  render: (invoice) => (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onSelectInvoice?.(invoice.invoice_id)}
                    >
                      View
                    </Button>
                  ),
                },
              ]}
              data={invoices}
              keyExtractor={(invoice) => invoice.invoice_id}
            />

            <div className="flex justify-end mt-4">
              <Button
                variant="secondary"
                disabled={!hasMore}
                onClick={() => fetchInvoices(cursor)}
              >
                Load More
              </Button>
            </div>
          </>
        ))}
    </div>
  );
}
