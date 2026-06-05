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
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../../services/api";
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
  releaseHoldOrder,
  updateOrderStatus,
} from "../../../services/ordersApi";

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

// ─── Toast ───────────────────────────────────────────────────────────────────

interface Toast {
  id: number;
  message: string;
  type: "success" | "error";
}

let toastCounter = 0;

function ToastContainer({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed top-4 right-4 z-[100] space-y-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm font-medium ${
            t.type === "success"
              ? "bg-success text-white"
              : "bg-error text-white"
          }`}
        >
          <span>{t.message}</span>
          <button
            type="button"
            onClick={() => onDismiss(t.id)}
            className="ml-2 hover:bg-white/20 rounded p-0.5"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

// ─── Intake Metadata Renderer ────────────────────────────────────────────────

function IntakeMetadataSection({ order }: { order: FuelOrder }) {
  const meta = order.intake_metadata;
  const channel = order.intake_channel;

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
                  href={`/admin/imports/${meta.import_batch_id}`}
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

// ─── Mutation Controls ───────────────────────────────────────────────────────

interface MutationControlsProps {
  order: FuelOrder;
  onMutationSuccess: () => void;
  addToast: (message: string, type: "success" | "error") => void;
}

function MutationControls({
  order,
  onMutationSuccess,
  addToast,
}: MutationControlsProps) {
  const [showAssignModal, setShowAssignModal] = useState(false);
  const [showCancelModal, setShowCancelModal] = useState(false);
  const [showStatusModal, setShowStatusModal] = useState(false);
  const [showHoldModal, setShowHoldModal] = useState(false);
  const [working, setWorking] = useState(false);

  // Assign driver
  const [assignDriverId, setAssignDriverId] = useState("");
  const handleAssign = useCallback(async () => {
    if (!assignDriverId.trim()) return;
    setWorking(true);
    try {
      const payload: AssignDriverPayload = { driver_id: assignDriverId.trim() };
      await assignDriver(order.order_id, payload);
      addToast("Driver assigned successfully", "success");
      setShowAssignModal(false);
      setAssignDriverId("");
      onMutationSuccess();
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : "Failed to assign driver";
      addToast(msg, "error");
    } finally {
      setWorking(false);
    }
  }, [order.order_id, assignDriverId, addToast, onMutationSuccess]);

  // Cancel order (HIGH risk — modal)
  const [cancelReason, setCancelReason] = useState("");
  const handleCancel = useCallback(async () => {
    if (!cancelReason.trim()) return;
    setWorking(true);
    try {
      await cancelOrder(order.order_id, { reason: cancelReason.trim() });
      addToast("Order cancelled", "success");
      setShowCancelModal(false);
      setCancelReason("");
      onMutationSuccess();
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : "Failed to cancel order";
      addToast(msg, "error");
    } finally {
      setWorking(false);
    }
  }, [order.order_id, cancelReason, addToast, onMutationSuccess]);

  // Change status
  const [newStatus, setNewStatus] = useState<OrderStatus>("confirmed");
  const [statusReason, setStatusReason] = useState("");
  const handleStatusChange = useCallback(async () => {
    setWorking(true);
    try {
      await updateOrderStatus(order.order_id, {
        new_status: newStatus,
        reason: statusReason || undefined,
      });
      addToast(`Status changed to ${newStatus}`, "success");
      setShowStatusModal(false);
      setStatusReason("");
      onMutationSuccess();
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : "Failed to change status";
      addToast(msg, "error");
    } finally {
      setWorking(false);
    }
  }, [order.order_id, newStatus, statusReason, addToast, onMutationSuccess]);

  // Place on hold
  const [holdReason, setHoldReason] = useState("");
  const handleHold = useCallback(async () => {
    if (!holdReason.trim()) return;
    setWorking(true);
    try {
      await holdOrder(order.order_id, { hold_reason: holdReason.trim() });
      addToast("Order placed on hold", "success");
      setShowHoldModal(false);
      setHoldReason("");
      onMutationSuccess();
    } catch (err) {
      const msg =
        err instanceof ApiError ? err.message : "Failed to place order on hold";
      addToast(msg, "error");
    } finally {
      setWorking(false);
    }
  }, [order.order_id, holdReason, addToast, onMutationSuccess]);

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
              onClick={() => setShowStatusModal(true)}
              className="px-3 py-1.5 text-xs font-medium border border-gray-200 rounded-lg hover:bg-gray-50"
              aria-label="Change status"
            >
              Change Status
            </button>
            <button
              type="button"
              onClick={() => setShowAssignModal(true)}
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
                  onClick={() => setShowHoldModal(true)}
                  className="px-3 py-1.5 text-xs font-medium text-warning-dark border border-warning-light rounded-lg hover:bg-warning-light"
                  aria-label="Place on hold"
                >
                  Place on Hold
                </button>
              )
            )}
            <button
              type="button"
              onClick={() => setShowCancelModal(true)}
              className="px-3 py-1.5 text-xs font-medium text-error-dark border border-error-light rounded-lg hover:bg-error-light"
              aria-label="Cancel order"
            >
              Cancel Order
            </button>
          </>
        )}
      </div>

      {/* Assign Driver Modal */}
      {showAssignModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          role="dialog"
          aria-modal="true"
        >
          <div className="bg-white rounded-xl shadow-xl w-full max-w-sm mx-4 p-6">
            <h3 className="text-lg font-semibold mb-4">Assign Driver</h3>
            <input
              type="text"
              value={assignDriverId}
              onChange={(e) => setAssignDriverId(e.target.value)}
              placeholder="Driver ID"
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg mb-4 focus:ring-2 focus:ring-primary focus:outline-none"
              aria-label="Driver ID"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowAssignModal(false)}
                className="px-3 py-2 text-sm border border-gray-200 rounded-lg"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleAssign}
                disabled={working || !assignDriverId.trim()}
                className="px-3 py-2 text-sm font-medium text-white rounded-lg disabled:opacity-50 bg-primary hover:bg-primary-hover"
              >
                {working ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  "Assign"
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Cancel Order Modal (HIGH risk) */}
      {showCancelModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          role="dialog"
          aria-modal="true"
        >
          <div className="bg-white rounded-xl shadow-xl w-full max-w-sm mx-4 p-6">
            <div className="flex items-center gap-2 mb-4">
              <AlertTriangle className="w-5 h-5 text-error" />
              <h3 className="text-lg font-semibold text-error-dark">
                Cancel Order
              </h3>
            </div>
            <p className="text-sm text-gray-600 mb-4">
              This action cannot be undone. Please provide a reason for
              cancellation.
            </p>
            <textarea
              value={cancelReason}
              onChange={(e) => setCancelReason(e.target.value)}
              placeholder="Reason for cancellation"
              rows={3}
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg mb-4 focus:ring-2 focus:ring-primary focus:outline-none"
              aria-label="Cancellation reason"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowCancelModal(false)}
                className="px-3 py-2 text-sm border border-gray-200 rounded-lg"
              >
                Keep Order
              </button>
              <button
                type="button"
                onClick={handleCancel}
                disabled={working || !cancelReason.trim()}
                className="px-3 py-2 text-sm font-medium text-white bg-error rounded-lg disabled:opacity-50 hover:bg-error-dark"
              >
                {working ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  "Confirm Cancel"
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Place on Hold Modal */}
      {showHoldModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          role="dialog"
          aria-modal="true"
        >
          <div className="bg-white rounded-xl shadow-xl w-full max-w-sm mx-4 p-6">
            <div className="flex items-center gap-2 mb-4">
              <AlertTriangle className="w-5 h-5 text-warning-dark" />
              <h3 className="text-lg font-semibold text-warning-dark">
                Place on Hold
              </h3>
            </div>
            <p className="text-sm text-gray-600 mb-4">
              Holding pauses the order until it is released. Provide a reason
              (e.g. credit check, awaiting customer confirmation).
            </p>
            <textarea
              value={holdReason}
              onChange={(e) => setHoldReason(e.target.value)}
              placeholder="Reason for hold"
              rows={3}
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg mb-4 focus:ring-2 focus:ring-primary focus:outline-none"
              aria-label="Hold reason"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowHoldModal(false)}
                className="px-3 py-2 text-sm border border-gray-200 rounded-lg"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleHold}
                disabled={working || !holdReason.trim()}
                className="px-3 py-2 text-sm font-medium text-white bg-warning-dark rounded-lg disabled:opacity-50 hover:opacity-90"
              >
                {working ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  "Place on Hold"
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Change Status Modal */}
      {showStatusModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          role="dialog"
          aria-modal="true"
        >
          <div className="bg-white rounded-xl shadow-xl w-full max-w-sm mx-4 p-6">
            <h3 className="text-lg font-semibold mb-4">Change Status</h3>
            <select
              value={newStatus}
              onChange={(e) => setNewStatus(e.target.value as OrderStatus)}
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg mb-3 focus:ring-2 focus:ring-primary focus:outline-none"
              aria-label="New status"
            >
              <option value="confirmed">Confirmed</option>
              <option value="scheduled">Scheduled</option>
              <option value="dispatched">Dispatched</option>
              <option value="in_transit">In Transit</option>
              <option value="delivered">Delivered</option>
              <option value="failed">Failed</option>
              <option value="on_hold">On Hold</option>
            </select>
            <input
              type="text"
              value={statusReason}
              onChange={(e) => setStatusReason(e.target.value)}
              placeholder="Reason (optional)"
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg mb-4 focus:ring-2 focus:ring-primary focus:outline-none"
              aria-label="Status change reason"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowStatusModal(false)}
                className="px-3 py-2 text-sm border border-gray-200 rounded-lg"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleStatusChange}
                disabled={working}
                className="px-3 py-2 text-sm font-medium text-white rounded-lg disabled:opacity-50 bg-primary hover:bg-primary-hover"
              >
                {working ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  "Update Status"
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

// ─── Main Page Component ─────────────────────────────────────────────────────

export default function OrderDetailPage() {
  const params = useParams();
  const router = useRouter();
  const orderId = params?.orderId as string;

  const [order, setOrder] = useState<FuelOrder | null>(null);
  const [events, setEvents] = useState<FuelOrderEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: "success" | "error") => {
    const id = ++toastCounter;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(
      () => setToasts((prev) => prev.filter((t) => t.id !== id)),
      4000,
    );
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const fetchData = useCallback(async () => {
    if (!orderId) return;
    setLoading(true);
    setError(null);
    try {
      const [orderRes, eventsRes] = await Promise.all([
        getOrder(orderId),
        getOrderEvents(orderId),
      ]);
      setOrder(orderRes.data);
      setEvents(eventsRes.data);
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
        <Loader2 className="w-6 h-6 text-gray-400 animate-spin" />
      </div>
    );
  }

  if (error || !order) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <p className="text-error mb-4">{error ?? "Order not found"}</p>
          <button
            type="button"
            onClick={() => router.back()}
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
            onClick={() => router.back()}
            className="p-2 rounded-lg hover:bg-gray-100"
            aria-label="Go back"
          >
            <ArrowLeft className="w-5 h-5 text-gray-600" />
          </button>
          <div className="flex-1">
            <h1 className="text-xl font-semibold text-primary">
              Order {order.order_id.slice(0, 16)}…
            </h1>
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
                  {order.customer_name} ({order.customer_id})
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
            {/* Assigned Driver Card */}
            {order.assigned_driver_id && (
              <div
                className="bg-white rounded-xl shadow-sm border border-gray-100 p-4"
                data-testid="assigned-driver-card"
              >
                <h3 className="text-sm font-semibold text-primary mb-3 flex items-center gap-2">
                  <Truck className="w-4 h-4" />
                  Assigned Driver
                </h3>
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-full bg-gray-100 flex items-center justify-center">
                    <User className="w-5 h-5 text-gray-500" />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-primary">
                      {order.assigned_driver_id}
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
