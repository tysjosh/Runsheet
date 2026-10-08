"use client";

/**
 * Order Detail Page — renders the Fuel_Order document, chronological
 * event timeline, intake_metadata (channel-specific), assigned driver
 * card, linked POD, and storm mode banner.
 *
 * Validates: Requirements 8.2.1, 8.2.2, 8.2.3
 */

import {
  AlertTriangle,
  ArrowLeft,
  Clock,
  Loader2,
  Truck,
  User,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  EntityLink,
  entityHref,
  Field,
  FormDialog,
  INPUT_CLASS,
  Select,
  ToastContainer,
  useToasts,
} from "@/components/ui";
import { hasAnyRole } from "../../config/modules";
import { ApiError } from "../../services/api";
import { PORTAL_REVIEW_HOLD_REASON } from "../../services/orderHoldReasons";
import {
  type AssignDriverPayload,
  assignDriver,
  cancelOrder,
  type FuelOrder,
  type FuelOrderEvent,
  getOrder,
  getOrderEvents,
  holdOrder,
  type OrderStatus,
  type ResolvedLink,
  releaseHoldOrder,
  updateOrderStatus,
} from "../../services/ordersApi";
import { getCurrentUserRoles } from "../../utils/auth";
import DriverPicker from "../ops/DriverPicker";
import { PageTitle } from "../ui/PageHeader";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatDateTime(dateStr?: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function getStatusColor(status: OrderStatus): string {
  const map: Record<string, string> = {
    placed: "bg-info-light text-info-dark",
    confirmed: "bg-brand-secondary-soft text-brand-secondary",
    scheduled: "bg-brand-secondary-soft text-brand-secondary",
    dispatched: "bg-info-light text-info-dark",
    in_transit: "bg-warning-light text-warning-dark",
    delivered: "bg-success-light text-success-dark",
    failed: "bg-error-light text-error-dark",
    cancelled: "bg-gray-100 text-gray-700",
    on_hold: "bg-warning-light text-warning-dark",
  };
  return map[status] ?? "bg-gray-100 text-gray-700";
}

// ─── Linked Reference Field (cross-module-entity-linkage Req 2.4, 13.1) ───────

/** Pull a human label out of a resolved reference summary. */
function summaryLabel(summary: Record<string, unknown>): string | undefined {
  for (const key of [
    "display_name",
    "legal_name",
    "name",
    "driver_name",
    "customer_name",
  ]) {
    const value = summary[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

interface LinkedRefFieldProps {
  /** The resolver link for this reference (undefined when not expanded). */
  link: ResolvedLink | undefined;
  /** Fallback id from the order document when the link was not expanded. */
  fallbackId?: string | null;
  /** Builds the destination href for a resolvable id. */
  href: (id: string) => string;
}

/**
 * Renders a cross-module reference as navigation to the owning module when it
 * resolves, or an explicit "Unlinked" affordance when it does not — never an
 * inert id string for a dangling reference (Req 2.4, 13.1, 13.3). Mirrors the
 * ``LinkedRefField`` used by the Job detail view for cross-module consistency.
 */
function LinkedRefField({ link, fallbackId, href }: LinkedRefFieldProps) {
  if (link?.status === "resolved") {
    const display = summaryLabel(link.summary) ?? link.id;
    return (
      <Link
        href={href(link.id)}
        className="text-info hover:text-info-dark underline underline-offset-2"
      >
        {display}
        {display !== link.id && (
          <span className="text-gray-500"> ({link.id})</span>
        )}
      </Link>
    );
  }

  if (link?.status === "unresolved") {
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className="text-gray-500">{link.id}</span>
        <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium text-warning-dark bg-warning-light">
          Unlinked
        </span>
      </span>
    );
  }

  if (fallbackId) {
    // Not expanded but the order document carries the id — link optimistically.
    return (
      <Link
        href={href(fallbackId)}
        className="text-info hover:text-info-dark underline underline-offset-2"
      >
        {fallbackId}
      </Link>
    );
  }

  return <span className="text-gray-500">—</span>;
}

// ─── Intake Metadata Renderer ────────────────────────────────────────────────

function IntakeMetadataSection({ order }: { order: FuelOrder }) {
  // The backend OrderResponse does not always include intake_metadata, so
  // default to an empty object and guard the channel string.
  const meta = order.intake_metadata ?? {};
  const channel = order.intake_channel ?? "";

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
      <h3 className="text-sm font-semibold text-primary mb-3">
        Intake Metadata
      </h3>
      <div className="space-y-2 text-sm">
        <div className="flex justify-between">
          <span className="text-gray-500">Channel</span>
          <span className="font-medium">{channel.replace("_", " ")}</span>
        </div>

        {/* Voice channel */}
        {channel === "voice" && (
          <>
            {meta.transcript && (
              <div>
                <span className="text-gray-500 block mb-1">Transcript</span>
                <p className="text-gray-700 bg-gray-50 rounded-lg p-3 text-xs whitespace-pre-wrap">
                  {meta.transcript}
                </p>
              </div>
            )}
            {meta.recording_url && (
              <div>
                <span className="text-gray-500 block mb-1">Recording</span>
                {/* biome-ignore lint/a11y/useMediaCaption: Captions are not provided with call recordings; transcripts render above when available. */}
                <audio controls className="w-full" aria-label="Call recording">
                  <source src={meta.recording_url} />
                  Your browser does not support the audio element.
                </audio>
              </div>
            )}
            {meta.agent_confidence != null && (
              <div className="flex justify-between">
                <span className="text-gray-500">Agent Confidence</span>
                <span>{(meta.agent_confidence * 100).toFixed(0)}%</span>
              </div>
            )}
            {meta.call_id && (
              <div className="flex justify-between">
                <span className="text-gray-500">Call ID</span>
                <span className="font-mono text-xs">{meta.call_id}</span>
              </div>
            )}
          </>
        )}

        {/* Dispatcher channel */}
        {channel === "dispatcher" && (
          <>
            {meta.dispatcher_user_id && (
              <div className="flex justify-between">
                <span className="text-gray-500">Dispatcher</span>
                <span className="font-mono text-xs">
                  {meta.dispatcher_user_id}
                </span>
              </div>
            )}
            {meta.session_id && (
              <div className="flex justify-between">
                <span className="text-gray-500">Session</span>
                <span className="font-mono text-xs">{meta.session_id}</span>
              </div>
            )}
          </>
        )}

        {/* CSV channel */}
        {channel === "csv" && (
          <>
            {meta.import_batch_id && (
              <div className="flex justify-between">
                <span className="text-gray-500">Import Batch</span>
                <a
                  href={`/dashboard/settings?tab=import&batch=${encodeURIComponent(meta.import_batch_id)}`}
                  className="text-info hover:underline text-xs font-mono"
                >
                  {meta.import_batch_id}
                </a>
              </div>
            )}
            {meta.csv_row_number != null && (
              <div className="flex justify-between">
                <span className="text-gray-500">Row Number</span>
                <span>{meta.csv_row_number}</span>
              </div>
            )}
          </>
        )}

        {/* EDI / API Partner */}
        {channel === "edi" && meta.edi_interchange_id && (
          <div className="flex justify-between">
            <span className="text-gray-500">EDI Interchange</span>
            <span className="font-mono text-xs">{meta.edi_interchange_id}</span>
          </div>
        )}
        {meta.partner_ref && (
          <div className="flex justify-between">
            <span className="text-gray-500">Partner Ref</span>
            <span className="font-mono text-xs">{meta.partner_ref}</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Event Timeline ──────────────────────────────────────────────────────────

function EventTimeline({ events }: { events: FuelOrderEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-gray-500">No events recorded.</p>;
  }

  return (
    <div className="space-y-3">
      {events.map((event) => (
        <div key={event.event_id} className="flex gap-3">
          <div className="flex flex-col items-center">
            <div className="w-2.5 h-2.5 rounded-full bg-primary mt-1.5" />
            <div className="w-px flex-1 bg-gray-200" />
          </div>
          <div className="pb-4">
            <p className="text-sm font-medium text-primary">
              {event.event_type.replace(/_/g, " ")}
            </p>
            <p className="text-xs text-gray-500">
              {formatDateTime(event.event_timestamp)}
            </p>
            {event.event_payload &&
              Object.keys(event.event_payload).length > 0 && (
                <pre className="mt-1 text-xs text-gray-600 bg-gray-50 rounded p-2 overflow-x-auto">
                  {JSON.stringify(event.event_payload, null, 2)}
                </pre>
              )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Portal request confirm / decline (PD24) ─────────────────────────────────

/** Shown after a 409 INVALID_STATUS_TRANSITION reload (design §10.3). */
export const PORTAL_REQUEST_CHANGED_MESSAGE =
  "This request changed. Reloaded the latest version.";

function isStaleTransition(err: unknown): boolean {
  const e = err as { status?: unknown; code?: unknown } | null;
  return e?.status === 409 && e?.code === "INVALID_STATUS_TRANSITION";
}

/**
 * Confirm (`release-hold` with `notes: "confirmed"`) or Decline (`cancel`
 * with `reason: "declined_by_dispatcher"`) a customer-portal request that is
 * awaiting confirmation. Admin and dispatcher only; the API re-checks.
 */
function PortalRequestControls({
  order,
  roles,
  onChanged,
  addToast,
}: {
  order: FuelOrder;
  roles: readonly string[] | null;
  onChanged: (notice: string | null) => void;
  addToast: (message: string, type: "success" | "error") => void;
}) {
  const [working, setWorking] = useState<"confirm" | "decline" | null>(null);
  if (
    order.status !== "on_hold" ||
    order.hold_reason !== PORTAL_REVIEW_HOLD_REASON ||
    !hasAnyRole(roles, ["admin", "dispatcher"])
  ) {
    return null;
  }

  const run = async (action: "confirm" | "decline") => {
    if (working) return;
    setWorking(action);
    try {
      if (action === "confirm") {
        await releaseHoldOrder(order.order_id, { notes: "confirmed" });
        addToast("Request confirmed", "success");
      } else {
        await cancelOrder(order.order_id, { reason: "declined_by_dispatcher" });
        addToast("Request declined", "success");
      }
      onChanged(null);
    } catch (err) {
      if (isStaleTransition(err)) {
        onChanged(PORTAL_REQUEST_CHANGED_MESSAGE);
      } else {
        addToast(
          err instanceof ApiError
            ? err.message
            : action === "confirm"
              ? "Failed to confirm the request"
              : "Failed to decline the request",
          "error",
        );
      }
    } finally {
      setWorking(null);
    }
  };

  return (
    <div
      className="flex flex-wrap items-center gap-3 rounded-xl border border-warning-light bg-warning-light px-4 py-3"
      data-testid="portal-request-controls"
    >
      <p className="flex-1 text-sm font-medium text-warning-dark">
        Customer portal request awaiting confirmation
      </p>
      <button
        type="button"
        onClick={() => run("confirm")}
        disabled={working !== null}
        className="px-3 py-1.5 text-xs font-medium text-white bg-success-dark rounded-lg hover:opacity-90 disabled:opacity-50 inline-flex items-center gap-1"
      >
        {working === "confirm" && <Loader2 className="w-3 h-3 animate-spin" />}
        Confirm
      </button>
      <button
        type="button"
        onClick={() => run("decline")}
        disabled={working !== null}
        className="px-3 py-1.5 text-xs font-medium text-error-dark border border-error-light bg-white rounded-lg hover:bg-error-light disabled:opacity-50 inline-flex items-center gap-1"
      >
        {working === "decline" && <Loader2 className="w-3 h-3 animate-spin" />}
        Decline
      </button>
    </div>
  );
}

// ─── Mutation Controls ───────────────────────────────────────────────────────

interface MutationControlsProps {
  order: FuelOrder;
  onMutationSuccess: () => void;
  addToast: (message: string, type: "success" | "error") => void;
}

const STATUS_OPTIONS: { value: OrderStatus; label: string }[] = [
  { value: "confirmed", label: "Confirmed" },
  { value: "scheduled", label: "Scheduled" },
  { value: "dispatched", label: "Dispatched" },
  { value: "in_transit", label: "In transit" },
  { value: "delivered", label: "Delivered" },
  { value: "failed", label: "Failed" },
  { value: "on_hold", label: "On hold" },
];

/** API message for a failed mutation (FormDialog shows it in the dialog). */
function mutationError(err: unknown, fallback: string): Error {
  return new Error(err instanceof ApiError ? err.message : fallback);
}

/**
 * Order actions (UI revamp, design.md §5 "Order status / hold": sm
 * FormDialog). Change status, Assign driver, Place on hold and Cancel order
 * each open a small FormDialog: validation and API errors stay inline in the
 * dialog, success closes it and shows the page toast. Release hold has no
 * input and stays a direct action.
 */
function MutationControls({
  order,
  onMutationSuccess,
  addToast,
}: MutationControlsProps) {
  const [open, setOpen] = useState<
    "assign" | "cancel" | "status" | "hold" | null
  >(null);
  const [working, setWorking] = useState(false);
  const close = () => setOpen(null);
  const done = (message: string) => () => {
    addToast(message, "success");
    onMutationSuccess();
  };

  // Release from hold (re-runs intake hooks server-side). The backend may
  // keep the order on_hold with a refreshed hold_reason when a re-run intake
  // hook fails, so we re-fetch and surface the resulting status to the user
  // rather than assuming success.
  const handleReleaseHold = useCallback(async () => {
    setWorking(true);
    try {
      await releaseHoldOrder(order.order_id);
      addToast("Release requested — refreshing order", "success");
      onMutationSuccess();
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : "Failed to release hold";
      addToast(msg, "error");
    } finally {
      setWorking(false);
    }
  }, [order.order_id, addToast, onMutationSuccess]);

  const isTerminal = ["delivered", "failed", "cancelled"].includes(
    order.status,
  );
  const isOnHold = order.status === "on_hold";
  // The state machine only allows placed/confirmed/scheduled → on_hold.
  const canHold = ["placed", "confirmed", "scheduled"].includes(order.status);

  return (
    <>
      <div className="flex flex-wrap gap-2">
        {!isTerminal && (
          <>
            <button
              type="button"
              onClick={() => setOpen("status")}
              className="px-3 py-1.5 text-xs font-medium border border-gray-200 rounded-lg hover:bg-gray-50"
              aria-label="Change status"
            >
              Change Status
            </button>
            <button
              type="button"
              onClick={() => setOpen("assign")}
              className="px-3 py-1.5 text-xs font-medium border border-gray-200 rounded-lg hover:bg-gray-50"
              aria-label="Assign driver"
            >
              Assign Driver
            </button>
            {isOnHold ? (
              <button
                type="button"
                onClick={handleReleaseHold}
                disabled={working}
                className="px-3 py-1.5 text-xs font-medium text-success-dark border border-success-light rounded-lg hover:bg-success-light disabled:opacity-50 inline-flex items-center gap-1"
                aria-label="Release hold"
              >
                {working && <Loader2 className="w-3 h-3 animate-spin" />}
                Release Hold
              </button>
            ) : (
              canHold && (
                <button
                  type="button"
                  onClick={() => setOpen("hold")}
                  className="px-3 py-1.5 text-xs font-medium text-warning-dark border border-warning-light rounded-lg hover:bg-warning-light"
                  aria-label="Place on hold"
                >
                  Place on Hold
                </button>
              )
            )}
            <button
              type="button"
              onClick={() => setOpen("cancel")}
              className="px-3 py-1.5 text-xs font-medium text-error-dark border border-error-light rounded-lg hover:bg-error-light"
              aria-label="Cancel order"
            >
              Cancel Order
            </button>
          </>
        )}
      </div>

      {open === "assign" && (
        <FormDialog<{ driver_id: string }>
          open
          size="sm"
          title="Assign driver"
          submitLabel="Assign"
          successMessage={null}
          initialValues={{ driver_id: "" }}
          validate={(v) =>
            v.driver_id.trim() ? {} : { driver_id: "Choose a driver." }
          }
          onSubmit={async (v) => {
            const payload: AssignDriverPayload = {
              driver_id: v.driver_id.trim(),
            };
            try {
              return await assignDriver(order.order_id, payload);
            } catch (err) {
              throw mutationError(err, "Failed to assign driver");
            }
          }}
          onSaved={done("Driver assigned successfully")}
          onClose={close}
        >
          {({ values, set, errors }) => (
            <div className="col-span-2">
              <DriverPicker
                value={values.driver_id || null}
                onChange={(id) => set("driver_id", id)}
                aria-label="Driver"
              />
              {errors.driver_id && (
                <p className="mt-1 text-xs text-red-700">{errors.driver_id}</p>
              )}
            </div>
          )}
        </FormDialog>
      )}

      {open === "cancel" && (
        <FormDialog<{ reason: string }>
          open
          size="sm"
          title="Cancel order"
          help="This action cannot be undone. Please provide a reason for cancellation."
          submitLabel="Confirm cancel"
          successMessage={null}
          initialValues={{ reason: "" }}
          validate={(v) =>
            v.reason.trim() ? {} : { reason: "Enter a reason." }
          }
          onSubmit={async (v) => {
            try {
              return await cancelOrder(order.order_id, {
                reason: v.reason.trim(),
              });
            } catch (err) {
              throw mutationError(err, "Failed to cancel order");
            }
          }}
          onSaved={done("Order cancelled")}
          onClose={close}
        >
          {({ values, set, errors }) => (
            <Field label="Cancellation reason" required error={errors.reason}>
              <textarea
                id="order-cancel-reason"
                value={values.reason}
                onChange={(e) => set("reason", e.target.value)}
                placeholder="Reason for cancellation"
                rows={3}
                className={INPUT_CLASS}
              />
            </Field>
          )}
        </FormDialog>
      )}

      {open === "hold" && (
        <FormDialog<{ reason: string }>
          open
          size="sm"
          title="Place on hold"
          help="Holding pauses the order until it is released (for example a credit check, or awaiting customer confirmation)."
          submitLabel="Place on hold"
          successMessage={null}
          initialValues={{ reason: "" }}
          validate={(v) =>
            v.reason.trim() ? {} : { reason: "Enter a reason." }
          }
          onSubmit={async (v) => {
            try {
              return await holdOrder(order.order_id, {
                hold_reason: v.reason.trim(),
              });
            } catch (err) {
              throw mutationError(err, "Failed to place order on hold");
            }
          }}
          onSaved={done("Order placed on hold")}
          onClose={close}
        >
          {({ values, set, errors }) => (
            <Field label="Hold reason" required error={errors.reason}>
              <textarea
                id="order-hold-reason"
                value={values.reason}
                onChange={(e) => set("reason", e.target.value)}
                placeholder="Reason for hold"
                rows={3}
                className={INPUT_CLASS}
              />
            </Field>
          )}
        </FormDialog>
      )}

      {open === "status" && (
        <FormDialog<{ new_status: OrderStatus; reason: string }>
          open
          size="sm"
          title="Change status"
          submitLabel="Update status"
          successMessage={null}
          initialValues={{ new_status: "confirmed", reason: "" }}
          onSubmit={async (v) => {
            try {
              await updateOrderStatus(order.order_id, {
                new_status: v.new_status,
                reason: v.reason.trim() || undefined,
              });
              return v.new_status;
            } catch (err) {
              throw mutationError(err, "Failed to change status");
            }
          }}
          onSaved={(s) => {
            const label = STATUS_OPTIONS.find((o) => o.value === s)?.label ?? s;
            addToast(`Status changed to ${label}`, "success");
            onMutationSuccess();
          }}
          onClose={close}
        >
          {({ values, set }) => (
            <>
              <Field label="New status" id="order-new-status">
                <Select
                  id="order-new-status"
                  value={values.new_status}
                  onChange={(v) => set("new_status", v as OrderStatus)}
                  options={STATUS_OPTIONS}
                />
              </Field>
              <Field label="Reason">
                <input
                  id="order-status-reason"
                  type="text"
                  value={values.reason}
                  onChange={(e) => set("reason", e.target.value)}
                  placeholder="Optional"
                  className={INPUT_CLASS}
                />
              </Field>
            </>
          )}
        </FormDialog>
      )}
    </>
  );
}

// ─── Main Page Component ─────────────────────────────────────────────────────

export default function OrderDetailView({
  orderId,
  onBack,
}: {
  /** The order to render. */
  orderId: string;
  /** Back affordance (in-shell pop, or browser back on the route). */
  onBack: () => void;
}) {
  const goBack = onBack;

  const [order, setOrder] = useState<FuelOrder | null>(null);
  const [events, setEvents] = useState<FuelOrderEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { toasts, addToast, dismissToast } = useToasts();
  // Roles gate the portal Confirm / Decline controls (presentation only).
  const [roles, setRoles] = useState<readonly string[] | null>(null);
  const [changedNotice, setChangedNotice] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getCurrentUserRoles().then((r) => {
      if (!cancelled) setRoles(r ?? []);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const fetchData = useCallback(async () => {
    if (!orderId) return;
    setLoading(true);
    setError(null);
    try {
      const [orderRes, eventsRes] = await Promise.all([
        getOrder(orderId, { expand: ["customer", "asset", "driver"] }),
        getOrderEvents(orderId),
      ]);
      setOrder(orderRes);
      setEvents(eventsRes.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load order");
    } finally {
      setLoading(false);
    }
  }, [orderId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Loader2 className="w-6 h-6 text-gray-500 animate-spin" />
      </div>
    );
  }

  if (error || !order) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          {/* One heading even when the order can't be shown (R2.5). */}
          <PageTitle className="mb-1 text-base font-semibold text-slate-900">
            Order not found
          </PageTitle>
          <p className="text-red-800 mb-4">
            {error ?? `We couldn't find order "${orderId}".`}
          </p>
          <button
            type="button"
            onClick={goBack}
            className="text-sm text-info hover:underline"
          >
            Go back
          </button>
        </div>
      </div>
    );
  }

  // Storm mode detection — check if any event references a storm_event_id
  const stormEvent = events.find(
    (e) =>
      e.event_payload &&
      (e.event_payload as Record<string, unknown>).storm_event_id,
  );
  const stormEventId = stormEvent
    ? ((stormEvent.event_payload as Record<string, unknown>)
        .storm_event_id as string)
    : null;

  return (
    <div className="min-h-screen bg-gray-50">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />

      <div className="max-w-5xl mx-auto px-6 py-6 space-y-6">
        {/* Back + Header */}
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={goBack}
            className="p-2 rounded-lg hover:bg-gray-100"
            aria-label="Go back"
          >
            <ArrowLeft className="w-5 h-5 text-gray-600" />
          </button>
          <div className="flex-1">
            <PageTitle className="text-xl font-semibold text-primary">
              Order {order.order_id.slice(0, 16)}…
            </PageTitle>
            <p className="text-sm text-gray-500">
              Created {formatDateTime(order.created_at)}
            </p>
          </div>
          <span
            className={`px-3 py-1 rounded-lg text-sm font-medium ${getStatusColor(order.status)}`}
          >
            {order.status.replace("_", " ")}
          </span>
        </div>

        {/* Storm Mode Banner */}
        {stormEventId && (
          <div
            className="flex items-center gap-3 rounded-xl border border-warning-light bg-warning-light px-4 py-3"
            role="alert"
            data-testid="storm-mode-banner"
          >
            <AlertTriangle className="w-5 h-5 text-warning-dark" />
            <div>
              <p className="text-sm font-semibold text-warning-dark">
                Storm Mode Active
              </p>
              <p className="text-xs text-warning-dark">
                This order was received during storm event{" "}
                <span className="font-mono">{stormEventId}</span>
              </p>
            </div>
          </div>
        )}

        {/* Customer-portal request: Confirm / Decline (PD24) */}
        <p
          role="status"
          className={changedNotice ? "text-sm text-gray-800" : "sr-only"}
        >
          {changedNotice ?? ""}
        </p>
        <PortalRequestControls
          order={order}
          roles={roles}
          onChanged={(notice) => {
            setChangedNotice(notice);
            fetchData();
          }}
          addToast={addToast}
        />

        {/* Mutation Controls (Task 14.5) */}
        <MutationControls
          order={order}
          onMutationSuccess={fetchData}
          addToast={addToast}
        />

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Main content */}
          <div className="lg:col-span-2 space-y-6">
            {/* Order Details */}
            <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
              <h3 className="text-sm font-semibold text-primary mb-3">
                Order Details
              </h3>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                <dt className="text-gray-500">Customer</dt>
                <dd className="text-gray-900">
                  <EntityLink
                    type="customer"
                    id={order.customer_id}
                    label={order.customer_name || undefined}
                    link={order.links?.customer}
                  />
                </dd>

                <dt className="text-gray-500">Address</dt>
                <dd className="text-gray-900">{order.ship_to_address}</dd>

                <dt className="text-gray-500">Product</dt>
                <dd className="text-gray-900">{order.product_code ?? "—"}</dd>

                <dt className="text-gray-500">Volume</dt>
                <dd className="text-gray-900">
                  {order.fill_to_full
                    ? "Fill to Full"
                    : order.gallons_requested
                      ? `${order.gallons_requested} gal`
                      : "—"}
                </dd>

                <dt className="text-gray-500">Call Type</dt>
                <dd className="text-gray-900">
                  {order.call_type.replace("_", " ")}
                </dd>

                <dt className="text-gray-500">Delivery Window</dt>
                <dd className="text-gray-900">
                  {order.delivery_window_start
                    ? `${formatDateTime(order.delivery_window_start)} — ${formatDateTime(order.delivery_window_end)}`
                    : "Not set"}
                </dd>

                {order.po_number && (
                  <>
                    <dt className="text-gray-500">PO Number</dt>
                    <dd className="text-gray-900">{order.po_number}</dd>
                  </>
                )}

                {order.special_instructions && (
                  <>
                    <dt className="text-gray-500">Instructions</dt>
                    <dd className="text-gray-900">
                      {order.special_instructions}
                    </dd>
                  </>
                )}

                {order.hold_reason && (
                  <>
                    <dt className="text-gray-500">Hold Reason</dt>
                    <dd className="text-warning-dark font-medium">
                      {order.hold_reason}
                    </dd>
                  </>
                )}
              </dl>
            </div>

            {/* Event Timeline */}
            <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
              <h3 className="text-sm font-semibold text-primary mb-3 flex items-center gap-2">
                <Clock className="w-4 h-4" />
                Event Timeline
              </h3>
              <EventTimeline events={events} />
            </div>

            {/* POD section when delivered */}
            {order.status === "delivered" && (
              <div
                className="bg-white rounded-xl shadow-sm border border-success-light p-4"
                data-testid="pod-section"
              >
                <h3 className="text-sm font-semibold text-success-dark mb-2">
                  Proof of Delivery
                </h3>
                <p className="text-sm text-gray-600">
                  Delivery completed. POD linked to order {order.order_id}.
                </p>
              </div>
            )}
          </div>

          {/* Sidebar */}
          <div className="space-y-6">
            {/* Assigned Driver Card — navigable to the Drivers module (Req 2.4,
                13.1); an unresolved reference shows an explicit "Unlinked"
                affordance rather than a dead id string (Req 13.3). */}
            {(order.assigned_driver_id ||
              order.links?.driver?.status === "resolved" ||
              order.links?.driver?.status === "unresolved") && (
              <div
                className="bg-white rounded-xl shadow-sm border border-gray-100 p-4"
                data-testid="assigned-driver-card"
              >
                <h3 className="text-sm font-semibold text-primary mb-3 flex items-center gap-2">
                  <User className="w-4 h-4" />
                  Assigned Driver
                </h3>
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-full bg-gray-100 flex items-center justify-center">
                    <User className="w-5 h-5 text-gray-500" />
                  </div>
                  <div>
                    <p className="text-sm font-medium">
                      <LinkedRefField
                        link={order.links?.driver}
                        fallbackId={order.assigned_driver_id}
                        // Resolve through the canonical route map rather than a
                        // hardcoded path. This used to point at
                        // `/ops/drivers?driver=`, which is not a route (the
                        // directory has no `page.tsx`) and so 404'd.
                        href={(id) => entityHref("driver", id)}
                      />
                    </p>
                    {order.assigned_run_id && (
                      <p className="text-xs text-gray-500">
                        Run: {order.assigned_run_id}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* Assigned Asset Card — navigable to the Fleet/tracking module
                (Req 2.4, 13.1); unresolved references render "Unlinked". */}
            {(order.assigned_asset_id ||
              order.links?.asset?.status === "resolved" ||
              order.links?.asset?.status === "unresolved") && (
              <div
                className="bg-white rounded-xl shadow-sm border border-gray-100 p-4"
                data-testid="assigned-asset-card"
              >
                <h3 className="text-sm font-semibold text-primary mb-3 flex items-center gap-2">
                  <Truck className="w-4 h-4" />
                  Assigned Asset
                </h3>
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-full bg-gray-100 flex items-center justify-center">
                    <Truck className="w-5 h-5 text-gray-500" />
                  </div>
                  <p className="text-sm font-medium">
                    <LinkedRefField
                      link={order.links?.asset}
                      fallbackId={order.assigned_asset_id}
                      href={(id) => entityHref("asset", id)}
                    />
                  </p>
                </div>
              </div>
            )}

            {/* Intake Metadata */}
            <IntakeMetadataSection order={order} />

            {/* Trace Info */}
            <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
              <h3 className="text-sm font-semibold text-primary mb-3">
                Trace Info
              </h3>
              <dl className="space-y-2 text-xs">
                <div className="flex justify-between">
                  <dt className="text-gray-500">Trace ID</dt>
                  <dd className="font-mono text-gray-700 truncate max-w-[180px]">
                    {order.trace_id}
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-gray-500">Schema Version</dt>
                  <dd className="text-gray-700">
                    {order.source_schema_version}
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-gray-500">Last Updated</dt>
                  <dd className="text-gray-700">
                    {formatDateTime(order.updated_at)}
                  </dd>
                </div>
              </dl>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
