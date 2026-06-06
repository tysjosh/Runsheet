"use client";

import { AlertTriangle, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  type AssetReadinessIndicator,
  getAssetReadiness,
  type ReadinessStatus,
} from "../../services/inventoryApi";
import { createJob } from "../../services/schedulingApi";
import type { Job, JobType, Priority } from "../../types/api";

const JOB_TYPES: { value: JobType; label: string }[] = [
  { value: "cargo_transport", label: "Cargo Transport" },
  { value: "passenger_transport", label: "Passenger Transport" },
  { value: "vessel_movement", label: "Vessel Movement" },
  { value: "airport_transfer", label: "Airport Transfer" },
  { value: "crane_booking", label: "Crane Booking" },
];

const PRIORITIES: { value: Priority; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "normal", label: "Normal" },
  { value: "high", label: "High" },
  { value: "urgent", label: "Urgent" },
];

// Asset options per job type (must match JOB_ASSET_COMPATIBILITY on backend)
const ASSETS_BY_JOB_TYPE: Record<string, { value: string; label: string }[]> = {
  cargo_transport: [
    { value: "TRK-001", label: "TRK-001 — Volvo FH16 Tanker" },
    { value: "TRK-002", label: "TRK-002 — Kenworth T680 Tanker" },
    { value: "TRK-003", label: "TRK-003 — Peterbilt 579 Tanker" },
    { value: "TRK-004", label: "TRK-004 — Freightliner Cascadia" },
    { value: "TRK-005", label: "TRK-005 — Mack Anthem Tanker" },
    { value: "TRK-006", label: "TRK-006 — International LT Tanker" },
    { value: "TRK-007", label: "TRK-007 — Ford F-750 Service" },
    { value: "TRK-008", label: "TRK-008 — RAM 5500 Service" },
    { value: "TNK-001", label: "TNK-001 — Heil 9200 Tanker Trailer" },
    { value: "TNK-002", label: "TNK-002 — Polar 8400 Tanker Trailer" },
  ],
  passenger_transport: [
    { value: "TRK-007", label: "TRK-007 — Ford F-750 Service" },
    { value: "TRK-008", label: "TRK-008 — RAM 5500 Service" },
  ],
  airport_transfer: [
    { value: "TRK-007", label: "TRK-007 — Ford F-750 Service" },
    { value: "TRK-008", label: "TRK-008 — RAM 5500 Service" },
  ],
  vessel_movement: [],
  crane_booking: [],
};

// ─── Readiness Indicator Component ──────────────────────────────────────────

interface ReadinessIndicatorProps {
  status: ReadinessStatus;
  missingParts: { name: string }[];
  lowParts: { name: string }[];
}

function ReadinessIndicator({
  status,
  missingParts,
  lowParts,
}: ReadinessIndicatorProps) {
  const colorMap: Record<ReadinessStatus, string> = {
    ready: "bg-success",
    warning: "bg-warning",
    critical: "bg-error",
    blocked: "bg-error",
  };

  const labelMap: Record<ReadinessStatus, string> = {
    ready: "All parts in stock",
    warning: "Some parts low stock",
    critical: "Critical parts out of stock",
    blocked: "Assignment blocked — parts unavailable",
  };

  const tooltipParts: string[] = [];
  if (missingParts.length > 0) {
    tooltipParts.push(`Missing: ${missingParts.map((p) => p.name).join(", ")}`);
  }
  if (lowParts.length > 0) {
    tooltipParts.push(`Low: ${lowParts.map((p) => p.name).join(", ")}`);
  }
  const tooltipText =
    tooltipParts.length > 0 ? tooltipParts.join(" | ") : labelMap[status];

  return (
    <span
      className="relative inline-flex items-center group"
      aria-label={`Asset readiness: ${labelMap[status]}`}
    >
      <span
        className={`inline-block w-2.5 h-2.5 rounded-full ${colorMap[status]}`}
        aria-hidden="true"
      />
      {/* Tooltip */}
      <span
        className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 px-2 py-1 text-[10px] text-white bg-gray-800 rounded whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50"
        role="tooltip"
      >
        {tooltipText}
      </span>
    </span>
  );
}

