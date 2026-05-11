"use client";

/**
 * Truck compartment-state UI (Task 11.9).
 *
 * Surfaces the Capability 7 compartment lifecycle that the Fuel Ops
 * Hardening spec introduced so dispatchers can:
 *
 *  * Pick a truck (by ``truck_id``) and see every configured
 *    compartment with a state badge (``clean`` / ``loaded`` /
 *    ``needs_cleaning``) alongside the last loaded product,
 *    capacity, allowed grades, and last-cleaned timestamp.
 *    Backs onto ``GET /api/fuel/mvp/trucks/{truck_id}/compartments``.
 *  * Record a Cleaning_Event for any compartment via a modal form
 *    with method (flush/purge/sanitize), actor id, notes, and
 *    optional evidence photos. Evidence photos are uploaded through
 *    the presigned-upload contract (``POST /api/driver/pod/uploads/presign``
 *    → ``PUT <upload_url>``) before the cleaning event is POSTed so
 *    the backend only persists validated ``file_ref`` references.
 *    Posts to ``POST /api/fuel/mvp/compartments/{id}/cleaning-events``.
 *
 * Styling intentionally mirrors the existing ``components/ops/``
 * surfaces (Tailwind utility classes, inline status chips, ``bg-black/30``
 * modal overlays) so this page sits alongside
 * :file:`CustomerTankPage.tsx` and :file:`SourcingPage.tsx` without
 * visual drift.
 *
 * Validates: Requirement 7.1.4.
 */

