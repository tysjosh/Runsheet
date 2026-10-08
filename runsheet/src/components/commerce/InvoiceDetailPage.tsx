"use client";

/**
 * Invoice detail (UI revamp task 3.4): the detail template (title row with
 * back, status badge and actions), facts on the left and the event timeline
 * on the right, products by name, money/gallons/dates through `lib/format`,
 * and Void as an sm FormDialog (design.md §5).
 */
import { useRouter } from "next/navigation";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  Button,
  type Column,
  DataTable,
  EntityLink,
  Field,
  FormDialog,
  INPUT_CLASS,
  InlineBanner,
  LoadErrorState,
  PageHeader,
  ProductChip,
  Skeleton,
} from "@/components/ui";
import { calendarDate, dateTime, gallons, money } from "../../lib/format";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import type {
  Invoice,
  InvoiceEvent,
  InvoiceLineItem,
  VoidInvoicePayload,
} from "../../services/commerceApi";
import {
  finalizeInvoice,
  getInvoice,
  getInvoiceEvents,
  retryQboPush,
  voidInvoice,
} from "../../services/commerceApi";
import { InvoiceStatusBadge, QboStateBadge } from "./billingStatus";

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
  const router = useRouter();
  const [invoice, setInvoice] = useState<Invoice | null>(null);
  const [events, setEvents] = useState<InvoiceEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadFailure, setLoadFailure] = useState<LoadFailure | null>(null);
  const [voidDialogOpen, setVoidDialogOpen] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    setLoadFailure(null);
    try {
      const [invoiceRes, eventsRes] = await Promise.all([
        getInvoice(invoiceId),
        getInvoiceEvents(invoiceId),
      ]);
      setInvoice(invoiceRes.data);
      setEvents(eventsRes.data);
    } catch (err) {
      setLoadFailure(classifyLoadError(err, "Failed to load invoice details"));
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
      (process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8080") +
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

  const submitVoid = async (v: VoidValues): Promise<Invoice> => {
    const payload: VoidInvoicePayload = {
      reason: v.reason.trim(),
      force: v.force,
    };
    const res = await voidInvoice(invoiceId, payload);
    return res.data;
  };

  const afterVoid = async (updated: Invoice) => {
    setInvoice(updated);
    try {
      const eventsRes = await getInvoiceEvents(invoiceId);
      setEvents(eventsRes.data);
    } catch {
      // The timeline also updates over the socket.
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

  const handleFinalize = async () => {
    setFinalizing(true);
    setError(null);
    try {
      const res = await finalizeInvoice(invoiceId);
      setInvoice(res.data);
      const eventsRes = await getInvoiceEvents(invoiceId);
      setEvents(eventsRes.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to finalize invoice",
      );
    } finally {
      setFinalizing(false);
    }
  };

  const back = () => (onBack ? onBack() : router.back());

  if (loading) {
    return (
      <div className="p-4">
        <Skeleton rows={6} label="Loading invoice details" />
      </div>
    );
  }

  if (loadFailure) {
    return (
      <LoadErrorState
        failure={loadFailure}
        entityLabel="Invoice"
        entityId={invoiceId}
        onBack={back}
        backLabel="Back to Invoices"
        homeHref="/dashboard/billing"
        homeLabel="Go to Billing"
        onRetry={fetchData}
      />
    );
  }

  if (!invoice) return null;

  const lineColumns: Column<InvoiceLineItem>[] = [
    {
      key: "product_code",
      header: "Product",
      cell: (item) => <ProductChip code={item.product_code} variant="full" />,
    },
    {
      key: "quantity_gallons",
      header: "Quantity",
      align: "right",
      width: 140,
      className: "tabular-nums",
      cell: (item) =>
        gallons(item.quantity_gallons, {
          decimals: Number.isInteger(item.quantity_gallons) ? 0 : 2,
        }),
    },
    {
      key: "unit_price_cents",
      header: "Unit price",
      align: "right",
      width: 130,
      className: "tabular-nums",
      cell: (item) => money(item.unit_price_cents / 100),
    },
    {
      key: "subtotal_cents",
      header: "Subtotal",
      align: "right",
      width: 140,
      className: "tabular-nums font-medium",
      cell: (item) => money(item.subtotal_cents / 100),
    },
  ];

  const dr = invoice.delivery_result;
  const canVoid = invoice.status !== "void" && invoice.status !== "paid";

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        host
        title={`Invoice ${invoice.invoice_number}`}
        back={{ label: "Back to Invoices", onClick: back }}
        badge={<InvoiceStatusBadge status={invoice.status} />}
        counts={
          <span className="whitespace-nowrap">
            Due {calendarDate(invoice.due_date)}
          </span>
        }
        actions={
          <>
            {invoice.status === "draft" && (
              <Button
                size="sm"
                onClick={handleFinalize}
                loading={finalizing}
                disabled={finalizing}
              >
                {finalizing ? "Finalizing…" : "Approve & send to ERP"}
              </Button>
            )}
            {canVoid && (
              <Button
                size="sm"
                variant="danger"
                onClick={() => setVoidDialogOpen(true)}
              >
                Void invoice
              </Button>
            )}
          </>
        }
      />
      <div className="flex-1 overflow-auto p-4">
        {error && (
          <div role="alert" className="mb-3">
            <InlineBanner tone="critical">{error}</InlineBanner>
          </div>
        )}
        <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <div className="min-w-0">
            <section aria-labelledby="invoice-summary-heading" className="mb-6">
              <h2 id="invoice-summary-heading" className="sr-only">
                Invoice summary
              </h2>
              <dl className="grid grid-cols-2 gap-3 md:grid-cols-5">
                <Fact
                  label="Subtotal"
                  value={money(invoice.subtotal_cents / 100)}
                />
                <Fact label="Tax" value={money(invoice.tax_cents / 100)} />
                <Fact label="Total" value={money(invoice.total_cents / 100)} />
                <Fact
                  label="Paid"
                  value={money(invoice.amount_paid_cents / 100)}
                />
                <Fact
                  label="Remaining"
                  value={money(invoice.remaining_cents / 100)}
                />
              </dl>
            </section>

            <section aria-labelledby="refs-heading" className="mb-6">
              <h2
                id="refs-heading"
                className="mb-2 text-sm font-semibold text-text"
              >
                References
              </h2>
              {/* Account traverses in-hub when a callback is supplied;
                  customer and order link to their canonical routes
                  (Req 12.1, 13.1). */}
              <dl className="grid grid-cols-1 gap-x-6 gap-y-3 rounded-lg border border-slate-200 p-4 text-sm md:grid-cols-3">
                <Row label="Account">
                  {onViewAccount ? (
                    <button
                      type="button"
                      onClick={() => onViewAccount(invoice.account_id)}
                      className="text-link underline underline-offset-2 hover:no-underline focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                    >
                      {invoice.account_id}
                    </button>
                  ) : (
                    <EntityLink type="account" id={invoice.account_id} />
                  )}
                </Row>
                <Row label="Customer">
                  <EntityLink type="customer" id={invoice.customer_id} />
                </Row>
                <Row label="Order">
                  <EntityLink type="order" id={invoice.order_id} />
                </Row>
                <Row label="Issued">{calendarDate(invoice.issued_at)}</Row>
                <Row label="QuickBooks">
                  <span className="inline-flex items-center gap-2">
                    <QboStateBadge state={invoice.qbo_push_state} />
                    {invoice.qbo_push_state === "dead_letter" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={handleRetryQbo}
                      >
                        Retry push
                      </Button>
                    )}
                  </span>
                </Row>
                {invoice.void_reason && (
                  <Row label="Void reason">{invoice.void_reason}</Row>
                )}
              </dl>
            </section>

            {dr && (
              <section
                aria-labelledby="delivery-result-heading"
                className="mb-6"
              >
                <h2
                  id="delivery-result-heading"
                  className="mb-2 text-sm font-semibold text-text"
                >
                  Delivery result
                </h2>
                <dl className="grid grid-cols-1 gap-x-6 gap-y-3 rounded-lg border border-slate-200 p-4 text-sm md:grid-cols-3">
                  <Row label="Actual gallons">
                    {gallons(dr.actual_gallons, {
                      decimals: Number.isInteger(dr.actual_gallons) ? 0 : 2,
                    })}
                  </Row>
                  <Row label="Delivered">{dateTime(dr.delivered_at)}</Row>
                  <Row label="POD">{dr.pod_id}</Row>
                  <Row label="Received by">{dr.recipient_name}</Row>
                  <Row label="Source ERP">
                    {dr.source_system || "Not imported"}
                  </Row>
                  {dr.source_record_id && (
                    <Row label="Source record">{dr.source_record_id}</Row>
                  )}
                </dl>
              </section>
            )}

            <section aria-labelledby="line-items-heading" className="mb-6">
              <h2
                id="line-items-heading"
                className="mb-2 text-sm font-semibold text-text"
              >
                Line items
              </h2>
              <div className="overflow-hidden rounded-lg border border-slate-200">
                <DataTable<InvoiceLineItem>
                  ariaLabel="Line items"
                  columns={lineColumns}
                  data={invoice.line_items}
                  getRowId={(item) => item.line_id}
                  emptyState={
                    <p className="text-sm text-text-muted">No line items.</p>
                  }
                />
              </div>
            </section>
          </div>

          <section aria-labelledby="timeline-heading" className="min-w-0">
            <h2
              id="timeline-heading"
              className="mb-2 text-sm font-semibold text-text"
            >
              Activity
            </h2>
            {events.length === 0 ? (
              <p className="text-sm text-text-muted">No events recorded.</p>
            ) : (
              <ol
                className="relative ml-2 border-l border-slate-300"
                aria-label="Invoice events"
              >
                {events.map((event) => (
                  <li key={event.event_id} className="mb-4 ml-4">
                    <span
                      aria-hidden="true"
                      className="absolute -left-1.5 h-3 w-3 rounded-full border-2 border-white bg-primary"
                    />
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium text-text">
                        {event.event_type.replace(/_/g, " ")}
                      </span>
                      <span className="text-xs text-text-muted">
                        {dateTime(event.occurred_at)}
                      </span>
                    </div>
                    <p className="text-xs text-slate-700">
                      by {event.actor}
                      {event.payload &&
                        Object.keys(event.payload).length > 0 && (
                          <span className="ml-2 break-all text-text-muted">
                            {JSON.stringify(event.payload)}
                          </span>
                        )}
                    </p>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </div>
      </div>

      {voidDialogOpen && (
        <FormDialog<VoidValues, Invoice>
          open
          size="sm"
          title="Void invoice"
          help="This can't be undone. The invoice is marked void and any applied payments are reversed."
          submitLabel="Confirm void"
          successMessage="Invoice voided"
          initialValues={{ reason: "", force: false }}
          validate={(v) => ({
            reason: v.reason.trim() ? undefined : "Enter a reason.",
          })}
          onSubmit={submitVoid}
          onSaved={(updated) => void afterVoid(updated)}
          onClose={() => setVoidDialogOpen(false)}
        >
          {({ values, set, errors }) => (
            <>
              <Field label="Reason" required error={errors.reason}>
                <textarea
                  id="void-reason"
                  value={values.reason}
                  onChange={(e) => set("reason", e.target.value)}
                  rows={3}
                  placeholder="Why this invoice is being voided"
                  className={`${INPUT_CLASS} h-auto py-1.5`}
                />
              </Field>
              {invoice.amount_paid_cents > 0 && (
                <label className="col-span-2 flex items-center gap-2 text-sm text-text">
                  <input
                    type="checkbox"
                    checked={values.force}
                    onChange={(e) => set("force", e.target.checked)}
                  />
                  <span>
                    Force void (reverse {money(invoice.amount_paid_cents / 100)}{" "}
                    in applied payments)
                  </span>
                </label>
              )}
            </>
          )}
        </FormDialog>
      )}
    </div>
  );
}

type VoidValues = { reason: string; force: boolean };

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2">
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums text-text">{value}</dd>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="font-medium text-text">{children}</dd>
    </div>
  );
}
