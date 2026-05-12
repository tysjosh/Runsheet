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
  Payment,
} from "../../services/commerceApi";
import { getPayments, type PaymentFilters } from "../../services/commerceApi";

export default function PaymentsListPage() {
  const [payments, setPayments] = useState<Payment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [invoiceFilter, setInvoiceFilter] = useState<string>("");
  const [accountFilter, setAccountFilter] = useState<string>("");

  const fetchPayments = useCallback(
    async (nextCursor?: string | null) => {
      setLoading(true);
      setError(null);
      try {
        const filters: PaymentFilters = { limit: 20 };
        if (invoiceFilter) filters.invoice_id = invoiceFilter;
        if (accountFilter) filters.account_id = accountFilter;
        if (nextCursor) filters.cursor = nextCursor;

        const response: CursorPaginatedResponse<Payment> =
          await getPayments(filters);
        setPayments(response.data);
        setCursor(response.cursor);
        setHasMore(response.has_more);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to load payments",
        );
      } finally {
        setLoading(false);
      }
    },
    [invoiceFilter, accountFilter],
  );

  useEffect(() => {
    fetchPayments();
  }, [fetchPayments]);

  const formatCents = (cents: number) =>
    `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;

  const formatDate = (dateString: string) => {
    try {
      return new Date(dateString).toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
    } catch {
      return dateString;
    }
  };

  const getStatusVariant = (
    status: string,
  ): "success" | "error" | "default" => {
    if (status === "applied") return "success";
    if (status === "reversed") return "error";
    return "default";
  };

  const getMethodLabel = (method: string): string => {
    const labels: Record<string, string> = {
      card: "Card",
      ach: "ACH",
      wire: "Wire",
      check: "Check",
      credit_balance: "Credit Balance",
      other: "Other",
    };
    return labels[method] || method;
  };

  const getSourceLabel = (source: string): string => {
    const labels: Record<string, string> = {
      stripe: "Stripe",
      qbo: "QuickBooks",
      manual: "Manual",
      account_credit: "Account Credit",
      void_cascade: "Void Cascade",
    };
    return labels[source] || source;
  };

  return (
    <div className="p-6">
      <PageHeader
        title="Payments"
        subtitle="View and manage all payment transactions across accounts."
      />

      <FilterBar
        searchPlaceholder="Search by invoice or account ID..."
        onSearchChange={(value) => {
          // Simple heuristic: if it looks like an invoice ID, filter by invoice
          // Otherwise filter by account
          if (value.toLowerCase().includes("inv")) {
            setInvoiceFilter(value);
            setAccountFilter("");
          } else if (value.toLowerCase().includes("acc")) {
            setAccountFilter(value);
            setInvoiceFilter("");
          } else if (value) {
            setInvoiceFilter(value);
            setAccountFilter("");
          } else {
            setInvoiceFilter("");
            setAccountFilter("");
          }
        }}
        filters={null}
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
          <span className="sr-only">Loading payments...</span>
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      )}

      {/* Payments table */}
      {!loading &&
        !error &&
        (payments.length === 0 ? (
          <EmptyState
            icon={<span className="text-4xl">💳</span>}
            title="No payments found"
            description="No payment transactions have been recorded yet."
          />
        ) : (
          <>
            <Table
              columns={[
                {
                  key: "payment_id",
                  label: "Payment ID",
                  render: (payment) => (
                    <span className="font-mono text-xs">
                      {payment.payment_id.slice(0, 8)}...
                    </span>
                  ),
                },
                {
                  key: "invoice_id",
                  label: "Invoice",
                  render: (payment) => (
                    <span className="font-mono text-xs">
                      {payment.invoice_id.slice(0, 12)}...
                    </span>
                  ),
                },
                {
                  key: "amount_cents",
                  label: "Amount",
                  render: (payment) => (
                    <span className="font-semibold">
                      {formatCents(payment.amount_cents)}
                    </span>
                  ),
                },
                {
                  key: "method",
                  label: "Method",
                  render: (payment) => (
                    <Badge variant="neutral">
                      {getMethodLabel(payment.method)}
                    </Badge>
                  ),
                },
                {
                  key: "source",
                  label: "Source",
                  render: (payment) => (
                    <span className="text-sm text-gray-600">
                      {getSourceLabel(payment.source)}
                    </span>
                  ),
                },
                {
                  key: "status",
                  label: "Status",
                  render: (payment) => (
                    <Badge variant={getStatusVariant(payment.status)}>
                      {payment.status}
                    </Badge>
                  ),
                },
                {
                  key: "received_at",
                  label: "Received",
                  render: (payment) => (
                    <span className="text-sm">
                      {formatDate(payment.received_at)}
                    </span>
                  ),
                },
                {
                  key: "reference",
                  label: "Reference",
                  render: (payment) => (
                    <span className="text-sm text-gray-600">
                      {payment.reference || "—"}
                    </span>
                  ),
                },
              ]}
              data={payments}
              keyExtractor={(payment) => payment.payment_id}
            />

            <div className="flex justify-between items-center mt-4">
              <div className="text-sm text-gray-600">
                Showing {payments.length} payment
                {payments.length !== 1 ? "s" : ""}
              </div>
              <Button
                variant="secondary"
                disabled={!hasMore}
                onClick={() => fetchPayments(cursor)}
              >
                Load More
              </Button>
            </div>
          </>
        ))}
    </div>
  );
}