import {
  AlertTriangle,
  Check,
  Droplets,
  Image as ImageIcon,
  Loader2,
  RefreshCw,
  Search,
  Sparkles,
  Truck as TruckIcon,
  Upload,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "../../services/api";
import {
  type PodUploadContentType,
  presignPodUpload,
  putPresignedFile,
} from "../../services/driverApi";
import type {
  CleaningEvent,
  CleaningMethod,
  CompartmentLifecycleState,
  LoadEligibilityDecision,
  LoadEligibilityResponse,
  TruckCompartmentState,
} from "../../services/fuelApi";
import {
  checkCompartmentLoadEligibility,
  getTruckCompartmentCapacityGallons,
  listTruckCompartments,
  recordCleaningEvent,
} from "../../services/fuelApi";

// ─── Constants ───────────────────────────────────────────────────────────────

const CLEANING_METHODS: { value: CleaningMethod; label: string }[] = [
  { value: "flush", label: "Flush" },
  { value: "purge", label: "Purge" },
  { value: "sanitize", label: "Sanitize" },
];

/** Mirrors the backend's ``_POD_UPLOAD_ALLOWED_MIME_TYPES``. */
const EVIDENCE_UPLOAD_TYPES: Record<string, PodUploadContentType> = {
  "image/jpeg": "image/jpeg",
  "image/png": "image/png",
  "image/heic": "image/heic",
  "application/pdf": "application/pdf",
};

const EVIDENCE_ACCEPT = Object.keys(EVIDENCE_UPLOAD_TYPES).join(",");

/**
 * Descriptor for the state badge rendered next to each compartment.
 *
 * Exported so the unit test can assert the colour / label combinations
 * without repeating the Tailwind class strings.
 */
export const STATE_BADGE_CONFIG: Record<
  CompartmentLifecycleState,
  { label: string; color: string; bg: string; title: string }
> = {
  clean: {
    label: "Clean",
    color: "text-green-700",
    bg: "bg-green-100",
    title: "Compartment is empty and safe to load any allowed grade.",
  },
  loaded: {
    label: "Loaded",
    color: "text-blue-700",
    bg: "bg-blue-100",
    title:
      "Compartment currently holds a product from the most recent loading plan.",
  },
  needs_cleaning: {
    label: "Needs Cleaning",
    color: "text-red-700",
    bg: "bg-red-100",
    title:
      "Cross-contamination rule triggered — record a Cleaning_Event before the next load.",
  },
};

// ─── Toasts ──────────────────────────────────────────────────────────────────

interface Toast {
  id: number;
  message: string;
  type: "success" | "error";
}

let toastIdCounter = 0;

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
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm font-medium text-white ${
            toast.type === "success" ? "bg-green-600" : "bg-red-600"
          }`}
        >
          {toast.type === "success" ? (
            <Check className="w-4 h-4" aria-hidden="true" />
          ) : (
            <AlertTriangle className="w-4 h-4" aria-hidden="true" />
          )}
          <span>{toast.message}</span>
          <button
            type="button"
            onClick={() => onDismiss(toast.id)}
            className="ml-2 p-0.5 hover:bg-white/20 rounded"
            aria-label="Dismiss notification"
          >
            <X className="w-3 h-3" aria-hidden="true" />
          </button>
        </div>
      ))}
    </div>
  );
}

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const addToast = useCallback((message: string, type: Toast["type"]) => {
    const id = ++toastIdCounter;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);
  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);
  return { toasts, addToast, dismissToast };
}

// ─── Formatters ──────────────────────────────────────────────────────────────

export function formatCapacity(
  compartment: TruckCompartmentState | null | undefined,
): string {
  if (!compartment) return "—";
  const gallons = getTruckCompartmentCapacityGallons(compartment);
  if (Number.isNaN(gallons)) return "—";
  return `${gallons.toFixed(0)} gal`;
}

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// ─── State Badge ─────────────────────────────────────────────────────────────

export function CompartmentStateBadge({
  state,
}: {
  state: CompartmentLifecycleState;
}) {
  const config = STATE_BADGE_CONFIG[state] ?? STATE_BADGE_CONFIG.clean;
  return (
    <span
      className={`inline-flex items-center text-xs px-2 py-0.5 rounded font-medium ${config.bg} ${config.color}`}
      title={config.title}
      data-testid={`compartment-state-badge-${state}`}
    >
      {config.label}
    </span>
  );
}

// ─── Cleaning Event Modal ────────────────────────────────────────────────────

/** Pure validator for the cleaning-event form; exported for unit tests. */
export interface CleaningFormValues {
  method: CleaningMethod;
  actor_id: string;
  notes: string;
}

export interface CleaningFormErrors {
  actor_id?: string;
  method?: string;
}

export function validateCleaningForm(
  values: CleaningFormValues,
): CleaningFormErrors {
  const errors: CleaningFormErrors = {};
  if (!values.actor_id || !values.actor_id.trim()) {
    errors.actor_id = "Actor ID is required.";
  }
  if (!CLEANING_METHODS.some((m) => m.value === values.method)) {
    errors.method = "Method must be flush, purge, or sanitize.";
  }
  return errors;
}

interface EvidenceItem {
  /** Local client id so we can list and remove before upload. */
  id: number;
  file: File;
  status: "queued" | "uploading" | "uploaded" | "error";
  /** Server-side ``file_ref`` after a successful PUT. */
  file_ref?: string;
  error?: string;
}

let evidenceIdCounter = 0;

interface CleaningEventModalProps {
  compartment: TruckCompartmentState;
  onClose: () => void;
  onSuccess: (event: CleaningEvent) => void;
}

function CleaningEventModal({
  compartment,
  onClose,
  onSuccess,
}: CleaningEventModalProps) {
  const [form, setForm] = useState<CleaningFormValues>({
    method: "flush",
    actor_id: "",
    notes: "",
  });
  const [fieldErrors, setFieldErrors] = useState<CleaningFormErrors>({});
  const [apiError, setApiError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [evidence, setEvidence] = useState<EvidenceItem[]>([]);

  const uploadedRefs = useMemo(
    () =>
      evidence
        .filter((e) => e.status === "uploaded" && e.file_ref)
        .map((e) => e.file_ref as string),
    [evidence],
  );

  const anyUploading = evidence.some((e) => e.status === "uploading");
  const anyPending = evidence.some(
    (e) => e.status === "queued" || e.status === "error",
  );

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";
  const errorInputClass =
    "w-full px-3 py-2 text-sm border border-red-300 rounded-lg focus:ring-2 focus:ring-red-200 focus:border-red-400 bg-white";

  function updateField<K extends keyof CleaningFormValues>(
    key: K,
    value: CleaningFormValues[K],
  ) {
    setForm((prev) => ({ ...prev, [key]: value }));
    if (key in fieldErrors) {
      setFieldErrors((prev) => ({ ...prev, [key]: undefined }));
    }
  }

  async function uploadSingle(item: EvidenceItem) {
    const contentType: PodUploadContentType | undefined =
      EVIDENCE_UPLOAD_TYPES[item.file.type];
    if (!contentType) {
      setEvidence((prev) =>
        prev.map((e) =>
          e.id === item.id
            ? { ...e, status: "error", error: "Unsupported file type" }
            : e,
        ),
      );
      return;
    }

    setEvidence((prev) =>
      prev.map((e) =>
        e.id === item.id ? { ...e, status: "uploading", error: undefined } : e,
      ),
    );

    try {
      const presigned = await presignPodUpload("photo", contentType);
      if (item.file.size > presigned.max_file_bytes) {
        throw new Error(
          `File exceeds tenant limit of ${Math.round(presigned.max_file_bytes / 1_000_000)} MB.`,
        );
      }
      await putPresignedFile(presigned.upload_url, item.file, contentType);
      setEvidence((prev) =>
        prev.map((e) =>
          e.id === item.id
            ? { ...e, status: "uploaded", file_ref: presigned.file_ref }
            : e,
        ),
      );
    } catch (err) {
      setEvidence((prev) =>
        prev.map((e) =>
          e.id === item.id
            ? {
                ...e,
                status: "error",
                error: err instanceof Error ? err.message : "Upload failed",
              }
            : e,
        ),
      );
    }
  }

  function handleFileSelect(files: FileList | null) {
    if (!files) return;
    const nextItems: EvidenceItem[] = [];
    for (const file of Array.from(files)) {
      nextItems.push({
        id: ++evidenceIdCounter,
        file,
        status: "queued",
      });
    }
    setEvidence((prev) => [...prev, ...nextItems]);
    // Kick off uploads sequentially so we don't saturate the network.
    void (async () => {
      for (const item of nextItems) {
        // eslint-disable-next-line no-await-in-loop
        await uploadSingle(item);
      }
    })();
  }

  function removeEvidence(id: number) {
    setEvidence((prev) => prev.filter((e) => e.id !== id));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const errors = validateCleaningForm(form);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    if (anyUploading) {
      setApiError("Wait for evidence uploads to finish before submitting.");
      return;
    }
    if (anyPending) {
      setApiError("Retry or remove queued evidence uploads before submitting.");
      return;
    }

    setApiError("");
    setSubmitting(true);
    try {
      const event = await recordCleaningEvent(compartment.compartment_id, {
        method: form.method,
        actor_id: form.actor_id.trim(),
        notes: form.notes.trim() ? form.notes.trim() : undefined,
        evidence_refs: uploadedRefs,
      });
      onSuccess(event);
      onClose();
    } catch (err) {
      if (err instanceof ApiError) {
        setApiError(err.message || `Request failed (HTTP ${err.status}).`);
      } else {
        setApiError(
          err instanceof Error
            ? err.message
            : "Failed to record cleaning event.",
        );
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      role="dialog"
      aria-modal="true"
      aria-labelledby="cleaning-event-modal-title"
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-xl mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <div>
            <h2
              id="cleaning-event-modal-title"
              className="text-lg font-semibold text-[#232323]"
            >
              Record cleaning event
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {compartment.truck_id} · Compartment {compartment.compartment_id}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close cleaning event form"
          >
            <X className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>

        <form
          onSubmit={handleSubmit}
          className="px-6 py-4 space-y-4"
          data-testid="cleaning-event-form"
        >
          {apiError && (
            <p
              role="alert"
              className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg"
            >
              {apiError}
            </p>
          )}

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="ce-method"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Method
              </label>
              <select
                id="ce-method"
                className={inputClass}
                value={form.method}
                onChange={(e) =>
                  updateField("method", e.target.value as CleaningMethod)
                }
              >
                {CLEANING_METHODS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label
                htmlFor="ce-actor"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Actor ID
              </label>
              <input
                id="ce-actor"
                type="text"
                className={fieldErrors.actor_id ? errorInputClass : inputClass}
                value={form.actor_id}
                onChange={(e) => updateField("actor_id", e.target.value)}
                placeholder="e.g. driver-042"
                required
              />
              {fieldErrors.actor_id && (
                <p className="text-xs text-red-600 mt-1">
                  {fieldErrors.actor_id}
                </p>
              )}
            </div>
          </div>

          <div>
            <label
              htmlFor="ce-notes"
              className="block text-xs font-medium text-gray-600 mb-1"
            >
              Notes (optional)
            </label>
            <textarea
              id="ce-notes"
              rows={3}
              className={inputClass}
              value={form.notes}
              onChange={(e) => updateField("notes", e.target.value)}
              placeholder="Anything the next driver should know."
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Evidence photos (optional)
            </label>
            <label
              htmlFor="ce-evidence"
              className="flex flex-col items-center justify-center gap-2 px-4 py-6 border-2 border-dashed border-gray-200 rounded-lg cursor-pointer hover:bg-gray-50 text-sm text-gray-600"
            >
              <Upload className="w-5 h-5" aria-hidden="true" />
              <span>Select photos or PDFs</span>
              <span className="text-[10px] text-gray-400">
                JPEG · PNG · HEIC · PDF
              </span>
              <input
                id="ce-evidence"
                type="file"
                className="sr-only"
                accept={EVIDENCE_ACCEPT}
                multiple
                data-testid="cleaning-event-evidence-input"
                onChange={(e) => {
                  handleFileSelect(e.target.files);
                  e.target.value = "";
                }}
              />
            </label>

            {evidence.length > 0 && (
              <ul className="mt-2 space-y-1.5">
                {evidence.map((item) => (
                  <li
                    key={item.id}
                    className="flex items-center gap-2 px-3 py-2 rounded-md border border-gray-100 text-xs"
                  >
                    <ImageIcon
                      className="w-3.5 h-3.5 text-gray-400"
                      aria-hidden="true"
                    />
                    <span className="flex-1 truncate text-gray-700">
                      {item.file.name}
                    </span>
                    {item.status === "uploading" && (
                      <span className="inline-flex items-center gap-1 text-blue-700">
                        <Loader2
                          className="w-3 h-3 animate-spin"
                          aria-hidden="true"
                        />
                        Uploading
                      </span>
                    )}
                    {item.status === "uploaded" && (
                      <span className="inline-flex items-center gap-1 text-green-700">
                        <Check className="w-3 h-3" aria-hidden="true" />
                        Uploaded
                      </span>
                    )}
                    {item.status === "error" && (
                      <span className="inline-flex items-center gap-1 text-red-700">
                        <AlertTriangle className="w-3 h-3" aria-hidden="true" />
                        {item.error || "Failed"}
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={() => removeEvidence(item.id)}
                      className="p-0.5 text-gray-400 hover:text-gray-600"
                      aria-label={`Remove ${item.file.name}`}
                    >
                      <X className="w-3 h-3" aria-hidden="true" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-gray-100">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 border border-gray-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || anyUploading}
              className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white rounded-lg bg-[#232323] hover:bg-[#1a1a1a] disabled:opacity-50"
            >
              {submitting ? (
                <>
                  <Loader2
                    className="w-3.5 h-3.5 animate-spin"
                    aria-hidden="true"
                  />
                  Saving...
                </>
              ) : (
                <>
                  <Sparkles className="w-3.5 h-3.5" aria-hidden="true" />
                  Record cleaning
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Load Eligibility Modal ──────────────────────────────────────────────────

/**
 * Badge styling for each possible load-eligibility decision. Exported
 * so unit tests can assert the colour / label combinations without
 * repeating the Tailwind class strings.
 */
export const ELIGIBILITY_DECISION_CONFIG: Record<
  LoadEligibilityDecision,
  { label: string; color: string; bg: string }
> = {
  allowed: {
    label: "Allowed",
    color: "text-green-700",
    bg: "bg-green-100",
  },
  blocked: {
    label: "Blocked",
    color: "text-red-700",
    bg: "bg-red-100",
  },
  requires_cleaning: {
    label: "Requires cleaning",
    color: "text-amber-700",
    bg: "bg-amber-100",
  },
};

interface LoadEligibilityModalProps {
  compartment: TruckCompartmentState;
  onClose: () => void;
}

function LoadEligibilityModal({
  compartment,
  onClose,
}: LoadEligibilityModalProps) {
  const [productCode, setProductCode] = useState("");
  const [result, setResult] = useState<LoadEligibilityResponse | null>(null);
  const [apiError, setApiError] = useState("");
  const [checking, setChecking] = useState(false);

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white uppercase";

  async function handleCheck(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = productCode.trim().toUpperCase();
    if (!trimmed) {
      setApiError("Product code is required.");
      return;
    }
    setApiError("");
    setChecking(true);
    try {
      const resp = await checkCompartmentLoadEligibility(
        compartment.compartment_id,
        trimmed,
      );
      setResult(resp);
    } catch (err) {
      setResult(null);
      if (err instanceof ApiError) {
        setApiError(err.message || `Request failed (HTTP ${err.status}).`);
      } else {
        setApiError(
          err instanceof Error ? err.message : "Failed to check eligibility.",
        );
      }
    } finally {
      setChecking(false);
    }
  }

  const decisionConfig = result
    ? (ELIGIBILITY_DECISION_CONFIG[result.decision] ??
      ELIGIBILITY_DECISION_CONFIG.allowed)
    : null;
  const governingConfig = result
    ? (ELIGIBILITY_DECISION_CONFIG[result.governing_rule] ??
      ELIGIBILITY_DECISION_CONFIG.allowed)
    : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      role="dialog"
      aria-modal="true"
      aria-labelledby="load-eligibility-modal-title"
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-xl mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <div>
            <h2
              id="load-eligibility-modal-title"
              className="text-lg font-semibold text-[#232323]"
            >
              Check load eligibility
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Compartment {compartment.compartment_id} · Last loaded{" "}
              <span className="font-medium text-[#232323]">
                {compartment.last_loaded_product ?? "none"}
              </span>
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close load eligibility form"
          >
            <X className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>

        <form
          onSubmit={handleCheck}
          className="px-6 py-4 space-y-4"
          data-testid="load-eligibility-form"
        >
          {apiError && (
            <p
              role="alert"
              className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg"
            >
              {apiError}
            </p>
          )}

          <div>
            <label
              htmlFor="le-product-code"
              className="block text-xs font-medium text-gray-600 mb-1"
            >
              Proposed product code
            </label>
            <input
              id="le-product-code"
              type="text"
              className={inputClass}
              value={productCode}
              onChange={(e) => setProductCode(e.target.value.toUpperCase())}
              placeholder="e.g. DIESEL_2"
              required
            />
            <p className="text-[10px] text-gray-400 mt-1">
              Enter the canonical product code used by the compatibility matrix.
            </p>
          </div>

          <div className="flex items-center justify-end gap-2 pt-2">
            <button
              type="submit"
              disabled={checking}
              className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white rounded-lg bg-[#232323] hover:bg-[#1a1a1a] disabled:opacity-50"
            >
              {checking ? (
                <>
                  <Loader2
                    className="w-3.5 h-3.5 animate-spin"
                    aria-hidden="true"
                  />
                  Checking...
                </>
              ) : (
                <>
                  <Search className="w-3.5 h-3.5" aria-hidden="true" />
                  Check
                </>
              )}
            </button>
          </div>
        </form>

        {result && decisionConfig && governingConfig && (
          <div className="px-6 pb-6" data-testid="load-eligibility-result">
            <div className="border border-gray-200 rounded-lg p-4 space-y-3">
              <div className="flex items-center gap-2">
                <span className="text-[10px] uppercase tracking-wide text-gray-500">
                  Decision
                </span>
                <span
                  className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded font-medium ${decisionConfig.bg} ${decisionConfig.color}`}
                  data-testid={`load-eligibility-decision-${result.decision}`}
                >
                  {result.decision === "allowed" ? (
                    <Check className="w-3 h-3" aria-hidden="true" />
                  ) : (
                    <AlertTriangle className="w-3 h-3" aria-hidden="true" />
                  )}
                  {decisionConfig.label}
                </span>
              </div>

              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
                <div>
                  <dt className="text-[10px] uppercase tracking-wide text-gray-500">
                    Proposed product
                  </dt>
                  <dd className="font-mono text-gray-900">
                    {result.proposed_product}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-wide text-gray-500">
                    Previous product
                  </dt>
                  <dd className="font-mono text-gray-900">
                    {result.previous_product ?? "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-wide text-gray-500">
                    Governing rule
                  </dt>
                  <dd>
                    <span
                      className={`inline-flex items-center text-[11px] px-1.5 py-0.5 rounded font-medium ${governingConfig.bg} ${governingConfig.color}`}
                      data-testid={`load-eligibility-governing-${result.governing_rule}`}
                    >
                      {governingConfig.label}
                    </span>
                  </dd>
                </div>
                {result.reason && (
                  <div className="sm:col-span-2">
                    <dt className="text-[10px] uppercase tracking-wide text-gray-500">
                      Reason
                    </dt>
                    <dd
                      className="text-gray-700"
                      data-testid="load-eligibility-reason"
                    >
                      {result.reason}
                    </dd>
                  </div>
                )}
              </dl>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Page ────────────────────────────────────────────────────────────────────

export default function TruckCompartmentsPage() {
  const [truckIdInput, setTruckIdInput] = useState("");
  const [activeTruckId, setActiveTruckId] = useState<string | null>(null);
  const [items, setItems] = useState<TruckCompartmentState[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [modalCompartment, setModalCompartment] =
    useState<TruckCompartmentState | null>(null);
  const [eligibilityCompartment, setEligibilityCompartment] =
    useState<TruckCompartmentState | null>(null);
  const { toasts, addToast, dismissToast } = useToasts();

  const fetchCompartments = useCallback(async (truckId: string) => {
    setLoading(true);
    setError("");
    try {
      const resp = await listTruckCompartments(truckId);
      setItems(resp.items);
      setActiveTruckId(resp.truck_id);
    } catch (err) {
      setItems([]);
      setError(
        err instanceof Error ? err.message : "Failed to load compartments.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  const handleLookup = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = truckIdInput.trim();
    if (!trimmed) {
      setError("Truck ID is required.");
      return;
    }
    void fetchCompartments(trimmed);
  };

  const handleRefresh = () => {
    if (activeTruckId) void fetchCompartments(activeTruckId);
  };

  const handleCleaningSuccess = (event: CleaningEvent) => {
    addToast(
      `Cleaning event recorded (${event.method}) for ${event.compartment_id}.`,
      "success",
    );
    if (activeTruckId) void fetchCompartments(activeTruckId);
  };

  // Keep page scroll locked when modal is open.
  useEffect(() => {
    if (!modalCompartment && !eligibilityCompartment) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [modalCompartment, eligibilityCompartment]);

  return (
    <div className="flex-1 flex flex-col p-6 bg-gray-50 overflow-auto">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
      <div className="bg-white rounded-xl border border-gray-200 p-6 max-w-6xl w-full mx-auto">
        <div className="flex items-start justify-between gap-4 mb-6">
          <div>
            <h1 className="text-2xl font-semibold text-[#232323] mb-1 flex items-center gap-2">
              <TruckIcon className="w-5 h-5" aria-hidden="true" />
              Truck compartments
            </h1>
            <p className="text-sm text-gray-500">
              Review compartment state and record a cleaning event when the
              cross-contamination guard flags a compartment as{" "}
              <span className="font-medium text-red-700">needs_cleaning</span>.
            </p>
          </div>
          {activeTruckId && (
            <button
              type="button"
              onClick={handleRefresh}
              disabled={loading}
              className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 border border-gray-200 disabled:opacity-50"
              aria-label="Refresh compartments"
            >
              <RefreshCw
                className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
                aria-hidden="true"
              />
              Refresh
            </button>
          )}
        </div>

        <form
          className="flex items-end gap-3 mb-6"
          onSubmit={handleLookup}
          data-testid="truck-lookup-form"
        >
          <div className="flex-1">
            <label
              htmlFor="truck-id-input"
              className="block text-xs font-medium text-gray-600 mb-1"
            >
              Truck ID
            </label>
            <div className="relative">
              <Search
                className="w-3.5 h-3.5 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2"
                aria-hidden="true"
              />
              <input
                id="truck-id-input"
                type="text"
                className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
                value={truckIdInput}
                onChange={(e) => setTruckIdInput(e.target.value)}
                placeholder="e.g. TRUCK-042"
                required
              />
            </div>
          </div>
          <button
            type="submit"
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white rounded-lg bg-[#232323] hover:bg-[#1a1a1a] disabled:opacity-50"
          >
            {loading ? (
              <Loader2
                className="w-3.5 h-3.5 animate-spin"
                aria-hidden="true"
              />
            ) : (
              <Search className="w-3.5 h-3.5" aria-hidden="true" />
            )}
            Load compartments
          </button>
        </form>

        {error && (
          <p
            role="alert"
            className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg mb-4"
          >
            {error}
          </p>
        )}

        {!activeTruckId && !error && (
          <div className="text-center py-16 text-gray-500 text-sm">
            Enter a truck ID above to see its compartments.
          </div>
        )}

        {activeTruckId && !loading && items.length === 0 && (
          <div className="text-center py-16 border border-dashed border-gray-200 rounded-lg">
            <Droplets
              className="w-10 h-10 mx-auto text-gray-300 mb-2"
              aria-hidden="true"
            />
            <p className="text-sm font-medium text-gray-700">
              No compartments configured for{" "}
              <span className="font-mono">{activeTruckId}</span>
            </p>
            <p className="text-xs text-gray-500 mt-1">
              Configure compartments via{" "}
              <code className="font-mono text-xs">
                PUT /api/fuel/mvp/compartments/&lbrace;truck_id&rbrace;
              </code>
              .
            </p>
          </div>
        )}

        {items.length > 0 && (
          <div className="overflow-x-auto border border-gray-100 rounded-lg">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Position</th>
                  <th className="px-3 py-2 font-medium">Compartment</th>
                  <th className="px-3 py-2 font-medium">State</th>
                  <th className="px-3 py-2 font-medium">Capacity</th>
                  <th className="px-3 py-2 font-medium">Last loaded</th>
                  <th className="px-3 py-2 font-medium">Last cleaned</th>
                  <th className="px-3 py-2 font-medium">Allowed grades</th>
                  <th className="px-3 py-2 font-medium text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {items.map((c) => (
                  <tr
                    key={c.compartment_id}
                    data-testid={`compartment-row-${c.compartment_id}`}
                  >
                    <td className="px-3 py-2 text-gray-600 font-mono text-xs">
                      {c.position_index}
                    </td>
                    <td className="px-3 py-2 text-[#232323] font-medium font-mono text-xs">
                      {c.compartment_id}
                    </td>
                    <td className="px-3 py-2">
                      <CompartmentStateBadge state={c.state} />
                    </td>
                    <td className="px-3 py-2 text-gray-600 text-xs">
                      {formatCapacity(c)}
                    </td>
                    <td className="px-3 py-2 text-gray-600 text-xs">
                      {c.last_loaded_product ? (
                        <span>
                          <span className="font-medium text-[#232323]">
                            {c.last_loaded_product}
                          </span>
                          <span className="block text-[10px] text-gray-400">
                            {formatTimestamp(c.last_loaded_at)}
                          </span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-600 text-xs">
                      {formatTimestamp(c.last_cleaned_at)}
                    </td>
                    <td className="px-3 py-2 text-gray-600 text-xs">
                      {c.allowed_grades.length > 0
                        ? c.allowed_grades.join(", ")
                        : "—"}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <div className="inline-flex items-center gap-1.5 justify-end">
                        <button
                          type="button"
                          onClick={() => setEligibilityCompartment(c)}
                          className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-[#232323] rounded-md bg-white border border-gray-200 hover:bg-gray-50"
                          data-testid={`check-eligibility-${c.compartment_id}`}
                        >
                          <Search className="w-3 h-3" aria-hidden="true" />
                          Check eligibility
                        </button>
                        <button
                          type="button"
                          onClick={() => setModalCompartment(c)}
                          className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-white rounded-md bg-[#232323] hover:bg-[#1a1a1a]"
                          data-testid={`record-cleaning-${c.compartment_id}`}
                        >
                          <Sparkles className="w-3 h-3" aria-hidden="true" />
                          Record cleaning
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {modalCompartment && (
        <CleaningEventModal
          compartment={modalCompartment}
          onClose={() => setModalCompartment(null)}
          onSuccess={handleCleaningSuccess}
        />
      )}
      {eligibilityCompartment && (
        <LoadEligibilityModal
          compartment={eligibilityCompartment}
          onClose={() => setEligibilityCompartment(null)}
        />
      )}
    </div>
  );
}
