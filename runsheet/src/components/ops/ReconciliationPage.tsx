"use client";

/**
 * Reconciliation dashboard page (Task 11.5).
 *
 * Surfaces the four-way gallon-variance records introduced by the Fuel
 * Ops Hardening spec (Capability 4 — POD + Reconciliation):
 *
 *   ordered_gallons (Order) → loaded_gallons (Loading_Plan) →
 *     delivered_gallons (POD/OCR) → invoiced_gallons (QBO integration)
 *
 * Features:
 *
 *  • Paginated 4-way variance table wired to
 *    `GET /api/fuel/mvp/reconciliation`, with order_id / plan_id /
 *    pod_id / min_variance_pct filters.
 *  • Alert highlighting for rows whose ``alert_flags`` contains
 *    ``variance_exceeds_threshold`` (Req 4.4.3) and per-cell variance
 *    coloring keyed to the 3% default threshold.
 *  • POD detail drawer with a BOL download link that hits
 *    ``GET /api/fuel/pod/{pod_id}/bol`` and follows the returned
 *    presigned URL (Req 4.3.4, 4.3.5). Rows whose BOL is still in
 *    ``pending_regeneration`` surface the state inline instead of a
 *    dead link.
 *
 * Styling mirrors other `components/ops/` pages (Tailwind utility
 * classes, inline status chips, `bg-black/30` modal overlays) so this
 * page sits alongside `FuelDistributionPage` and `CustomerTankPage`.
 *
 * Validates: Requirements 4.3.4, 4.4.4.
 */

import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Copy,
  Download,
  FileText,
  Loader2,
  RefreshCw,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  BOLDownloadResponse,
  HashChainMismatch,
  HashChainVerifyResponse,
  HashProofResponse,
  ReconciliationListFilters,
  ReconciliationRecord,
} from "../../services/fuelApi";
import {
  getPodBol,
  getPodHashProof,
  listReconciliationRecords,
  verifyPodHashChain,
} from "../../services/fuelApi";

// ─── Constants ───────────────────────────────────────────────────────────────

const PAGE_SIZE = 25;

/** Default tenant-level alert threshold surfaced by the backend. Used
 *  for per-cell variance coloring when a row does not already carry
 *  ``alert_flags`` — the backend only emits ``variance_exceeds_threshold``
 *  once, so this drives the visual heat-map independent of the flag. */
const DEFAULT_ALERT_PCT = 3.0;

/** Alert flag emitted by :class:`ReconciliationService` when any
 *  variance crosses the tenant-configured threshold (Req 4.4.3). */
const VARIANCE_ALERT_FLAG = "variance_exceeds_threshold";

