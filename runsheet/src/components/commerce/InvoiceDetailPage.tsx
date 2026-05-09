"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import type { Invoice, InvoiceEvent, VoidInvoicePayload } from "../../services/commerceApi";
import {
  getInvoice,
  getInvoiceEvents,
  voidInvoice,
  retryQboPush,
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
        <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div role="alert" className="p-6">
        <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded">
          {error}
        </div>
      </div>
    );
  }

  if (!invoice) return null;

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
      default:
        return "bg-purple-100 text-purple-800";
    }
  };

  return (
    <div className="p-6">
      {/* Header */}
      <header className="mb-6">
        <div className="flex items-center gap-4 mb-2">
          {onBack && (
            <button type="button" onClick={onBack} className="text-blue-600 hover:underline">
              ← Back to Invoices
            </button>
          )}
        </div>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">
              Invoice {invoice.invoice_number}
            </h1>
            <p className="text-gray-600">
              Due: {invoice.due_date} · Account:{" "}
              <button
                type="button"
                onClick={() => onViewAccount?.(invoice.account_id)}
                className="text-blue-600 hover:underline"
              >
                {invoice.account_id}
              </button>
            </p>
          </div>
          <div className="flex items-center gap-3">
            <span
              className={`px-3 py-1 rounded text-sm font-medium ${statusColor(invoice.status)}`}
            >
              {invoice.status}
            </span>
            {invoice.status !== "void" && invoice.status !== "paid" && (
              <button
                type="button"
                onClick={() => setVoidDialogOpen(true)}
                className="bg-red-600 text-white px-4 py-2 rounded hover:bg-red-700"
              >
                Void Invoice
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Summary cards */}
      <section aria-labelledby="invoice-summary-heading" className="mb-8">
        <h2 id="invoice-summary-heading" className="sr-only">
          Invoice Summary
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Subtotal</p>
            <p className="text-xl font-bold">{formatCents(invoice.subtotal_cents)}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Tax</p>
            <p className="text-xl font-bold">{formatCents(invoice.tax_cents)}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Total</p>
            <p className="text-xl font-bold">{formatCents(invoice.total_cents)}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Paid</p>
            <p className="text-xl font-bold">{formatCents(invoice.amount_paid_cents)}</p>
          </div>
          <div className="border rounded p-4">
            <p className="text-sm text-gray-600">Remaining</p>
            <p className="text-xl font-bold">{formatCents(invoice.remaining_cents)}</p>
          </div>
        </div>
      </section>

      {/* Line items */}
      <section aria-labelledby="line-items-heading" className="mb-8">
        <h2 id="line-items-heading" className="text-lg font-semibold mb-3">
          Line Items
        </h2>
        <table className="w-full border-collapse" role="table">
          <thead>
            <tr className="border-b bg-gray-50">
              <th className="text-left p-3 font-medium">Product</th>
              <th className="text-left p-3 font-medium">Quantity (gal)</th>
              <th className="text-left p-3 font-medium">Unit Price</th>
              <th className="text-left p-3 font-medium">Subtotal</th>
            </tr>
          </thead>
          <tbody>
            {invoice.line_items.map((item) => (
              <tr key={item.line_id} className="border-b">
                <td className="p-3">{item.product_code}</td>
                <td className="p-3">{item.quantity_gallons}</td>
                <td className="p-3">{formatCents(item.unit_price_cents)}</td>
                <td className="p-3">{formatCents(item.subtotal_cents)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* QBO push state */}
      <section aria-labelledby="qbo-heading" className="mb-8">
        <h2 id="qbo-heading" className="text-lg font-semibold mb-3">
          QBO Sync Status
        </h2>
        <div className="flex items-center gap-4">
          <span
            className={`px-3 py-1 rounded text-sm font-medium ${
              invoice.qbo_push_state === "pushed"
                ? "bg-green-100 text-green-800"
                : invoice.qbo_push_state === "dead_letter"
                  ? "bg-red-100 text-red-800"
                  : "bg-gray-100 text-gray-800"
            }`}
          >
            {invoice.qbo_push_state}
          </span>
          {invoice.qbo_push_state === "dead_letter" && (
            <button
              type="button"
              onClick={handleRetryQbo}
              className="text-blue-600 hover:underline text-sm"
            >
              Retry Push
            </button>
          )}
        </div>
      </section>

      {/* Event timeline */}
      <section aria-labelledby="timeline-heading" className="mb-8">
        <h2 id="timeline-heading" className="text-lg font-semibold mb-3">
          Event Timeline
        </h2>
        {events.length === 0 ? (
          <p className="text-gray-500">No events recorded.</p>
        ) : (
          <ol className="relative border-l border-gray-300 ml-4" aria-label="Invoice events">
            {events.map((event) => (
              <li key={event.event_id} className="mb-4 ml-6">
                <span className="absolute -left-2 w-4 h-4 bg-blue-600 rounded-full border-2 border-white" />
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
                <label htmlFor="void-reason" className="block text-sm font-medium mb-1">
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
                      Force void (reverse {formatCents(invoice.amount_paid_cents)} in applied payments)
                    </span>
                  </label>
                </div>
              )}
              <div className="flex gap-3">
                <button
                  type="submit"
                  disabled={voiding || !voidReason.trim()}
                  className="bg-red-600 text-white px-4 py-2 rounded hover:bg-red-700 disabled:opacity-50"
                >
                  {voiding ? "Voiding..." : "Confirm Void"}
                </button>
                <button
                  type="button"
                  onClick={() => setVoidDialogOpen(false)}
                  className="border px-4 py-2 rounded hover:bg-gray-50"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
