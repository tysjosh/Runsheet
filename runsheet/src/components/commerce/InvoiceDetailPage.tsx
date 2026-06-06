"use client";

import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  EntityLink,
  StatsBar,
  Table,
} from "@/components/ui";
import type {
  Invoice,
  InvoiceEvent,
  VoidInvoicePayload,
} from "../../services/commerceApi";
import {
  getInvoice,
  getInvoiceEvents,
  retryQboPush,
  voidInvoice,
} from "../../services/commerceApi";

interface InvoiceDetailPageProps {
  invoiceId: string;
  onBack?: () => void;
  onViewAccount?: (accountId: string) => void;
}

export default function InvoiceDetailPage({
  invoiceId,
  onBack,
  onViewAccount,
}: InvoiceDetailPageProps) {
  const [invoice, setInvoice] = useState<Invoice | null>(null);
  const [events, setEvents] = useState<InvoiceEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [voidDialogOpen, setVoidDialogOpen] = useState(false);
  const [voidReason, setVoidReason] = useState("");
  const [voidForce, setVoidForce] = useState(false);
  const [voiding, setVoiding] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [invoiceRes, eventsRes] = await Promise.all([
        getInvoice(invoiceId),
        getInvoiceEvents(invoiceId),
      ]);
      setInvoice(invoiceRes.data);
      setEvents(eventsRes.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load invoice details",
      );
    } finally {
      setLoading(false);
    }
  }, [invoiceId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // WebSocket subscription for live invoice updates
  useEffect(() => {
    const wsUrl =
      (process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000") +
      "/ws/commerce/invoices";

    let ws: WebSocket | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
    let shouldReconnect = true;

    const connect = () => {
      if (!shouldReconnect) return;
      try {
        ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onmessage = (event) => {
          try {
            const message = JSON.parse(event.data);
            if (
              message.type === "invoice_updated" &&
              message.data?.invoice_id === invoiceId
            ) {
              setInvoice(message.data);
            }
            if (
              message.type === "invoice_event" &&
              message.data?.invoice_id === invoiceId
            ) {
              setEvents((prev) => [...prev, message.data]);
            }
          } catch {
            // ignore parse errors
          }
        };

        ws.onclose = () => {
          if (shouldReconnect) {
            reconnectTimeout = setTimeout(connect, 3000);
          }
        };

        ws.onerror = () => {
          ws?.close();
        };
      } catch {
        if (shouldReconnect) {
          reconnectTimeout = setTimeout(connect, 3000);
        }
      }
    };

    connect();

    return () => {
      shouldReconnect = false;
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      if (ws) {
        ws.onclose = null;
        ws.close(1000, "Component unmounted");
      }
      wsRef.current = null;
    };
  }, [invoiceId]);

  const handleVoid = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!voidReason.trim()) return;
    setVoiding(true);
    try {
      const payload: VoidInvoicePayload = {
        reason: voidReason,
        force: voidForce,
      };
      const res = await voidInvoice(invoiceId, payload);
      setInvoice(res.data);
      setVoidDialogOpen(false);
      setVoidReason("");
      setVoidForce(false);
      // Refresh events
      const eventsRes = await getInvoiceEvents(invoiceId);
      setEvents(eventsRes.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to void invoice");
    } finally {
      setVoiding(false);
    }
  };

  const handleRetryQbo = async () => {
    try {
      const res = await retryQboPush(invoiceId);
      setInvoice(res.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to retry QBO push");
    }
  };

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12">
        <span className="sr-only">Loading invoice details...</span>
        <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div role="alert" className="p-6">
        <div className="bg-error-light border border-error-light text-error-dark p-4 rounded">
          {error}
        </div>
      </div>
    );
  }

  if (!invoice) return null;

  const formatCents = (cents: number) =>
    `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;

  const _statusColor = (status: string) => {
    switch (status) {
      case "paid":
        return "bg-success-light text-success-dark";
      case "open":
        return "bg-info-light text-info-dark";
      case "partial":
        return "bg-warning-light text-warning-dark";
      case "overdue":
        return "bg-error-light text-error-dark";
      case "void":
        return "bg-gray-100 text-gray-800";
      default:
        return "bg-brand-secondary-soft text-brand-secondary";
    }
  };

  return (
    <div className="p-6">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center gap-4 mb-2">
          {onBack && (
            <Button variant="ghost" size="sm" onClick={onBack}>
              ← Back to Invoices
            </Button>
          )}
        </div>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">
              Invoice {invoice.invoice_number}
            </h1>
            <p className="text-gray-600">Due: {invoice.due_date}</p>
            {/* Navigable references to the order, account, and customer this
                invoice belongs to (Req 12.1, 13.1). Account is an in-hub
                destination (it lives as a tab in CommerceHub) so it navigates
                via the onViewAccount callback rather than a standalone route;
                customer and order have canonical routes and link via
                <EntityLink>. */}
            <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-sm">
              <div className="flex items-center gap-1.5">
                <dt className="text-gray-500">Account:</dt>
                <dd>
                  {onViewAccount ? (
                    <button
                      type="button"
                      onClick={() => onViewAccount(invoice.account_id)}
                      className="text-info hover:text-info-dark underline underline-offset-2"
                    >
                      {invoice.account_id}
                    </button>
                  ) : (
                    <EntityLink type="account" id={invoice.account_id} />
                  )}
                </dd>
              </div>
              <div className="flex items-center gap-1.5">
                <dt className="text-gray-500">Customer:</dt>
                <dd>
                  <EntityLink type="customer" id={invoice.customer_id} />
                </dd>
              </div>
              <div className="flex items-center gap-1.5">
                <dt className="text-gray-500">Order:</dt>
                <dd>
                  <EntityLink type="order" id={invoice.order_id} />
                </dd>
              </div>
            </dl>
          </div>
          <div className="flex items-center gap-3">
            <Badge
              variant={
                invoice.status === "paid"
                  ? "success"
                  : invoice.status === "open"
                    ? "info"
                    : invoice.status === "partial"
                      ? "warning"
                      : invoice.status === "overdue"
                        ? "error"
                        : invoice.status === "void"
                          ? "neutral"
                          : "default"
              }
            >
              {invoice.status}
            </Badge>
            {invoice.status !== "void" && invoice.status !== "paid" && (
              <Button variant="danger" onClick={() => setVoidDialogOpen(true)}>
                Void Invoice
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* Summary cards */}
      <section aria-labelledby="invoice-summary-heading" className="mb-8">
        <h2 id="invoice-summary-heading" className="sr-only">
          Invoice Summary
        </h2>
        <StatsBar
          stats={[
            { label: "Subtotal", value: formatCents(invoice.subtotal_cents) },
            { label: "Tax", value: formatCents(invoice.tax_cents) },
            { label: "Total", value: formatCents(invoice.total_cents) },
            { label: "Paid", value: formatCents(invoice.amount_paid_cents) },
            { label: "Remaining", value: formatCents(invoice.remaining_cents) },
          ]}
        />
      </section>

      {/* Line items */}
      <section aria-labelledby="line-items-heading" className="mb-8">
        <h2 id="line-items-heading" className="text-lg font-semibold mb-3">
          Line Items
        </h2>
        <Table
          columns={[
            { key: "product_code", label: "Product" },
            { key: "quantity_gallons", label: "Quantity (gal)" },
            {
              key: "unit_price_cents",
              label: "Unit Price",
              render: (item) => formatCents(item.unit_price_cents),
            },
            {
              key: "subtotal_cents",
              label: "Subtotal",
              render: (item) => formatCents(item.subtotal_cents),
            },
          ]}
          data={invoice.line_items}
          getRowId={(item) => item.line_id}
        />
      </section>

      {/* QBO push state */}
      <section aria-labelledby="qbo-heading" className="mb-8">
        <h2 id="qbo-heading" className="text-lg font-semibold mb-3">
          QBO Sync Status
        </h2>
        <div className="flex items-center gap-4">
          <Badge
            variant={
              invoice.qbo_push_state === "pushed"
                ? "success"
                : invoice.qbo_push_state === "dead_letter"
                  ? "error"
                  : "neutral"
            }
          >
            {invoice.qbo_push_state}
          </Badge>
          {invoice.qbo_push_state === "dead_letter" && (
            <Button variant="ghost" size="sm" onClick={handleRetryQbo}>
              Retry Push
            </Button>
          )}
        </div>
      </section>

      {/* Event timeline */}
      <section aria-labelledby="timeline-heading" className="mb-8">
        <h2 id="timeline-heading" className="text-lg font-semibold mb-3">
          Event Timeline
        </h2>
        {events.length === 0 ? (
          <EmptyState
            icon={<span className="text-4xl">📋</span>}
            title="No events"
            description="No events recorded."
          />
        ) : (
          <ol
            className="relative border-l border-gray-300 ml-4"
            aria-label="Invoice events"
          >
            {events.map((event) => (
              <li key={event.event_id} className="mb-4 ml-6">
                <span className="absolute -left-2 w-4 h-4 bg-primary rounded-full border-2 border-white" />
                <div className="flex items-center gap-2">
                  <span className="font-medium text-sm capitalize">
                    {event.event_type.replace(/_/g, " ")}
                  </span>
                  <span className="text-xs text-gray-500">
                    {new Date(event.occurred_at).toLocaleString()}
                  </span>
                </div>
                <p className="text-sm text-gray-600">
                  by {event.actor}
                  {event.payload && Object.keys(event.payload).length > 0 && (
                    <span className="ml-2 text-xs text-gray-400">
                      {JSON.stringify(event.payload)}
                    </span>
                  )}
                </p>
              </li>
            ))}
          </ol>
        )}
      </section>

      {/* Void dialog */}
      {voidDialogOpen && (
        <div
          role="dialog"
          aria-labelledby="void-dialog-title"
          className="fixed inset-0 z-50 flex items-center justify-center"
        >
          <div
            className="absolute inset-0 bg-black/30"
            onClick={() => setVoidDialogOpen(false)}
            role="presentation"
          />
          <div className="relative bg-white rounded-lg shadow-xl p-6 w-full max-w-md">
            <h2 id="void-dialog-title" className="text-xl font-bold mb-4">
              Void Invoice
            </h2>
            <p className="text-sm text-gray-600 mb-4">
              This action cannot be undone. The invoice will be marked as void
              and any applied payments will be reversed.
            </p>
            <form onSubmit={handleVoid}>
              <div className="mb-4">
                <label
                  htmlFor="void-reason"
                  className="block text-sm font-medium mb-1"
                >
                  Reason
                </label>
                <textarea
                  id="void-reason"
                  value={voidReason}
                  onChange={(e) => setVoidReason(e.target.value)}
                  required
                  rows={3}
                  className="w-full border rounded px-3 py-2"
                  placeholder="Reason for voiding..."
                />
              </div>
              {invoice.amount_paid_cents > 0 && (
                <div className="mb-4">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={voidForce}
                      onChange={(e) => setVoidForce(e.target.checked)}
                    />
                    <span className="text-sm">
                      Force void (reverse{" "}
                      {formatCents(invoice.amount_paid_cents)} in applied
                      payments)
                    </span>
                  </label>
                </div>
              )}
              <div className="flex gap-3">
                <Button
                  type="submit"
                  variant="danger"
                  loading={voiding}
                  disabled={!voidReason.trim()}
                >
                  Confirm Void
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => setVoidDialogOpen(false)}
                >
                  Cancel
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