// ─── Toast System (mirrors CustomerTankPage.tsx) ─────────────────────────────

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
          className={`flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm font-medium ${
            toast.type === "success"
              ? "bg-green-600 text-white"
              : "bg-red-600 text-white"
          }`}
        >
          {toast.type === "success" ? (
            <Check className="w-4 h-4" />
          ) : (
            <AlertTriangle className="w-4 h-4" />
          )}
          <span>{toast.message}</span>
          <button
            type="button"
            onClick={() => onDismiss(toast.id)}
            className="ml-2 p-0.5 hover:bg-white/20 rounded"
            aria-label="Dismiss notification"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      ))}
    </div>
  );
}

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: "success" | "error") => {
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

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Format a gallon quantity for table display. Nulls (e.g. invoiced
 * gallons before the QBO webhook fires) render as an em-dash.
 */
export function formatGallons(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  if (value >= 10_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

/**
 * Format a variance percentage for display. Nulls (missing invoice
 * leg) render as an em-dash so the operator can distinguish "pending"
 * from "zero".
 */
export function formatVariancePct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${value.toFixed(2)}%`;
}

/**
 * Map a variance percentage + threshold to a tailwind cell class.
 * Null variances render as a neutral cell so operators can tell them
 * apart from a true zero.
 */
export function varianceCellClass(
  pct: number | null | undefined,
  threshold: number = DEFAULT_ALERT_PCT,
): string {
  if (pct == null || Number.isNaN(pct)) return "text-gray-400";
  const abs = Math.abs(pct);
  if (abs >= threshold) return "text-red-700 font-semibold";
  if (abs >= threshold * 0.5) return "text-yellow-700 font-medium";
  return "text-gray-700";
}

/**
 * Pure derivation for the row-level highlight decision. A row is
 * highlighted when the backend surfaces the ``variance_exceeds_threshold``
 * flag or when any present variance crosses the UI threshold. Keeping
 * this as a pure helper makes the page-level alert count stable and
 * testable without DOM assertions.
 */
export function isAlertedRow(
  record: ReconciliationRecord,
  threshold: number = DEFAULT_ALERT_PCT,
): boolean {
  if (record.alert_flags?.includes(VARIANCE_ALERT_FLAG)) {
    return true;
  }
  const variances: (number | null | undefined)[] = [
    record.variance_load_vs_order_pct,
    record.variance_delivered_vs_loaded_pct,
    record.variance_invoiced_vs_delivered_pct,
  ];
  return variances.some(
    (v) => v != null && !Number.isNaN(v) && Math.abs(v) >= threshold,
  );
}

function formatTimestamp(iso: string | null | undefined): string {
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

// ─── Filters Row ─────────────────────────────────────────────────────────────

interface ReconciliationFiltersState {
  order_id?: string;
  plan_id?: string;
  pod_id?: string;
  min_variance_pct?: number;
}

interface FiltersRowProps {
  filters: ReconciliationFiltersState;
  onChange: (next: ReconciliationFiltersState) => void;
  onReset: () => void;
  onRefresh: () => void;
  loading?: boolean;
}

function FiltersRow({
  filters,
  onChange,
  onReset,
  onRefresh,
  loading,
}: FiltersRowProps) {
  const inputClass =
    "pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white w-40";
  const numberClass =
    "px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white w-32";

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div>
        <label
          htmlFor="rec-filter-order"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          Order ID
        </label>
        <div className="relative">
          <Search
            className="w-3.5 h-3.5 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2"
            aria-hidden="true"
          />
          <input
            id="rec-filter-order"
            type="text"
            placeholder="e.g. ORD-0042"
            className={inputClass}
            value={filters.order_id ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                order_id: e.target.value.trim() || undefined,
              })
            }
          />
        </div>
      </div>

      <div>
        <label
          htmlFor="rec-filter-plan"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          Plan ID
        </label>
        <div className="relative">
          <Search
            className="w-3.5 h-3.5 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2"
            aria-hidden="true"
          />
          <input
            id="rec-filter-plan"
            type="text"
            placeholder="e.g. plan-0042"
            className={inputClass}
            value={filters.plan_id ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                plan_id: e.target.value.trim() || undefined,
              })
            }
          />
        </div>
      </div>

      <div>
        <label
          htmlFor="rec-filter-pod"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          POD ID
        </label>
        <div className="relative">
          <Search
            className="w-3.5 h-3.5 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2"
            aria-hidden="true"
          />
          <input
            id="rec-filter-pod"
            type="text"
            placeholder="e.g. pod-0042"
            className={inputClass}
            value={filters.pod_id ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                pod_id: e.target.value.trim() || undefined,
              })
            }
          />
        </div>
      </div>

      <div>
        <label
          htmlFor="rec-filter-variance"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          Min Variance %
        </label>
        <input
          id="rec-filter-variance"
          type="number"
          min="0"
          step="0.1"
          placeholder={`≥ ${DEFAULT_ALERT_PCT}`}
          className={numberClass}
          value={filters.min_variance_pct ?? ""}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              onChange({ ...filters, min_variance_pct: undefined });
              return;
            }
            const parsed = Number(raw);
            onChange({
              ...filters,
              min_variance_pct:
                Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined,
            });
          }}
        />
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onReset}
          className="px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 border border-gray-200"
        >
          Reset
        </button>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 border border-gray-200 disabled:opacity-50"
          aria-label="Refresh reconciliation records"
        >
          <RefreshCw
            className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
            aria-hidden="true"
          />
          Refresh
        </button>
      </div>
    </div>
  );
}

// ─── POD Detail Drawer ───────────────────────────────────────────────────────

interface PodDetailDrawerProps {
  record: ReconciliationRecord;
  onClose: () => void;
  onError: (message: string) => void;
}

/**
 * Side drawer that renders the full reconciliation row and the BOL
 * download link. Fetches the BOL row on open; surfaces the three
 * states the backend exposes:
 *
 *  1. ``generated`` + ``download_url`` — render a download link that
 *     opens the presigned S3 URL in a new tab.
 *  2. ``pending_regeneration`` — render a non-actionable state chip
 *     so the dispatcher knows the PDF is queued for regeneration.
 *  3. 404 / transport error — render an error banner and keep the
 *     rest of the drawer functional.
 */
function PodDetailDrawer({ record, onClose, onError }: PodDetailDrawerProps) {
  const [bol, setBol] = useState<BOLDownloadResponse | null>(null);
  const [loadingBol, setLoadingBol] = useState(false);
  const [bolError, setBolError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoadingBol(true);
    setBolError(null);
    getPodBol(record.pod_id)
      .then((res) => {
        if (!cancelled) setBol(res);
      })
      .catch((err) => {
        if (cancelled) return;
        const message =
          err instanceof Error ? err.message : "Failed to load BOL.";
        setBolError(message);
        // Only surface transport/auth failures as toasts — a missing
        // BOL (HTTP 404) is an expected state for a POD whose
        // ``overlay.bol_generation`` flag was off at finalization.
        if (!/bol_not_found/i.test(message)) onError(message);
      })
      .finally(() => {
        if (!cancelled) setLoadingBol(false);
      });
    return () => {
      cancelled = true;
    };
  }, [record.pod_id, onError]);

  const alerted = isAlertedRow(record);
  const bolPending = bol?.status === "pending_regeneration";
  const bolReady = bol?.status === "generated" && !!bol.download_url;

  return (
    <div
      className="fixed inset-0 z-50 flex items-stretch justify-end bg-black/30"
      onClick={onClose}
    >
      <div
        className="bg-white w-full max-w-xl h-full overflow-y-auto shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100 sticky top-0 bg-white">
          <div>
            <h2 className="text-lg font-semibold text-[#232323]">
              POD {record.pod_id}
            </h2>
            <p className="text-xs text-gray-500">
              Reconciliation {record.reconciliation_id}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close POD detail"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-6 py-4 space-y-4">
          {alerted && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-800">
              <AlertTriangle
                className="w-4 h-4 mt-0.5 flex-shrink-0"
                aria-hidden="true"
              />
              <div>
                <div className="font-medium">Variance threshold exceeded</div>
                <div className="text-xs text-red-700 mt-0.5">
                  At least one variance exceeded the tenant alert threshold.
                  Review the gallon legs below.
                </div>
              </div>
            </div>
          )}

          <section>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
              Identifiers
            </h3>
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-xs text-gray-500">Order</dt>
                <dd className="text-gray-900 font-mono break-all">
                  {record.order_id}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-gray-500">Loading plan</dt>
                <dd className="text-gray-900 font-mono break-all">
                  {record.plan_id}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-gray-500">POD</dt>
                <dd className="text-gray-900 font-mono break-all">
                  {record.pod_id}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-gray-500">Invoice</dt>
                <dd className="text-gray-900 font-mono break-all">
                  {record.invoice_id ?? "—"}
                </dd>
              </div>
            </dl>
          </section>

          <section>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
              Gallons (4-way)
            </h3>
            <div className="grid grid-cols-4 gap-2 text-sm">
              <GallonCard label="Ordered" value={record.ordered_gallons} />
              <GallonCard label="Loaded" value={record.loaded_gallons} />
              <GallonCard label="Delivered" value={record.delivered_gallons} />
              <GallonCard label="Invoiced" value={record.invoiced_gallons} />
            </div>
          </section>

          <section>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
              Variances
            </h3>
            <ul className="space-y-1.5 text-sm">
              <VarianceRow
                label="Load vs Order"
                value={record.variance_load_vs_order_pct}
              />
              <VarianceRow
                label="Delivered vs Loaded"
                value={record.variance_delivered_vs_loaded_pct}
              />
              <VarianceRow
                label="Invoiced vs Delivered"
                value={record.variance_invoiced_vs_delivered_pct}
              />
            </ul>
          </section>

          {record.alert_flags && record.alert_flags.length > 0 && (
            <section>
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                Alert flags
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {record.alert_flags.map((flag) => (
                  <span
                    key={flag}
                    className="inline-flex items-center text-[10px] px-2 py-0.5 rounded font-medium bg-red-100 text-red-700"
                  >
                    {flag}
                  </span>
                ))}
              </div>
            </section>
          )}

          <section>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
              Bill of Lading
            </h3>
            {loadingBol ? (
              <div className="inline-flex items-center gap-2 text-sm text-gray-500">
                <Loader2 className="w-4 h-4 animate-spin" aria-hidden="true" />
                Loading BOL…
              </div>
            ) : bolError ? (
              <div className="text-sm text-gray-700 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
                {/bol_not_found/i.test(bolError)
                  ? "No BOL has been generated for this POD."
                  : bolError}
              </div>
            ) : bolPending ? (
              <div className="inline-flex items-center gap-2 text-sm text-yellow-800 bg-yellow-50 border border-yellow-200 rounded-lg px-3 py-2">
                <AlertTriangle className="w-4 h-4" aria-hidden="true" />
                BOL is queued for regeneration.
              </div>
            ) : bolReady && bol ? (
              <a
                href={bol.download_url ?? "#"}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-3 py-2 text-sm font-medium text-white bg-[#232323] hover:bg-black rounded-lg"
                aria-label={`Download BOL PDF for POD ${record.pod_id}`}
              >
                <Download className="w-4 h-4" aria-hidden="true" />
                Download BOL PDF
              </a>
            ) : (
              <div className="text-sm text-gray-500">
                BOL is not yet available.
              </div>
            )}
            {bol?.generated_at && (
              <p className="text-xs text-gray-500 mt-1.5">
                Generated {formatTimestamp(bol.generated_at)}
              </p>
            )}
          </section>

          <TamperEvidenceSection podId={record.pod_id} onError={onError} />

          <section>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
              Metadata
            </h3>
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-xs text-gray-500">Generated</dt>
                <dd className="text-gray-900">
                  {formatTimestamp(record.generated_at)}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-gray-500">Tenant</dt>
                <dd className="text-gray-900 font-mono break-all">
                  {record.tenant_id}
                </dd>
              </div>
            </dl>
          </section>
        </div>
      </div>
    </div>
  );
}

// ─── Tamper Evidence Section (Req 4.5.3, 4.5.4) ──────────────────────────────

interface TamperEvidenceSectionProps {
  podId: string;
  onError: (message: string) => void;
}

/**
 * Collapsible "Tamper evidence" panel inside the POD drawer. Exposes
 * two auditor-facing controls:
 *
 *  1. **Show hash proof** — calls ``GET /api/fuel/pod/{pod_id}/hash-proof``
 *     and renders the canonical payload + chain pointers so auditors
 *     can re-hash offline (Req 4.5.3).
 *  2. **Verify chain** — calls ``POST /api/fuel/pod/hash-chain/verify``
 *     over a range starting at the current POD and reports the first
 *     mismatch when the chain is broken (Req 4.5.4, 4.5.5).
 *
 * Transport / auth failures bubble up via the existing toast helper
 * (``onError``); a missing hash proof renders inline instead of
 * dismissing the section.
 */
function TamperEvidenceSection({ podId, onError }: TamperEvidenceSectionProps) {
  const [open, setOpen] = useState(false);
  const [showProof, setShowProof] = useState(false);
  const [proof, setProof] = useState<HashProofResponse | null>(null);
  const [proofLoading, setProofLoading] = useState(false);
  const [proofError, setProofError] = useState<string | null>(null);
  const [copyLabel, setCopyLabel] = useState<"Copy" | "Copied">("Copy");

  const [showVerify, setShowVerify] = useState(false);
  const [fromPodId, setFromPodId] = useState(podId);
  const [toPodId, setToPodId] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [verifyResult, setVerifyResult] =
    useState<HashChainVerifyResponse | null>(null);

  const payloadJson = useMemo(() => {
    if (!proof) return "";
    try {
      return JSON.stringify(proof.canonical_payload, null, 2);
    } catch {
      return "";
    }
  }, [proof]);

  const handleShowProof = useCallback(async () => {
    setShowProof(true);
    setProofLoading(true);
    setProofError(null);
    try {
      const res = await getPodHashProof(podId);
      setProof(res);
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to load hash proof.";
      setProofError(message);
      onError(message);
    } finally {
      setProofLoading(false);
    }
  }, [podId, onError]);

  const handleCopyPayload = useCallback(async () => {
    if (!payloadJson) return;
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(payloadJson);
      }
      setCopyLabel("Copied");
      setTimeout(() => setCopyLabel("Copy"), 1500);
    } catch {
      onError("Copy to clipboard failed.");
    }
  }, [payloadJson, onError]);

  const handleVerifyChain = useCallback(async () => {
    if (!fromPodId.trim()) {
      onError("from_pod_id is required.");
      return;
    }
    setVerifying(true);
    setVerifyResult(null);
    try {
      const body: { from_pod_id: string; to_pod_id?: string } = {
        from_pod_id: fromPodId.trim(),
      };
      const trimmedTo = toPodId.trim();
      if (trimmedTo) body.to_pod_id = trimmedTo;
      const res = await verifyPodHashChain(body);
      setVerifyResult(res);
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to verify hash chain.";
      onError(message);
    } finally {
      setVerifying(false);
    }
  }, [fromPodId, toPodId, onError]);

  return (
    <section>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center justify-between w-full text-left group"
        aria-expanded={open}
        aria-controls={`tamper-evidence-${podId}`}
      >
        <span className="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase tracking-wide">
          <ShieldCheck className="w-3.5 h-3.5" aria-hidden="true" />
          Tamper evidence
        </span>
        {open ? (
          <ChevronUp
            className="w-4 h-4 text-gray-400 group-hover:text-gray-600"
            aria-hidden="true"
          />
        ) : (
          <ChevronDown
            className="w-4 h-4 text-gray-400 group-hover:text-gray-600"
            aria-hidden="true"
          />
        )}
      </button>

      {open && (
        <div
          id={`tamper-evidence-${podId}`}
          className="mt-3 space-y-3 border border-gray-200 rounded-lg p-3 bg-gray-50"
        >
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={handleShowProof}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-200 rounded-lg hover:bg-gray-100"
            >
              Show hash proof
            </button>
            <button
              type="button"
              onClick={() => setShowVerify((v) => !v)}
              aria-expanded={showVerify}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-200 rounded-lg hover:bg-gray-100"
            >
              Verify chain
            </button>
          </div>

          {showProof && (
            <div className="rounded-lg border border-gray-200 bg-white p-3 space-y-2">
              {proofLoading ? (
                <div className="inline-flex items-center gap-2 text-sm text-gray-500">
                  <Loader2
                    className="w-4 h-4 animate-spin"
                    aria-hidden="true"
                  />
                  Loading hash proof…
                </div>
              ) : proofError ? (
                <div className="text-sm text-gray-700 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
                  {proofError}
                </div>
              ) : proof ? (
                <>
                  <dl className="grid grid-cols-1 gap-2 text-xs">
                    <div>
                      <dt className="text-gray-500">pod_hash</dt>
                      <dd
                        className="font-mono break-all text-gray-900"
                        data-testid="tamper-pod-hash"
                      >
                        {proof.pod_hash}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-gray-500">previous_pod_hash</dt>
                      <dd
                        className="font-mono break-all text-gray-900"
                        data-testid="tamper-previous-pod-hash"
                      >
                        {proof.previous_pod_hash}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-gray-500">chain_sequence</dt>
                      <dd className="font-mono text-gray-900">
                        {typeof proof.canonical_payload?.chain_sequence ===
                          "number" ||
                        typeof proof.canonical_payload?.chain_sequence ===
                          "string"
                          ? String(proof.canonical_payload.chain_sequence)
                          : "—"}
                      </dd>
                    </div>
                  </dl>
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs text-gray-500">
                        canonical_payload
                      </span>
                      <button
                        type="button"
                        onClick={handleCopyPayload}
                        className="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium text-gray-600 bg-white border border-gray-200 rounded hover:bg-gray-50"
                        aria-label="Copy canonical payload"
                      >
                        <Copy className="w-3 h-3" aria-hidden="true" />
                        {copyLabel}
                      </button>
                    </div>
                    <pre className="text-[11px] font-mono text-gray-800 bg-gray-50 border border-gray-200 rounded p-2 overflow-auto max-h-[240px]">
                      {payloadJson}
                    </pre>
                  </div>
                </>
              ) : null}
            </div>
          )}

          {showVerify && (
            <div className="rounded-lg border border-gray-200 bg-white p-3 space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label
                    htmlFor={`verify-from-${podId}`}
                    className="block text-[11px] font-medium text-gray-600 mb-1"
                  >
                    from_pod_id
                  </label>
                  <input
                    id={`verify-from-${podId}`}
                    type="text"
                    value={fromPodId}
                    onChange={(e) => setFromPodId(e.target.value)}
                    className="w-full px-2.5 py-1.5 text-xs font-mono border border-gray-200 rounded focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
                  />
                </div>
                <div>
                  <label
                    htmlFor={`verify-to-${podId}`}
                    className="block text-[11px] font-medium text-gray-600 mb-1"
                  >
                    to_pod_id (optional)
                  </label>
                  <input
                    id={`verify-to-${podId}`}
                    type="text"
                    value={toPodId}
                    onChange={(e) => setToPodId(e.target.value)}
                    placeholder="Leave blank to verify single POD"
                    className="w-full px-2.5 py-1.5 text-xs font-mono border border-gray-200 rounded focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
                  />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleVerifyChain}
                  disabled={verifying}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-[#232323] hover:bg-black rounded-lg disabled:opacity-50"
                >
                  {verifying ? (
                    <Loader2
                      className="w-3.5 h-3.5 animate-spin"
                      aria-hidden="true"
                    />
                  ) : null}
                  Verify
                </button>
              </div>

              {verifyResult?.valid && (
                <div
                  data-testid="chain-intact-badge"
                  className="inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-green-800 bg-green-50 border border-green-200 rounded-lg"
                >
                  <Check className="w-3.5 h-3.5" aria-hidden="true" />
                  Chain intact
                  <span className="text-green-700">
                    · {verifyResult.verified_count} POD
                    {verifyResult.verified_count === 1 ? "" : "s"} verified
                  </span>
                </div>
              )}

              {verifyResult &&
                !verifyResult.valid &&
                verifyResult.first_mismatch && (
                  <ChainMismatchCard mismatch={verifyResult.first_mismatch} />
                )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function ChainMismatchCard({ mismatch }: { mismatch: HashChainMismatch }) {
  return (
    <div
      data-testid="chain-mismatch-card"
      className="rounded-lg border border-red-200 bg-red-50 p-3 space-y-2"
    >
      <div className="flex items-center gap-2 text-sm font-semibold text-red-800">
        <AlertTriangle className="w-4 h-4" aria-hidden="true" />
        Tamper detected
      </div>
      <p className="text-xs text-red-700">
        Hash chain verification failed at the POD below. Rehash the canonical
        payload offline to confirm.
      </p>
      <dl className="grid grid-cols-1 gap-1.5 text-xs">
        <div>
          <dt className="text-red-700">pod_id</dt>
          <dd
            className="font-mono break-all text-red-900"
            data-testid="mismatch-pod-id"
          >
            {mismatch.pod_id}
          </dd>
        </div>
        <div>
          <dt className="text-red-700">expected_hash</dt>
          <dd
            className="font-mono break-all text-red-900"
            data-testid="mismatch-expected-hash"
          >
            {mismatch.expected_hash ?? "—"}
          </dd>
        </div>
        <div>
          <dt className="text-red-700">actual_hash</dt>
          <dd
            className="font-mono break-all text-red-900"
            data-testid="mismatch-actual-hash"
          >
            {mismatch.stored_hash ?? mismatch.computed_hash ?? "—"}
          </dd>
        </div>
        <div>
          <dt className="text-red-700">reason</dt>
          <dd className="text-red-900">{mismatch.reason}</dd>
        </div>
      </dl>
    </div>
  );
}

function GallonCard({
  label,
  value,
}: {
  label: string;
  value: number | null | undefined;
}) {
  return (
    <div className="border border-gray-200 rounded-lg px-3 py-2 bg-gray-50">
      <div className="text-[10px] uppercase tracking-wide text-gray-500">
        {label}
      </div>
      <div className="text-base font-semibold text-gray-900">
        {formatGallons(value)}
      </div>
    </div>
  );
}
function VarianceRow({
  label,
  value,
}: {
  label: string;
  value: number | null | undefined;
}) {
  return (
    <li className="flex items-center justify-between">
      <span className="text-xs text-gray-500">{label}</span>
      <span className={`font-mono ${varianceCellClass(value)}`}>
        {formatVariancePct(value)}
      </span>
    </li>
  );
}

// ─── Table ───────────────────────────────────────────────────────────────────

interface ReconciliationTableProps {
  records: ReconciliationRecord[];
  onSelect: (record: ReconciliationRecord) => void;
}

function ReconciliationTable({ records, onSelect }: ReconciliationTableProps) {
  if (records.length === 0) {
    return (
      <div className="border border-dashed border-gray-200 rounded-lg px-6 py-12 text-center text-sm text-gray-500">
        No reconciliation records match the current filters.
      </div>
    );
  }

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-gray-50">
          <tr className="text-left text-xs uppercase tracking-wide text-gray-500">
            <th className="px-3 py-2 font-medium">POD</th>
            <th className="px-3 py-2 font-medium">Order</th>
            <th className="px-3 py-2 font-medium text-right">Ordered</th>
            <th className="px-3 py-2 font-medium text-right">Loaded</th>
            <th className="px-3 py-2 font-medium text-right">Delivered</th>
            <th className="px-3 py-2 font-medium text-right">Invoiced</th>
            <th className="px-3 py-2 font-medium text-right">L/O %</th>
            <th className="px-3 py-2 font-medium text-right">D/L %</th>
            <th className="px-3 py-2 font-medium text-right">I/D %</th>
            <th className="px-3 py-2 font-medium">Generated</th>
            <th className="px-3 py-2 font-medium" aria-label="Actions" />
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {records.map((record) => {
            const alerted = isAlertedRow(record);
            return (
              <tr
                key={record.reconciliation_id}
                data-testid={`reconciliation-row-${record.reconciliation_id}`}
                className={alerted ? "bg-red-50" : "bg-white"}
              >
                <td className="px-3 py-2 font-mono text-xs text-gray-700 break-all">
                  {record.pod_id}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-gray-700 break-all">
                  {record.order_id}
                </td>
                <td className="px-3 py-2 text-right text-gray-700">
                  {formatGallons(record.ordered_gallons)}
                </td>
                <td className="px-3 py-2 text-right text-gray-700">
                  {formatGallons(record.loaded_gallons)}
                </td>
                <td className="px-3 py-2 text-right text-gray-700">
                  {formatGallons(record.delivered_gallons)}
                </td>
                <td className="px-3 py-2 text-right text-gray-700">
                  {formatGallons(record.invoiced_gallons)}
                </td>
                <td
                  className={`px-3 py-2 text-right font-mono ${varianceCellClass(record.variance_load_vs_order_pct)}`}
                >
                  {formatVariancePct(record.variance_load_vs_order_pct)}
                </td>
                <td
                  className={`px-3 py-2 text-right font-mono ${varianceCellClass(record.variance_delivered_vs_loaded_pct)}`}
                >
                  {formatVariancePct(record.variance_delivered_vs_loaded_pct)}
                </td>
                <td
                  className={`px-3 py-2 text-right font-mono ${varianceCellClass(record.variance_invoiced_vs_delivered_pct)}`}
                >
                  {formatVariancePct(record.variance_invoiced_vs_delivered_pct)}
                </td>
                <td className="px-3 py-2 text-xs text-gray-500 whitespace-nowrap">
                  {formatTimestamp(record.generated_at)}
                </td>
                <td className="px-3 py-2">
                  <button
                    type="button"
                    onClick={() => onSelect(record)}
                    className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-gray-700 hover:text-gray-900 border border-gray-200 rounded-md hover:bg-gray-50"
                    aria-label={`Open POD ${record.pod_id}`}
                  >
                    <FileText className="w-3 h-3" aria-hidden="true" />
                    POD
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ─── Summary Bar ─────────────────────────────────────────────────────────────

interface SummaryBarProps {
  total: number;
  pageCount: number;
  alertedCount: number;
}

function SummaryBar({ total, pageCount, alertedCount }: SummaryBarProps) {
  return (
    <div className="grid grid-cols-3 gap-3">
      <div className="border border-gray-200 rounded-lg px-4 py-3 bg-white">
        <div className="text-xs text-gray-500 uppercase tracking-wide">
          Total records
        </div>
        <div className="text-xl font-semibold text-gray-900">{total}</div>
      </div>
      <div className="border border-gray-200 rounded-lg px-4 py-3 bg-white">
        <div className="text-xs text-gray-500 uppercase tracking-wide">
          On this page
        </div>
        <div className="text-xl font-semibold text-gray-900">{pageCount}</div>
      </div>
      <div
        className={`border rounded-lg px-4 py-3 ${alertedCount > 0 ? "border-red-200 bg-red-50" : "border-gray-200 bg-white"}`}
      >
        <div className="text-xs uppercase tracking-wide text-gray-500">
          Alerted on this page
        </div>
        <div
          className={`text-xl font-semibold ${alertedCount > 0 ? "text-red-700" : "text-gray-900"}`}
        >
          {alertedCount}
        </div>
      </div>
    </div>
  );
}

// ─── Page Component ──────────────────────────────────────────────────────────

export interface ReconciliationPageProps {
  /** Initial filters — useful when linking in from a specific plan / POD. */
  initialFilters?: ReconciliationFiltersState;
}

export default function ReconciliationPage({
  initialFilters,
}: ReconciliationPageProps = {}) {
  const { toasts, addToast, dismissToast } = useToasts();

  const [filters, setFilters] = useState<ReconciliationFiltersState>(
    initialFilters ?? {},
  );
  const [page, setPage] = useState(1);
  const [records, setRecords] = useState<ReconciliationRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ReconciliationRecord | null>(null);

  const queryFilters: ReconciliationListFilters = useMemo(
    () => ({
      order_id: filters.order_id,
      plan_id: filters.plan_id,
      pod_id: filters.pod_id,
      min_variance_pct: filters.min_variance_pct,
      page,
      size: PAGE_SIZE,
    }),
    [filters, page],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await listReconciliationRecords(queryFilters);
      setRecords(res.items);
      setTotal(res.total);
      setHasNext(res.has_next);
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to load reconciliation records.";
      setError(message);
      addToast(message, "error");
    } finally {
      setLoading(false);
    }
  }, [queryFilters, addToast]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleFiltersChange = useCallback(
    (next: ReconciliationFiltersState) => {
      setFilters(next);
      setPage(1);
    },
    [],
  );

  const handleResetFilters = useCallback(() => {
    setFilters({});
    setPage(1);
  }, []);

  const alertedCount = useMemo(
    () => records.filter((r) => isAlertedRow(r)).length,
    [records],
  );

  return (
    <div className="flex flex-col h-full bg-gray-50">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />

      <div className="border-b border-gray-200 bg-white px-6 py-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold text-[#232323]">
              Reconciliation
            </h1>
            <p className="text-sm text-gray-500 mt-0.5">
              Four-way variance tracking: ordered → loaded → delivered →
              invoiced gallons.
            </p>
          </div>
        </div>
        <div className="mt-4">
          <FiltersRow
            filters={filters}
            onChange={handleFiltersChange}
            onReset={handleResetFilters}
            onRefresh={refresh}
            loading={loading}
          />
        </div>
      </div>

      <div className="flex-1 overflow-auto px-6 py-4 space-y-4">
        <SummaryBar
          total={total}
          pageCount={records.length}
          alertedCount={alertedCount}
        />

        {error && !loading && (
          <div className="flex items-start gap-2 p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-800">
            <AlertTriangle
              className="w-4 h-4 mt-0.5 flex-shrink-0"
              aria-hidden="true"
            />
            <div>
              <div className="font-medium">Could not load reconciliation.</div>
              <div className="text-xs mt-0.5">{error}</div>
            </div>
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-12 text-sm text-gray-500">
            <Loader2 className="w-5 h-5 animate-spin mr-2" aria-hidden="true" />
            Loading reconciliation records…
          </div>
        ) : (
          <ReconciliationTable records={records} onSelect={setSelected} />
        )}

        <div className="flex items-center justify-between pt-2">
          <div className="text-xs text-gray-500">
            Page {page}
            {total > 0 && (
              <span>
                {" "}
                · Showing {records.length} of {total} records
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={loading || page <= 1}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-gray-700 hover:text-gray-900 border border-gray-200 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
              aria-label="Previous page"
            >
              <ChevronLeft className="w-4 h-4" aria-hidden="true" />
              Prev
            </button>
            <button
              type="button"
              onClick={() => setPage((p) => p + 1)}
              disabled={loading || !hasNext}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-gray-700 hover:text-gray-900 border border-gray-200 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
              aria-label="Next page"
            >
              Next
              <ChevronRight className="w-4 h-4" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>

      {selected && (
        <PodDetailDrawer
          record={selected}
          onClose={() => setSelected(null)}
          onError={(msg) => addToast(msg, "error")}
        />
      )}
    </div>
  );
}
