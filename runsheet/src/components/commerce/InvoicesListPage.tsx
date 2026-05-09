"use client";

import React, { useCallback, useEffect, useState } from "react";
import type { Invoice, CursorPaginatedResponse } from "../../services/commerceApi";
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
  const [customerFilter, setCustomerFilter] = useState<string>("");

  const fetchInvoices = useCallback(async (nextCursor?: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const filters: InvoiceFilters = { limit: 20 };
      if (statusFilter) filters.status = statusFilter as InvoiceFilters["status"];
      if (customerFilter) filters.customer_id = customerFilter;
      if (nextCursor) filters.cursor = nextCursor;

      const response: CursorPaginatedResponse<Invoice> = await getInvoices(filters);
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
  }, [statusFilter, customerFilter]);

  useEffect(() => {
    fetchInvoices();
  }, [fetchInvoices]);

  const formatCents = (cents: number) =>
    `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;

  const statusColor = (status: string) => {
    switch (status) {
      case "paid":
        return "bg-green-100 text-green-800";
      case "open":
        return "bg-blue-100 text-blue-800";
      case "partial":
        return "bg-yellow-100 text-yellow-800";
      case "overdue":
        return "bg-red-100 text-red-800";
      case "void":
        return "bg-gray-100 text-gray-800";
      case "draft":
        return "bg-purple-100 text-purple-800";
      default:
        return "bg-gray-100 text-gray-800";
    }
  };

  return (
    <div className="p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-bold">Invoices</h1>
        <p className="text-gray-600 mt-1">
          View and manage invoices across all accounts.
        </p>
      </header>

      {/* Filters */}
      <div className="flex gap-4 mb-6 flex-wrap">
        <div>
          <label htmlFor="invoice-status-filter" className="block text-sm font-medium mb-1">
            Status
          </label>
          <select
            id="invoice-status-filter"
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value);
              setCursor(null);
              fetchInvoices();
            }}
            className="border rounded px-3 py-2"
          >
            <option value="">All</option>
            <option value="draft">Draft</option>
            <option value="open">Open</option>
            <option value="partial">Partial</option>
            <option value="paid">Paid</option>
            <option value="overdue">Overdue</option>
            <option value="void">Void</option>
          </select>
        </div>
        <div>
          <label htmlFor="invoice-customer-filter" className="block text-sm font-medium mb-1">
            Customer ID
          </label>
          <input
            id="invoice-customer-filter"
            type="text"
            value={customerFilter}
            onChange={(e) => {
              setCustomerFilter(e.target.value);
              setCursor(null);
            }}
            placeholder="Filter by customer..."
            className="border rounded px-3 py-2"
          />
        </div>
      </div>

      {/* Error state */}
      {error && (
        <div role="alert" className="bg-red-50 border border-red-200 text-red-700 p-4 rounded mb-4">
          {error}
        </div>
      )}

      {/* Loading state */}
      {loading && (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading invoices...</span>
          <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
        </div>
      )}

      {/* Invoices table */}
      {!loading && !error && (
        <>
          <table className="w-full border-collapse" role="table">
            <thead>
              <tr className="border-b bg-gray-50">
                <th className="text-left p-3 font-medium">Invoice #</th>
                <th className="text-left p-3 font-medium">Status</th>
                <th className="text-left p-3 font-medium">Total</th>
                <th className="text-left p-3 font-medium">Remaining</th>
                <th className="text-left p-3 font-medium">Due Date</th>
                <th className="text-left p-3 font-medium">QBO State</th>
                <th className="text-left p-3 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {invoices.map((invoice) => (
                <tr key={invoice.invoice_id} className="border-b hover:bg-gray-50">
                  <td className="p-3 font-mono text-sm">{invoice.invoice_number}</td>
                  <td className="p-3">
                    <span
                      className={`inline-block px-2 py-1 rounded text-xs font-medium ${statusColor(invoice.status)}`}
                    >
                      {invoice.status}
                    </span>
                  </td>
                  <td className="p-3">{formatCents(invoice.total_cents)}</td>
                  <td className="p-3">{formatCents(invoice.remaining_cents)}</td>
                  <td className="p-3">{invoice.due_date}</td>
                  <td className="p-3">
                    <span
                      className={`inline-block px-2 py-1 rounded text-xs ${
                        invoice.qbo_push_state === "pushed"
                          ? "bg-green-100 text-green-800"
                          : invoice.qbo_push_state === "dead_letter"
                            ? "bg-red-100 text-red-800"
                            : "bg-gray-100 text-gray-800"
                      }`}
                    >
                      {invoice.qbo_push_state}
                    </span>
                  </td>
                  <td className="p-3">
                    <button
                      type="button"
                      onClick={() => onSelectInvoice?.(invoice.invoice_id)}
                      className="text-blue-600 hover:underline"
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
              {invoices.length === 0 && (
                <tr>
                  <td colSpan={7} className="p-6 text-center text-gray-500">
                    No invoices found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          {/* Pagination */}
          <nav aria-label="Pagination" className="flex justify-end items-center mt-4">
            <button
              type="button"
              disabled={!hasMore}
              onClick={() => fetchInvoices(cursor)}
              className="px-3 py-1 border rounded disabled:opacity-50"
            >
              Load More
            </button>
          </nav>
        </>
      )}
    </div>
  );
}
