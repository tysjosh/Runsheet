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
  ChevronUp,
  Copy,
  Download,
  FileText,
  Loader2,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type Column,
  DataTable,
  Drawer,
  EntityLink,
  ExportCsvButton,
  Field,
  FilterPopover,
  IconButton,
  INPUT_CLASS,
  NumberField,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { dateTime, number } from "../../lib/format";
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
import { PageTitle } from "../ui/PageHeader";
import { notify } from "../ui/toast/notify";

const SEARCH_CLASS =
  "h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus";

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

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Gallons: "12.3K" from 10,000 up, else at most one decimal ("987.7"). */
export function formatGallons(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value >= 10_000) return `${number(value / 1_000, { decimals: 1 })}K`;
  const tenths = Math.round(value * 10) / 10;
  return number(tenths, { decimals: Number.isInteger(tenths) ? 0 : 1 });
}

/** Variance percentage with two decimals: "3.20%". */
export function formatVariancePct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${number(value, { decimals: 2 })}%`;
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
  if (pct == null || Number.isNaN(pct)) return "text-gray-500";
  const abs = Math.abs(pct);
  if (abs >= threshold) return "text-error-dark font-semibold";
  if (abs >= threshold * 0.5) return "text-warning-dark font-medium";
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
  return dateTime(iso);
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
  const popoverCount =
    (filters.plan_id ? 1 : 0) +
    (filters.pod_id ? 1 : 0) +
    (filters.min_variance_pct != null ? 1 : 0);
  return (
    <Toolbar
      label="Reconciliation"
      search={
        <input
          id="rec-filter-order"
          type="search"
          aria-label="Order ID"
          placeholder="Order ID, e.g. ORD-0042"
          className={SEARCH_CLASS}
          value={filters.order_id ?? ""}
          onChange={(e) =>
            onChange({
              ...filters,
              order_id: e.target.value.trim() || undefined,
            })
          }
        />
      }
      filters={
        <FilterPopover
          count={popoverCount}
          label="Reconciliation filters"
          onClear={onReset}
        >
          <div className="grid w-80 grid-cols-2 gap-3">
            <Field label="Plan ID" id="rec-filter-plan">
              <input
                id="rec-filter-plan"
                type="text"
                placeholder="e.g. plan-0042"
                className={INPUT_CLASS}
                value={filters.plan_id ?? ""}
                onChange={(e) =>
                  onChange({
                    ...filters,
                    plan_id: e.target.value.trim() || undefined,
                  })
                }
              />
            </Field>
            <Field label="POD ID" id="rec-filter-pod">
              <input
                id="rec-filter-pod"
                type="text"
                placeholder="e.g. pod-0042"
                className={INPUT_CLASS}
                value={filters.pod_id ?? ""}
                onChange={(e) =>
                  onChange({
                    ...filters,
                    pod_id: e.target.value.trim() || undefined,
                  })
                }
              />
            </Field>
            <Field
              label="Min variance %"
              id="rec-filter-variance"
              help={`Alerts start at ${number(DEFAULT_ALERT_PCT)}%.`}
            >
              <NumberField
                id="rec-filter-variance"
                value={filters.min_variance_pct ?? null}
                onChange={(n) =>
                  onChange({
                    ...filters,
                    min_variance_pct: n != null && n >= 0 ? n : undefined,
                  })
                }
                unit="%"
                decimals={1}
                min={0}
              />
            </Field>
          </div>
        </FilterPopover>
      }
      end={
        <IconButton
          label="Refresh reconciliation records"
          size="sm"
          onClick={onRefresh}
          disabled={loading}
          icon={
            <RefreshCw
              className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
            />
          }
        />
      }
    />
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
    <Drawer open onClose={onClose} title={`POD ${record.pod_id}`} width={576}>
      <div>
        <p className="px-6 pt-3 text-xs text-gray-500">
          Reconciliation {record.reconciliation_id}
        </p>

        <div className="px-6 py-4 space-y-4">
          {alerted && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-error-light border border-error-light text-sm text-error-dark">
              <AlertTriangle
                className="w-4 h-4 mt-0.5 flex-shrink-0"
                aria-hidden="true"
              />
              <div>
                <div className="font-medium">Variance threshold exceeded</div>
                <div className="text-xs text-error-dark mt-0.5">
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
                <dd className="text-gray-900 break-all">
                  <EntityLink
                    type="order"
                    id={record.order_id}
                    showId={false}
                  />
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
                <dd className="text-gray-900 break-all">
                  {record.invoice_id ? (
                    <EntityLink
                      type="invoice"
                      id={record.invoice_id}
                      showId={false}
                    />
                  ) : (
                    "—"
                  )}
                </dd>
              </div>
            </dl>
          </section>

          <section>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
              Responsible (from order)
            </h3>
            <dl className="grid grid-cols-3 gap-3 text-sm">
              <div>
                <dt className="text-xs text-gray-500">Customer</dt>
                <dd className="text-gray-900 break-all">
                  <EntityLink
                    type="customer"
                    id={record.customer_id}
                    showId={false}
                  />
                </dd>
              </div>
              <div>
                <dt className="text-xs text-gray-500">Asset</dt>
                <dd className="text-gray-900 break-all">
                  <EntityLink
                    type="asset"
                    id={record.assigned_asset_id}
                    showId={false}
                  />
                </dd>
              </div>
              <div>
                <dt className="text-xs text-gray-500">Driver</dt>
                <dd className="text-gray-900 break-all">
                  <EntityLink
                    type="driver"
                    id={record.assigned_driver_id}
                    showId={false}
                  />
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
                    className="inline-flex items-center text-[10px] px-2 py-0.5 rounded font-medium bg-error-light text-error-dark"
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
              <div className="inline-flex items-center gap-2 text-sm text-warning-dark bg-warning-light border border-warning-light rounded-lg px-3 py-2">
                <AlertTriangle className="w-4 h-4" aria-hidden="true" />
                BOL is queued for regeneration.
              </div>
            ) : bolReady && bol ? (
              <a
                href={bol.download_url ?? "#"}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-3 py-2 text-sm font-medium text-white bg-primary hover:bg-primary-hover rounded-lg"
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
    </Drawer>
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
            className="w-4 h-4 text-gray-500 group-hover:text-gray-600"
            aria-hidden="true"
          />
        ) : (
          <ChevronDown
            className="w-4 h-4 text-gray-500 group-hover:text-gray-600"
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
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-primary hover:bg-primary-hover rounded-lg disabled:opacity-50"
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
                  className="inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-success-dark bg-success-light border border-success-light rounded-lg"
                >
                  <Check className="w-3.5 h-3.5" aria-hidden="true" />
                  Chain intact
                  <span className="text-success-dark">
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
      className="rounded-lg border border-error-light bg-error-light p-3 space-y-2"
    >
      <div className="flex items-center gap-2 text-sm font-semibold text-error-dark">
        <AlertTriangle className="w-4 h-4" aria-hidden="true" />
        Tamper detected
      </div>
      <p className="text-xs text-error-dark">
        Hash chain verification failed at the POD below. Rehash the canonical
        payload offline to confirm.
      </p>
      <dl className="grid grid-cols-1 gap-1.5 text-xs">
        <div>
          <dt className="text-error-dark">pod_id</dt>
          <dd
            className="font-mono break-all text-error-dark"
            data-testid="mismatch-pod-id"
          >
            {mismatch.pod_id}
          </dd>
        </div>
        <div>
          <dt className="text-error-dark">expected_hash</dt>
          <dd
            className="font-mono break-all text-error-dark"
            data-testid="mismatch-expected-hash"
          >
            {mismatch.expected_hash ?? "—"}
          </dd>
        </div>
        <div>
          <dt className="text-error-dark">actual_hash</dt>
          <dd
            className="font-mono break-all text-error-dark"
            data-testid="mismatch-actual-hash"
          >
            {mismatch.stored_hash ?? mismatch.computed_hash ?? "—"}
          </dd>
        </div>
        <div>
          <dt className="text-error-dark">reason</dt>
          <dd className="text-error-dark">{mismatch.reason}</dd>
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

// ─── Reconciliation chain (Req 12.2, 13.1) ───────────────────────────────────

/**
 * Render the `order_id → plan_id → pod_id → invoice_id` chain navigably.
 *
 * `order` and `invoice` resolve to owning-module routes via the shared
 * `<EntityLink>` component (Req 13.1). `plan` and `pod` have no dedicated
 * owning-module route / `EntityLink` type yet, so they are rendered as their
 * existing monospace display rather than dead links — keeping the chain
 * coherent without fabricating a destination (cf. task note: "render the chain
 * navigable where an EntityLink type exists, otherwise keep the existing
 * display"). A missing invoice (no QBO leg yet) renders an em dash.
 */
function ReconciliationChain({
  record,
  stopPropagation = false,
}: {
  record: ReconciliationRecord;
  stopPropagation?: boolean;
}) {
  const sep = <span className="text-gray-300">→</span>;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 font-mono text-xs">
      <EntityLink
        type="order"
        id={record.order_id}
        showId={false}
        stopPropagation={stopPropagation}
        data-testid={`chain-order-${record.reconciliation_id}`}
      />
      {sep}
      <span className="text-gray-700 break-all" title="Loading plan">
        {record.plan_id}
      </span>
      {sep}
      <span className="text-gray-700 break-all" title="Proof of delivery">
        {record.pod_id}
      </span>
      {sep}
      {record.invoice_id ? (
        <EntityLink
          type="invoice"
          id={record.invoice_id}
          showId={false}
          stopPropagation={stopPropagation}
          data-testid={`chain-invoice-${record.reconciliation_id}`}
        />
      ) : (
        <span className="text-gray-500" title="Invoice (pending)">
          —
        </span>
      )}
    </span>
  );
}

// ─── Table ───────────────────────────────────────────────────────────────────

interface ReconciliationTableProps {
  records: ReconciliationRecord[];
  onSelect: (record: ReconciliationRecord) => void;
  loading?: boolean;
  pagination?: {
    page: number;
    totalPages: number;
    onPageChange: (page: number) => void;
  };
}

function ReconciliationTable({
  records,
  onSelect,
  loading,
  pagination,
}: ReconciliationTableProps) {
  const columns: Column<ReconciliationRecord>[] = [
    {
      key: "alert",
      header: "Variance",
      width: 140,
      cell: (record) =>
        isAlertedRow(record) ? (
          <StatusBadge status="critical" label="Over threshold" />
        ) : (
          <StatusBadge status="ok" label="Within" />
        ),
    },
    {
      key: "chain",
      label: "Chain",
      title: () => "Order → plan → POD → invoice",
      className: "break-all",
      render: (record) => (
        <ReconciliationChain record={record} stopPropagation />
      ),
    },
    {
      key: "ordered_gallons",
      label: "Ordered",
      align: "right",
      className: "text-gray-700",
      render: (record) => formatGallons(record.ordered_gallons),
    },
    {
      key: "loaded_gallons",
      label: "Loaded",
      align: "right",
      className: "text-gray-700",
      render: (record) => formatGallons(record.loaded_gallons),
    },
    {
      key: "delivered_gallons",
      label: "Delivered",
      align: "right",
      className: "text-gray-700",
      render: (record) => formatGallons(record.delivered_gallons),
    },
    {
      key: "invoiced_gallons",
      label: "Invoiced",
      align: "right",
      className: "text-gray-700",
      render: (record) => formatGallons(record.invoiced_gallons),
    },
    {
      key: "variance_load_vs_order_pct",
      label: "L/O %",
      align: "right",
      className: "font-mono",
      render: (record) => (
        <span className={varianceCellClass(record.variance_load_vs_order_pct)}>
          {formatVariancePct(record.variance_load_vs_order_pct)}
        </span>
      ),
    },
    {
      key: "variance_delivered_vs_loaded_pct",
      label: "D/L %",
      align: "right",
      className: "font-mono",
      render: (record) => (
        <span
          className={varianceCellClass(record.variance_delivered_vs_loaded_pct)}
        >
          {formatVariancePct(record.variance_delivered_vs_loaded_pct)}
        </span>
      ),
    },
    {
      key: "variance_invoiced_vs_delivered_pct",
      label: "I/D %",
      align: "right",
      className: "font-mono",
      render: (record) => (
        <span
          className={varianceCellClass(
            record.variance_invoiced_vs_delivered_pct,
          )}
        >
          {formatVariancePct(record.variance_invoiced_vs_delivered_pct)}
        </span>
      ),
    },
    {
      key: "customer_id",
      label: "Customer",
      className: "text-xs break-all",
      render: (record) => (
        <EntityLink
          type="customer"
          id={record.customer_id}
          showId={false}
          stopPropagation
          data-testid={`pivot-customer-${record.reconciliation_id}`}
        />
      ),
    },
    {
      key: "assigned_asset_id",
      label: "Asset",
      className: "text-xs break-all",
      render: (record) => (
        <EntityLink
          type="asset"
          id={record.assigned_asset_id}
          showId={false}
          stopPropagation
          data-testid={`pivot-asset-${record.reconciliation_id}`}
        />
      ),
    },
    {
      key: "assigned_driver_id",
      label: "Driver",
      className: "text-xs break-all",
      render: (record) => (
        <EntityLink
          type="driver"
          id={record.assigned_driver_id}
          showId={false}
          stopPropagation
          data-testid={`pivot-driver-${record.reconciliation_id}`}
        />
      ),
    },
    {
      key: "generated_at",
      label: "Generated",
      className: "text-xs text-gray-500 whitespace-nowrap",
      render: (record) => formatTimestamp(record.generated_at),
    },
    {
      key: "actions",
      label: "",
      render: (record) => (
        <button
          type="button"
          onClick={() => onSelect(record)}
          className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-gray-700 hover:text-gray-900 border border-gray-200 rounded-md hover:bg-gray-50"
          aria-label={`Open POD ${record.pod_id}`}
        >
          <FileText className="w-3 h-3" aria-hidden="true" />
          POD
        </button>
      ),
    },
  ];

  return (
    <DataTable<ReconciliationRecord>
      ariaLabel="Reconciliation records"
      columns={columns}
      data={records}
      loading={loading}
      getRowId={(record) => record.reconciliation_id}
      rowTestId={(record) => `reconciliation-row-${record.reconciliation_id}`}
      pagination={pagination}
      emptyState={
        <span className="text-gray-500">
          No reconciliation records match the current filters.
        </span>
      }
    />
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
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load reconciliation records.",
      );
    } finally {
      setLoading(false);
    }
  }, [queryFilters]);

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

  const actions = useMemo(
    () => (
      <ExportCsvButton
        type="reconciliation"
        params={{
          order_id: filters.order_id,
          plan_id: filters.plan_id,
          pod_id: filters.pod_id,
          min_variance_pct: filters.min_variance_pct,
        }}
        subject="reconciliation"
        allowedRoles={["admin", "dispatcher"]}
      />
    ),
    [filters],
  );
  // Summary cards → title-row counts (design.md §6 rule 3).
  const counts = useMemo(
    () => (
      <span className="whitespace-nowrap">
        {number(total)} records · {number(alertedCount)} over threshold on this
        page
      </span>
    ),
    [total, alertedCount],
  );
  const embedded = usePageChrome({ actions, counts });

  const totalPages = Math.max(
    page + (hasNext ? 1 : 0),
    Math.ceil(total / PAGE_SIZE),
  );

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center gap-3 border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Reconciliation
          </PageTitle>
          <span className="text-xs text-text-muted">{counts}</span>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      <FiltersRow
        filters={filters}
        onChange={handleFiltersChange}
        onReset={handleResetFilters}
        onRefresh={refresh}
        loading={loading}
      />
      <div className="min-h-0 flex-1 overflow-auto">
        {error && !loading ? (
          <div
            role="alert"
            className="m-4 flex items-start gap-2 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800"
          >
            <AlertTriangle
              className="mt-0.5 h-4 w-4 flex-shrink-0"
              aria-hidden="true"
            />
            <div>
              <div className="font-medium">Could not load reconciliation.</div>
              <div className="mt-0.5 text-xs">{error}</div>
              <button
                type="button"
                onClick={refresh}
                className="mt-1 text-xs font-semibold text-link hover:underline"
              >
                Try again
              </button>
            </div>
          </div>
        ) : (
          <ReconciliationTable
            records={records}
            onSelect={setSelected}
            loading={loading}
            pagination={
              totalPages > 1
                ? { page, totalPages, onPageChange: setPage }
                : undefined
            }
          />
        )}
      </div>

      {selected && (
        <PodDetailDrawer
          record={selected}
          onClose={() => setSelected(null)}
          onError={(msg) => notify({ type: "error", message: msg })}
        />
      )}
    </div>
  );
}
