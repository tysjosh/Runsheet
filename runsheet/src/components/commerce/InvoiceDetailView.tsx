"use client";

import {
  ArrowLeft,
  Calendar,
  DollarSign,
  FileText,
  Package,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  getInvoice,
  getInvoiceEvents,
  getPayments,
  type Invoice,
  type InvoiceEvent,
  type InvoiceLineItem,
  type Payment,
  retryQboPush,
  voidInvoice,
} from "../../services/commerceApi";
import { Badge, Button, type Column, Table } from "../ui";

interface InvoiceDetailViewProps {
  invoiceId: string;
  onBack: () => void;
}

export default function InvoiceDetailView({
  invoiceId,
  onBack,
}: InvoiceDetailViewProps) {
  const [invoice, setInvoice] = useState<Invoice | null>(null);
  const [events, setEvents] = useState<InvoiceEvent[]>([]);
  const [payments, setPayments] = useState<Payment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  useEffect(() => {
    async function fetchInvoiceDetails() {
      setLoading(true);
      setError(null);
      try {
        const [invoiceRes, eventsRes, paymentsRes] = await Promise.all([
          getInvoice(invoiceId),
          getInvoiceEvents(invoiceId),
          getPayments({ invoice_id: invoiceId }),
        ]);

        setInvoice(invoiceRes.data);
        setEvents(eventsRes.data || []);
        setPayments(paymentsRes.data || []);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to load invoice details",
        );
      } finally {
        setLoading(false);
      }
    }

    fetchInvoiceDetails();
  }, [invoiceId]);

  const handleVoidInvoice = async () => {
    if (!invoice) return;

    const reason = prompt("Enter reason for voiding this invoice:");
    if (!reason) return;

    setActionLoading(true);
    try {
      const response = await voidInvoice(invoiceId, { reason });
      setInvoice(response.data);
      alert("Invoice voided successfully");
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to void invoice");
    } finally {
      setActionLoading(false);
    }
  };

  const handleRetryQboPush = async () => {
    setActionLoading(true);
    try {
      const response = await retryQboPush(invoiceId);
      setInvoice(response.data);
      alert("QBO push retry initiated");
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to retry QBO push");
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="p-6">
        <div className="flex justify-center py-12">
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      </div>
    );
  }

  if (error || !invoice) {
    return (
      <div className="p-6">
        <Button variant="ghost" onClick={onBack} className="mb-4">
          <ArrowLeft className="w-4 h-4 mr-2" />
          Back to Invoices
        </Button>
        <div className="bg-error-light border border-error-light text-error-dark p-4 rounded">
          {error || "Invoice not found"}
        </div>
      </div>
    );
  }

  const getStatusBadge = (status: string) => {
    switch (status) {
      case "paid":
        return <Badge variant="success">Paid</Badge>;
      case "open":
        return <Badge variant="info">Open</Badge>;
      case "partial":
        return <Badge variant="warning">Partial</Badge>;
      case "overdue":
        return <Badge variant="error">Overdue</Badge>;
      case "void":
        return <Badge variant="default">Void</Badge>;
      case "draft":
        return <Badge variant="default">Draft</Badge>;
      default:
        return <Badge variant="default">{status}</Badge>;
    }
  };

  const getQboStateBadge = (state: string) => {
    if (state === "pushed") return <Badge variant="success">Pushed</Badge>;
    if (state === "dead_letter")
      return <Badge variant="error">Dead Letter</Badge>;
    if (state === "retry") return <Badge variant="warning">Retry</Badge>;
    return <Badge variant="default">{state}</Badge>;
  };

  const getPaymentMethodBadge = (method: string) => {
    return <Badge variant="default">{method}</Badge>;
  };

  const formatCurrency = (cents: number) => {
    return `$${(cents / 100).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  };

  const formatDateTime = (dateString: string) => {
    return new Date(dateString).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  const canVoid = invoice.status !== "void" && invoice.status !== "paid";
  const canRetryQbo = invoice.qbo_push_state === "dead_letter";

  const lineItemColumns: Column<InvoiceLineItem>[] = [
    {
      key: "product_code",
      label: "Product Code",
      className: "text-sm font-medium text-gray-900",
      render: (item) => item.product_code,
    },
    {
      key: "quantity_gallons",
      label: "Quantity (gal)",
      align: "right",
      className: "text-sm text-gray-700",
      render: (item) => item.quantity_gallons.toLocaleString(),
    },
    {
      key: "unit_price_cents",
      label: "Unit Price",
      align: "right",
      className: "text-sm text-gray-700",
      render: (item) => formatCurrency(item.unit_price_cents),
    },
    {
      key: "subtotal_cents",
      label: "Subtotal",
      align: "right",
      className: "text-sm font-medium text-gray-900",
      render: (item) => formatCurrency(item.subtotal_cents),
    },
  ];

  const paymentColumns: Column<Payment>[] = [
    {
      key: "payment_id",
      label: "Payment ID",
      className: "text-xs font-mono text-gray-700",
      render: (payment) => `${payment.payment_id.substring(0, 8)}...`,
    },
    {
      key: "method",
      label: "Method",
      render: (payment) => getPaymentMethodBadge(payment.method),
    },
    {
      key: "source",
      label: "Source",
      className: "text-sm text-gray-700 capitalize",
      render: (payment) => payment.source,
    },
    {
      key: "status",
      label: "Status",
      render: (payment) => (
        <Badge variant={payment.status === "applied" ? "success" : "error"}>
          {payment.status}
        </Badge>
      ),
    },
    {
      key: "received_at",
      label: "Received",
      className: "text-sm text-gray-700",
      render: (payment) => formatDate(payment.received_at),
    },
    {
      key: "amount_cents",
      label: "Amount",
      align: "right",
      className: "text-sm font-medium text-gray-900",
      render: (payment) => formatCurrency(payment.amount_cents),
    },
  ];

  return (
    <div className="p-6">
      {/* Header */}
      <div className="mb-6">
        <Button variant="ghost" onClick={onBack} className="mb-4">
          <ArrowLeft className="w-4 h-4 mr-2" />
          Back to Invoices
        </Button>

        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 mb-2">
              Invoice {invoice.invoice_number}
            </h1>
            <div className="flex items-center gap-3">
              {getStatusBadge(invoice.status)}
              {getQboStateBadge(invoice.qbo_push_state)}
              <span className="text-sm text-gray-500">
                Invoice ID: {invoice.invoice_id}
              </span>
            </div>
          </div>
          <div className="flex gap-2">
            {canRetryQbo && (
              <Button
                variant="secondary"
                size="sm"
                onClick={handleRetryQboPush}
                disabled={actionLoading}
              >
                <RefreshCw className="w-4 h-4 mr-2" />
                Retry QBO Push
              </Button>
            )}
            {canVoid && (
              <Button
                variant="secondary"
                size="sm"
                onClick={handleVoidInvoice}
                disabled={actionLoading}
              >
                <XCircle className="w-4 h-4 mr-2" />
                Void Invoice
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* Invoice Summary */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-2">
            <DollarSign className="w-4 h-4 text-gray-400" />
            <span className="text-sm text-gray-600">Total Amount</span>
          </div>
          <p className="text-2xl font-bold text-gray-900">
            {formatCurrency(invoice.total_cents)}
          </p>
        </div>

        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-2">
            <DollarSign className="w-4 h-4 text-success" />
            <span className="text-sm text-gray-600">Amount Paid</span>
          </div>
          <p className="text-2xl font-bold text-success">
            {formatCurrency(invoice.amount_paid_cents)}
          </p>
        </div>

        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-2">
            <DollarSign className="w-4 h-4 text-warning-dark" />
            <span className="text-sm text-gray-600">Remaining</span>
          </div>
          <p className="text-2xl font-bold text-warning-dark">
            {formatCurrency(invoice.remaining_cents)}
          </p>
        </div>

        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-2">
            <FileText className="w-4 h-4 text-gray-400" />
            <span className="text-sm text-gray-600">Tax</span>
          </div>
          <p className="text-2xl font-bold text-gray-900">
            {formatCurrency(invoice.tax_cents)}
          </p>
        </div>
      </div>

      {/* Invoice Details & Dates */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        {/* Invoice Information */}
        <div className="bg-white rounded-xl border border-gray-200 p-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">
            Invoice Information
          </h2>
          <dl className="space-y-3">
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Account ID</dt>
              <dd className="text-sm font-medium text-gray-900">
                {invoice.account_id}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Customer ID</dt>
              <dd className="text-sm font-medium text-gray-900">
                {invoice.customer_id}
              </dd>
            </div>
            {invoice.order_id && (
              <div className="flex justify-between">
                <dt className="text-sm text-gray-600">Order ID</dt>
                <dd className="text-sm font-medium text-gray-900">
                  {invoice.order_id}
                </dd>
              </div>
            )}
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Subtotal</dt>
              <dd className="text-sm font-medium text-gray-900">
                {formatCurrency(invoice.subtotal_cents)}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">QBO Push Attempts</dt>
              <dd className="text-sm font-medium text-gray-900">
                {invoice.qbo_push_attempts}
              </dd>
            </div>
            {invoice.qbo_push_last_error && (
              <div className="flex flex-col">
                <dt className="text-sm text-gray-600 mb-1">QBO Last Error</dt>
                <dd className="text-xs font-mono text-error-dark bg-error-light p-2 rounded">
                  {invoice.qbo_push_last_error}
                </dd>
              </div>
            )}
          </dl>
        </div>

        {/* Dates */}
        <div className="bg-white rounded-xl border border-gray-200 p-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">
            Important Dates
          </h2>
          <dl className="space-y-3">
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">
                <Calendar className="w-4 h-4 inline mr-1" />
                Issued
              </dt>
              <dd className="text-sm font-medium text-gray-900">
                {formatDate(invoice.issued_at)}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">
                <Calendar className="w-4 h-4 inline mr-1" />
                Due Date
              </dt>
              <dd className="text-sm font-medium text-gray-900">
                {formatDate(invoice.due_date)}
              </dd>
            </div>
            {invoice.finalized_at && (
              <div className="flex justify-between">
                <dt className="text-sm text-gray-600">Finalized</dt>
                <dd className="text-sm font-medium text-gray-900">
                  {formatDateTime(invoice.finalized_at)}
                </dd>
              </div>
            )}
            {invoice.voided_at && (
              <div className="flex justify-between">
                <dt className="text-sm text-gray-600">Voided</dt>
                <dd className="text-sm font-medium text-error-dark">
                  {formatDateTime(invoice.voided_at)}
                </dd>
              </div>
            )}
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Created</dt>
              <dd className="text-sm font-medium text-gray-900">
                {formatDateTime(invoice.created_at)}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-sm text-gray-600">Last Updated</dt>
              <dd className="text-sm font-medium text-gray-900">
                {formatDateTime(invoice.updated_at)}
              </dd>
            </div>
          </dl>
        </div>
      </div>

      {/* Void Reason */}
      {invoice.void_reason && (
        <div className="bg-error-light border border-error-light rounded-xl p-4 mb-6">
          <h3 className="text-sm font-semibold text-error-dark mb-2">
            Void Reason
          </h3>
          <p className="text-sm text-error-dark">{invoice.void_reason}</p>
        </div>
      )}

      {/* Line Items */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 mb-6">
        <div className="flex items-center gap-2 mb-4">
          <Package className="w-5 h-5 text-gray-400" />
          <h2 className="text-lg font-semibold text-gray-900">Line Items</h2>
        </div>

        {invoice.line_items.length === 0 ? (
          <p className="text-sm text-gray-500 py-4">No line items</p>
        ) : (
          <Table<InvoiceLineItem>
            ariaLabel="Invoice line items"
            columns={lineItemColumns}
            data={invoice.line_items}
            getRowId={(item) => item.line_id}
            variant="compact"
            emptyState={<span className="text-gray-500">No line items</span>}
          />
        )}
      </div>

      {/* Payment History */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 mb-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">
          Payment History
        </h2>

        {payments.length === 0 ? (
          <p className="text-sm text-gray-500 py-4">No payments recorded</p>
        ) : (
          <Table<Payment>
            ariaLabel="Payment history"
            columns={paymentColumns}
            data={payments}
            getRowId={(payment) => payment.payment_id}
            variant="compact"
            emptyState={
              <span className="text-gray-500">No payments recorded</span>
            }
          />
        )}
      </div>

      {/* Invoice Events Timeline */}
      <div className="bg-white rounded-xl border border-gray-200 p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">
          Event Timeline
        </h2>

        {events.length === 0 ? (
          <p className="text-sm text-gray-500 py-4">No events recorded</p>
        ) : (
          <div className="space-y-4">
            {events.map((event) => (
              <div
                key={event.event_id}
                className="flex gap-4 pb-4 border-b border-gray-100 last:border-0"
              >
                <div className="flex-shrink-0 w-2 h-2 mt-2 rounded-full bg-primary" />
                <div className="flex-1">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-sm font-medium text-gray-900 capitalize">
                      {event.event_type.replace(/_/g, " ")}
                    </span>
                    <span className="text-xs text-gray-500">
                      {formatDateTime(event.occurred_at)}
                    </span>
                  </div>
                  <p className="text-xs text-gray-600">Actor: {event.actor}</p>
                  {Object.keys(event.payload).length > 0 && (
                    <details className="mt-2">
                      <summary className="text-xs text-gray-500 cursor-pointer">
                        View payload
                      </summary>
                      <pre className="text-xs bg-gray-50 p-2 rounded mt-1 overflow-x-auto">
                        {JSON.stringify(event.payload, null, 2)}
                      </pre>
                    </details>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