// ─── Toast Notification Component ───────────────────────────────────────────

// NOTE: This modal intentionally keeps its own divergent toast (warning/error
// variants, string ids, bottom-right placement, aria-live) and is excluded from
// the shared-toast consolidation in @/components/ui (see ui-scaffolding-
// consolidation Req 1.5 / 4.3). Do not migrate it onto the canonical toast.

interface ToastNotification {
  id: string;
  message: string;
  type: "warning" | "error";
}

function ToastContainer({
  toasts,
  onDismiss,
}: {
  toasts: ToastNotification[];
  onDismiss: (id: string) => void;
}) {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-[60] space-y-2" aria-live="polite">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`flex items-start gap-2 px-4 py-3 rounded-lg shadow-lg text-sm max-w-sm ${
            toast.type === "error"
              ? "bg-error-light text-error-dark border border-error-light"
              : "bg-warning-light text-warning-dark border border-warning-light"
          }`}
          role="alert"
        >
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <span className="flex-1">{toast.message}</span>
          <button
            onClick={() => onDismiss(toast.id)}
            className="text-gray-400 hover:text-gray-600"
            aria-label="Dismiss notification"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}

// ─── Main Component ─────────────────────────────────────────────────────────

interface CreateJobModalProps {
  onClose: () => void;
  onCreated: (job: Job) => void;
}

export default function CreateJobModal({
  onClose,
  onCreated,
}: CreateJobModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [toasts, setToasts] = useState<ToastNotification[]>([]);
  const [readinessMap, setReadinessMap] = useState<
    Record<string, AssetReadinessIndicator>
  >({});
  const [form, setForm] = useState({
    job_type: "cargo_transport" as JobType,
    origin: "",
    destination: "",
    scheduled_time: "",
    asset_assigned: "",
    priority: "normal" as Priority,
    notes: "",
  });

  // Fetch readiness for all assets of the current job type
  const fetchReadiness = useCallback(async (jobType: string) => {
    const assets = ASSETS_BY_JOB_TYPE[jobType] || [];
    if (assets.length === 0) return;

    const results: Record<string, AssetReadinessIndicator> = {};
    const settled = await Promise.allSettled(
      assets.map((asset) => getAssetReadiness(asset.value)),
    );

    settled.forEach((result, index) => {
      if (result.status === "fulfilled") {
        results[assets[index].value] = result.value.data;
      }
      // Fail-open: if request fails, don't show indicator for that asset
    });

    setReadinessMap(results);
  }, []);

  // Fetch readiness when job type changes
  useEffect(() => {
    fetchReadiness(form.job_type);
  }, [form.job_type, fetchReadiness]);

  const addToast = useCallback((message: string, type: "warning" | "error") => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setToasts((prev) => [...prev, { id, message, type }]);
    // Auto-dismiss after 8 seconds
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 8000);
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.origin || !form.destination || !form.scheduled_time) {
      setError("Origin, destination, and scheduled time are required.");
      return;
    }
    setError("");
    setSubmitting(true);
    try {
      const res = await createJob({
        job_type: form.job_type,
        origin: form.origin,
        destination: form.destination,
        scheduled_time: new Date(form.scheduled_time).toISOString(),
        asset_assigned: form.asset_assigned || undefined,
        priority: form.priority,
        notes: form.notes || undefined,
      });

      // Check for risk flags in the response (Requirement 7.2)
      const responseData = res.data as any;
      if (responseData?.readiness_flags) {
        const flags = responseData.readiness_flags;
        const parts: string[] = [];
        if (flags.missing_parts?.length) {
          parts.push(
            `Out of stock: ${flags.missing_parts.map((p: any) => p.name).join(", ")}`,
          );
        }
        if (flags.low_parts?.length) {
          parts.push(
            `Low stock: ${flags.low_parts.map((p: any) => p.name).join(", ")}`,
          );
        }
        if (parts.length > 0) {
          const location = flags.depot_location || flags.location || "";
          const locationStr = location ? ` at ${location}` : "";
          addToast(
            `Assignment risk${locationStr}: ${parts.join(" | ")}`,
            "warning",
          );
        }
      }

      onCreated(res.data);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create job");
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  const currentAssets = ASSETS_BY_JOB_TYPE[form.job_type] || [];

  return (
    <>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
        <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
          <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
            <h2 className="text-lg font-semibold text-primary">Create Job</h2>
            <button
              onClick={onClose}
              className="p-1 text-gray-400 hover:text-gray-600 rounded"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
            {error && (
              <p className="text-sm text-error bg-error-light px-3 py-2 rounded-lg">
                {error}
              </p>
            )}

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Job Type
                </label>
                <select
                  value={form.job_type}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      job_type: e.target.value as JobType,
                      asset_assigned: "",
                    })
                  }
                  className={inputClass}
                >
                  {JOB_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Priority
                </label>
                <select
                  value={form.priority}
                  onChange={(e) =>
                    setForm({ ...form, priority: e.target.value as Priority })
                  }
                  className={inputClass}
                >
                  {PRIORITIES.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Origin
              </label>
              <input
                type="text"
                value={form.origin}
                onChange={(e) => setForm({ ...form, origin: e.target.value })}
                placeholder="e.g. Houston Terminal"
                className={inputClass}
                required
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Destination
              </label>
              <input
                type="text"
                value={form.destination}
                onChange={(e) =>
                  setForm({ ...form, destination: e.target.value })
                }
                placeholder="e.g. Dallas Depot"
                className={inputClass}
                required
              />
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Scheduled Time
                </label>
                <input
                  type="datetime-local"
                  value={form.scheduled_time}
                  onChange={(e) =>
                    setForm({ ...form, scheduled_time: e.target.value })
                  }
                  className={inputClass}
                  required
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Asset (optional)
                </label>
                <div className="relative">
                  <select
                    value={form.asset_assigned}
                    onChange={(e) =>
                      setForm({ ...form, asset_assigned: e.target.value })
                    }
                    className={inputClass}
                  >
                    <option value="">— None —</option>
                    {currentAssets.map((a) => {
                      const readiness = readinessMap[a.value];
                      const statusIcon = readiness
                        ? readiness.status === "ready"
                          ? "🟢"
                          : readiness.status === "warning"
                            ? "🟡"
                            : "🔴"
                        : "";
                      return (
                        <option key={a.value} value={a.value}>
                          {statusIcon} {a.label}
                        </option>
                      );
                    })}
                  </select>
                </div>
                {/* Readiness indicator below the select */}
                {form.asset_assigned && readinessMap[form.asset_assigned] && (
                  <div className="mt-1.5 flex items-center gap-1.5">
                    <ReadinessIndicator
                      status={readinessMap[form.asset_assigned].status}
                      missingParts={
                        readinessMap[form.asset_assigned].missing_parts
                      }
                      lowParts={readinessMap[form.asset_assigned].low_parts}
                    />
                    <span className="text-[10px] text-gray-500">
                      {readinessMap[form.asset_assigned].status === "ready"
                        ? "Parts available"
                        : readinessMap[form.asset_assigned].status === "warning"
                          ? "Low stock warning"
                          : "Critical shortage"}
                    </span>
                  </div>
                )}
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Notes (optional)
              </label>
              <textarea
                value={form.notes}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
                placeholder="Any additional details..."
                rows={2}
                className={`${inputClass} resize-none`}
              />
            </div>

            <div className="flex justify-end gap-3 pt-2">
              <button
                type="button"
                onClick={onClose}
                className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={submitting}
                className="px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50 bg-primary hover:bg-primary-hover"
              >
                {submitting ? "Creating..." : "Create Job"}
              </button>
            </div>
          </form>
        </div>
      </div>

      {/* Toast notifications for risk flags */}
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </>
  );
}
