"use client";

/**
 * Terminal-sourcing UI (Task 11.8).
 *
 * Surfaces the Capability 8 outputs introduced by the Fuel Ops Hardening
 * spec so dispatchers can review ranked terminal recommendations, rack
 * prices, supplier contracts, and terminal wait warnings in a single
 * operations page:
 *
 *  * ``GET /api/fuel/sourcing/recommendations`` — ranked Terminal
 *    candidates for a (product, volume, origin, as_of) query with a
 *    ``rack_price_fallback`` banner when the live provider was
 *    unavailable and an aggregated ``wait_warning_terminal_ids`` flag.
 *    Validates Requirement 8.5.4.
 *  * ``GET /api/fuel/rack-prices`` — the latest OPIS rack prices the
 *    recommender scored against, filtered by the current request's
 *    canonical product_code and branded toggle so operators can
 *    cross-check the top picks against the price feed.
 *  * ``GET /api/fuel/supplier-contracts`` — active supplier contracts
 *    with their monthly rolling-lift summary so dispatchers see which
 *    contracts the recommender could apply.
 *  * ``GET /api/fuel/terminals/{terminal_id}/wait-summary`` — per-
 *    terminal rolling 2-hour wait summary, lazy-loaded when the user
 *    expands a candidate, so an exceeded wait threshold shows the
 *    most-recent samples and ``wait_warning_exceeded`` flag inline.
 *
 * Styling mirrors :file:`ReconciliationPage.tsx` (Tailwind utility
 * classes, inline status chips) so the page sits next to the other
 * ``components/ops/`` surfaces without visual drift.
 *
 * Validates: Requirement 8.5.4.
 */

import {
  AlertTriangle,
  Building2,
  Check,
  ChevronDown,
  ChevronUp,
  Clock,
  DollarSign,
  FileText,
  Gauge,
  Loader2,
  MapPin,
  RefreshCw,
  Search,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  type Column,
  EntityLink,
  Field,
  type FieldErrors,
  FormDialog,
  INPUT_CLASS,
  NumberField,
  ProductChip,
  ProductSelect,
  Table,
  ToastContainer,
  Toolbar,
  usePageChrome,
  useToasts,
} from "@/components/ui";
import { dateTime, money, number, pct, productName } from "../../lib/format";
import { ApiError } from "../../services/api";
import type {
  RackPrice,
  SourcingRecommendation,
  SourcingRecommendationsQuery,
  SourcingTerminalCandidate,
  SupplierContractResponse,
  Terminal,
  TerminalWaitReportCreateRequest,
  TerminalWaitSummary,
} from "../../services/fuelApi";
import {
  getSourcingRecommendations,
  getTerminalWaitSummary,
  listRackPrices,
  listSupplierContracts,
  listTerminals,
  submitTerminalWaitReport,
} from "../../services/fuelApi";

// ─── Defaults and constants ──────────────────────────────────────────────────

/** Safety cap for the rack-prices and supplier-contract side panels. */
const SIDE_PANEL_PAGE_SIZE = 50;

// ─── Formatters ──────────────────────────────────────────────────────────────

export function formatUsd(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return money(value, { decimals: 4 });
}

export function formatGallons(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return number(value);
}

export function formatKm(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${number(value, { decimals: 1 })} km`;
}

export function formatMinutes(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${number(value)} min`;
}

export function formatScore(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return number(value, { decimals: 3 });
}

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  return dateTime(iso);
}

/** Human-readable rank label (1st, 2nd, 3rd, 4th, …). */
export function rankLabel(index: number): string {
  const n = index + 1;
  if (n === 1) return "1st";
  if (n === 2) return "2nd";
  if (n === 3) return "3rd";
  return `${n}th`;
}

// ─── Query form ──────────────────────────────────────────────────────────────

interface QueryFormState {
  product_code: string;
  volume_gallons: string;
  origin_lat: string;
  origin_lon: string;
  branded: "any" | "branded" | "unbranded";
  truck_id: string;
  run_id: string;
  as_of: string; // ISO datetime-local format (blank → now)
  terminal_ids: string;
}

const EMPTY_FORM: QueryFormState = {
  product_code: "",
  volume_gallons: "",
  origin_lat: "",
  origin_lon: "",
  branded: "any",
  truck_id: "",
  run_id: "",
  as_of: "",
  terminal_ids: "",
};

type QueryValues = Omit<
  QueryFormState,
  "volume_gallons" | "origin_lat" | "origin_lon"
> & {
  volume_gallons: number | null;
  origin_lat: number | null;
  origin_lon: number | null;
};

const toValues = (f: QueryFormState): QueryValues => {
  const n = (v: string) => (v.trim() === "" ? null : Number(v));
  return {
    ...f,
    volume_gallons: n(f.volume_gallons),
    origin_lat: n(f.origin_lat),
    origin_lon: n(f.origin_lon),
  };
};

const toForm = (v: QueryValues): QueryFormState => {
  const s = (x: number | null) =>
    x === null || Number.isNaN(x) ? "" : String(x);
  return {
    ...v,
    volume_gallons: s(v.volume_gallons),
    origin_lat: s(v.origin_lat),
    origin_lon: s(v.origin_lon),
  };
};

/** Field-level errors for the query dialog (same rules as validateQueryForm). */
function queryFieldErrors(v: QueryValues): FieldErrors {
  const e: FieldErrors = {};
  if (!v.product_code.trim()) e.product_code = "Choose a product.";
  if (
    v.volume_gallons === null ||
    Number.isNaN(v.volume_gallons) ||
    v.volume_gallons <= 0
  )
    e.volume_gallons = "Volume must be a positive number.";
  if (
    v.origin_lat === null ||
    Number.isNaN(v.origin_lat) ||
    v.origin_lat < -90 ||
    v.origin_lat > 90
  )
    e.origin_lat = "Latitude must be between -90 and 90.";
  if (
    v.origin_lon === null ||
    Number.isNaN(v.origin_lon) ||
    v.origin_lon < -180 ||
    v.origin_lon > 180
  )
    e.origin_lon = "Longitude must be between -180 and 180.";
  const asOf = toForm(v);
  const r = validateQueryForm(asOf);
  if (!r.ok && Object.keys(e).length === 0) e.as_of = r.error;
  return e;
}

interface QueryDialogProps {
  open: boolean;
  form: QueryFormState;
  onClose: () => void;
  /** Runs the ranking; throw to keep the dialog open with the message. */
  onSubmit: (form: QueryFormState) => Promise<void>;
  /** Canonical terminals backing the terminal-restriction picker. */
  terminals: Terminal[];
}

/**
 * The recommendation query, as a `FormDialog` (design.md §5 "Sourcing
 * order": md, NumberField). Product is a `ProductSelect`; volume is whole
 * gallons; the origin is two 4-decimal coordinates. The terminal restriction
 * is a canonical terminal picker (cross-module-entity-linkage Req 9.2), stored
 * as a comma-separated string so `validateQueryForm` is unchanged.
 */
function QueryDialog({
  open,
  form,
  onClose,
  onSubmit,
  terminals,
}: QueryDialogProps) {
  return (
    <FormDialog<QueryValues & Record<string, unknown>>
      open={open}
      size="md"
      title="Rank loading terminals"
      help="Rank terminals against live rack prices, contracts and wait times for one truck run."
      submitLabel="Rank terminals"
      successMessage={null}
      initialValues={toValues(form) as QueryValues & Record<string, unknown>}
      validate={queryFieldErrors}
      onSubmit={(v) => onSubmit(toForm(v))}
      onClose={onClose}
    >
      {({ values, set, errors }) => {
        const selected = values.terminal_ids
          .split(",")
          .map((x) => x.trim())
          .filter(Boolean);
        return (
          <>
            <Field
              label="Product"
              required
              error={errors.product_code}
              id="sourcing-product-code"
            >
              <ProductSelect
                id="sourcing-product-code"
                value={values.product_code || null}
                onChange={(code) => set("product_code", code)}
              />
            </Field>
            <Field
              label="Volume"
              required
              error={errors.volume_gallons}
              span={1}
              id="sourcing-volume"
            >
              <NumberField
                id="sourcing-volume"
                unit="gal"
                min={1}
                value={values.volume_gallons}
                onChange={(n) => set("volume_gallons", n)}
                placeholder="8,000"
              />
            </Field>
            <Field label="Branded filter" span={1}>
              <select
                id="sourcing-branded"
                className={INPUT_CLASS}
                value={values.branded}
                onChange={(e) =>
                  set("branded", e.target.value as QueryFormState["branded"])
                }
              >
                <option value="any">Any</option>
                <option value="branded">Branded only</option>
                <option value="unbranded">Unbranded only</option>
              </select>
            </Field>
            <Field
              label="Origin latitude"
              required
              error={errors.origin_lat}
              span={1}
              id="sourcing-lat"
            >
              <NumberField
                id="sourcing-lat"
                decimals={4}
                min={-90}
                max={90}
                value={values.origin_lat}
                onChange={(n) => set("origin_lat", n)}
                placeholder="40.7128"
              />
            </Field>
            <Field
              label="Origin longitude"
              required
              error={errors.origin_lon}
              span={1}
              id="sourcing-lon"
            >
              <NumberField
                id="sourcing-lon"
                decimals={4}
                min={-180}
                max={180}
                value={values.origin_lon}
                onChange={(n) => set("origin_lon", n)}
                placeholder="-74.0060"
              />
            </Field>
            <Field label="Truck ID" span={1}>
              <input
                id="sourcing-truck-id"
                type="text"
                placeholder="e.g. T-0042"
                className={INPUT_CLASS}
                value={values.truck_id}
                onChange={(e) => set("truck_id", e.target.value)}
              />
            </Field>
            <Field label="Run ID" span={1}>
              <input
                id="sourcing-run-id"
                type="text"
                placeholder="e.g. run-1234"
                className={INPUT_CLASS}
                value={values.run_id}
                onChange={(e) => set("run_id", e.target.value)}
              />
            </Field>
            <Field
              label="As-of time"
              help="Leave blank for now."
              error={errors.as_of}
            >
              <input
                id="sourcing-as-of"
                type="datetime-local"
                className={INPUT_CLASS}
                value={values.as_of}
                onChange={(e) => set("as_of", e.target.value)}
              />
            </Field>
            {terminals.length > 0 ? (
              <Field
                label="Restrict to terminals"
                help="Hold ⌘/Ctrl to choose several; leave empty to rank all."
              >
                <select
                  id="sourcing-terminal-ids"
                  multiple
                  className={`${INPUT_CLASS} h-28 py-1`}
                  value={selected}
                  onChange={(e) =>
                    set(
                      "terminal_ids",
                      Array.from(e.target.selectedOptions)
                        .map((o) => o.value)
                        .filter(Boolean)
                        .join(","),
                    )
                  }
                >
                  {terminals.map((t) => (
                    <option key={t.terminal_id} value={t.terminal_id}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </Field>
            ) : (
              <p className="col-span-2 rounded-lg border border-dashed border-slate-300 px-3 py-2 text-xs text-text-muted">
                No terminals set up yet, so every eligible terminal is ranked.
                Add terminals under Compliance → Terminals to restrict by
                terminal.
              </p>
            )}
          </>
        );
      }}
    </FormDialog>
  );
}

// ─── Form validation ─────────────────────────────────────────────────────────

interface ValidatedQuery {
  query: SourcingRecommendationsQuery;
}

/**
 * Pure validator that coerces the form state into the query-shape the
 * backend expects. Surfaces actionable error messages rather than
 * silently dropping malformed fields. Exported so unit tests can pin
 * the exact contract (required-fields, numeric-range, CSV trimming).
 */
export function validateQueryForm(
  form: QueryFormState,
): { ok: true; value: ValidatedQuery } | { ok: false; error: string } {
  const productCode = form.product_code.trim();
  if (!productCode) {
    return { ok: false, error: "Product code is required." };
  }

  const volume = Number(form.volume_gallons);
  if (!Number.isFinite(volume) || volume <= 0) {
    return {
      ok: false,
      error: "Volume (gallons) must be a positive number.",
    };
  }

  const lat = Number(form.origin_lat);
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
    return {
      ok: false,
      error: "Origin latitude must be between -90 and 90.",
    };
  }

  const lon = Number(form.origin_lon);
  if (!Number.isFinite(lon) || lon < -180 || lon > 180) {
    return {
      ok: false,
      error: "Origin longitude must be between -180 and 180.",
    };
  }

  const query: SourcingRecommendationsQuery = {
    product_code: productCode,
    volume_gallons: volume,
    origin_lat: lat,
    origin_lon: lon,
  };

  if (form.branded === "branded") query.branded = true;
  else if (form.branded === "unbranded") query.branded = false;

  const truckId = form.truck_id.trim();
  if (truckId) query.truck_id = truckId;

  const runId = form.run_id.trim();
  if (runId) query.run_id = runId;

  const asOf = form.as_of.trim();
  if (asOf) {
    // ``datetime-local`` yields ``YYYY-MM-DDTHH:mm`` in local time with
    // no timezone. We append ``:00`` seconds and let the backend coerce
    // to UTC — the endpoint accepts both naive and tz-aware ISO
    // strings.
    const parsed = new Date(asOf);
    if (Number.isNaN(parsed.getTime())) {
      return { ok: false, error: "As-of time must be a valid timestamp." };
    }
    query.as_of = parsed.toISOString();
  }

  const terminalIds = form.terminal_ids
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (terminalIds.length > 0) {
    query.terminal_ids = terminalIds.join(",");
  }

  return { ok: true, value: { query } };
}

// ─── Recommendation banner ───────────────────────────────────────────────────

function RecommendationBanner({
  recommendation,
}: {
  recommendation: SourcingRecommendation;
}) {
  const waitWarningCount = (recommendation.wait_warning_terminal_ids ?? [])
    .length;
  return (
    <div className="space-y-2">
      {recommendation.rack_price_fallback && (
        <div
          data-testid="sourcing-rack-fallback-banner"
          className="flex items-start gap-2 p-3 rounded-lg bg-warning-light border border-warning-light text-sm text-warning-dark"
        >
          <AlertTriangle
            className="w-4 h-4 mt-0.5 flex-shrink-0"
            aria-hidden="true"
          />
          <div>
            <div className="font-medium">Rack prices served from cache</div>
            <div className="text-xs mt-0.5">
              The live rack-price provider was unavailable; candidates are
              ranked against the most recent cached prices. Re-run in a few
              minutes to pick up live pricing.
            </div>
          </div>
        </div>
      )}
      {waitWarningCount > 0 && (
        <div
          data-testid="sourcing-wait-warning-banner"
          className="flex items-start gap-2 p-3 rounded-lg bg-error-light border border-error-light text-sm text-error-dark"
        >
          <Clock className="w-4 h-4 mt-0.5 flex-shrink-0" aria-hidden="true" />
          <div>
            <div className="font-medium">
              {waitWarningCount === 1
                ? "1 terminal exceeds the wait-time threshold."
                : `${waitWarningCount} terminals exceed the wait-time threshold.`}
            </div>
            <div className="text-xs mt-0.5">
              Terminals:{" "}
              <span className="font-mono">
                {(recommendation.wait_warning_terminal_ids ?? []).join(", ")}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Candidate row with collapsible details ──────────────────────────────────

interface CandidateRowProps {
  candidate: SourcingTerminalCandidate;
  rank: number;
  isBest: boolean;
}

// ─── Wait-report submission form ─────────────────────────────────────────────

interface WaitReportFormValues {
  wait_minutes: string;
  source: "driver_report" | "eld_geofence";
  reporter_id: string;
  notes: string;
}

export interface WaitReportFormErrors {
  wait_minutes?: string;
  reporter_id?: string;
}

const EMPTY_WAIT_REPORT_FORM: WaitReportFormValues = {
  wait_minutes: "",
  source: "driver_report",
  reporter_id: "",
  notes: "",
};

/**
 * Pure validator for the wait-report form. Mirrors the backend
 * contract for ``POST /api/fuel/terminals/{terminal_id}/wait-reports``
 * — ``wait_minutes`` must be a non-negative number and
 * ``reporter_id`` is required when ``source === "driver_report"``
 * (Req 8.4.2). Exported so the unit test can pin the exact rules.
 */
export function validateWaitReportForm(
  values: WaitReportFormValues,
): WaitReportFormErrors {
  const errors: WaitReportFormErrors = {};
  const waitNum = Number(values.wait_minutes);
  if (
    values.wait_minutes.trim() === "" ||
    !Number.isFinite(waitNum) ||
    waitNum < 0
  ) {
    errors.wait_minutes = "Wait minutes must be a non-negative number.";
  }
  if (values.source === "driver_report" && !values.reporter_id.trim()) {
    errors.reporter_id = "Reporter ID is required for driver reports.";
  }
  return errors;
}

interface WaitReportFormProps {
  terminalId: string;
  onSubmitted: () => void;
}

type WaitReportDialogValues = Omit<WaitReportFormValues, "wait_minutes"> & {
  wait_minutes: number | null;
};

const EMPTY_WAIT_REPORT_DIALOG: WaitReportDialogValues = {
  ...EMPTY_WAIT_REPORT_FORM,
  wait_minutes: null,
};

/**
 * "Report wait time" opens an md FormDialog (D9: every create flow) to file
 * a manual observation against the candidate terminal without leaving the
 * Sourcing page. Whole minutes via NumberField. The optional dispatcher note
 * is persisted end-to-end via ``TerminalWaitReportCreateRequest.notes`` so
 * the rolling wait-time context survives into the ``terminal_wait_reports``
 * ES index for post-hoc analytics and audit (Req 8.4.2). On success the
 * dialog closes, toasts and the wait summary re-fetches.
 */
function WaitReportForm({ terminalId, onSubmitted }: WaitReportFormProps) {
  const [open, setOpen] = useState(false);

  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-xs text-gray-600">
        Seen a different wait at this terminal?
      </span>
      <Button
        size="sm"
        variant="secondary"
        onClick={() => setOpen(true)}
        data-testid={`wait-report-open-${terminalId}`}
      >
        Report wait time
      </Button>
      <FormDialog<WaitReportDialogValues>
        open={open}
        size="md"
        title="Report wait time"
        help={`Terminal ${terminalId}. The report joins the rolling 2-hour wait summary.`}
        submitLabel="Submit wait report"
        successMessage="Wait report submitted"
        initialValues={EMPTY_WAIT_REPORT_DIALOG}
        validate={(v) =>
          validateWaitReportForm({
            ...v,
            wait_minutes:
              v.wait_minutes == null || Number.isNaN(v.wait_minutes)
                ? ""
                : String(v.wait_minutes),
          }) as FieldErrors
        }
        onSubmit={async (v) => {
          const body: TerminalWaitReportCreateRequest = {
            wait_minutes: v.wait_minutes as number,
            source: v.source,
            observed_at: new Date().toISOString(),
            notes: v.notes.trim() || undefined,
          };
          const reporter = v.reporter_id.trim();
          if (reporter) body.reporter_id = reporter;
          await submitTerminalWaitReport(terminalId, body);
        }}
        onSaved={onSubmitted}
        onClose={() => setOpen(false)}
      >
        {({ values, set, errors }) => (
          <>
            <Field
              label="Wait minutes"
              required
              span={1}
              error={errors.wait_minutes}
              id={`wr-minutes-${terminalId}`}
            >
              <NumberField
                id={`wr-minutes-${terminalId}`}
                unit="min"
                min={0}
                decimals={0}
                value={values.wait_minutes}
                onChange={(n) => set("wait_minutes", n)}
                placeholder="45"
              />
            </Field>
            <Field label="Source" span={1} id={`wr-source-${terminalId}`}>
              <select
                id={`wr-source-${terminalId}`}
                className={INPUT_CLASS}
                value={values.source}
                onChange={(e) =>
                  set(
                    "source",
                    e.target.value as WaitReportFormValues["source"],
                  )
                }
              >
                <option value="driver_report">Driver report</option>
                <option value="eld_geofence">ELD geofence</option>
              </select>
            </Field>
            <Field
              label="Reporter ID"
              required={values.source === "driver_report"}
              error={errors.reporter_id}
              id={`wr-reporter-${terminalId}`}
            >
              <input
                id={`wr-reporter-${terminalId}`}
                type="text"
                className={INPUT_CLASS}
                value={values.reporter_id}
                onChange={(e) => set("reporter_id", e.target.value)}
                placeholder="driver-042"
              />
            </Field>
            <Field label="Notes (optional)" id={`wr-notes-${terminalId}`}>
              <textarea
                id={`wr-notes-${terminalId}`}
                rows={2}
                className={INPUT_CLASS}
                value={values.notes}
                onChange={(e) => set("notes", e.target.value)}
                placeholder="Why was this wait time observed?"
              />
            </Field>
          </>
        )}
      </FormDialog>
    </div>
  );
}

// ─── Candidate row with collapsible details ──────────────────────────────────

function CandidateRow({ candidate, rank, isBest }: CandidateRowProps) {
  const [expanded, setExpanded] = useState(isBest);
  const [waitSummary, setWaitSummary] = useState<TerminalWaitSummary | null>(
    null,
  );
  const [waitLoading, setWaitLoading] = useState(false);
  const [waitError, setWaitError] = useState<string | null>(null);
  const [waitReloadToken, setWaitReloadToken] = useState(0);

  const handleToggle = useCallback(() => {
    setExpanded((prev) => !prev);
  }, []);

  const refreshWaitSummary = useCallback(() => {
    setWaitReloadToken((t) => t + 1);
  }, []);

  // Lazy-load wait summary on first expand, and re-fetch whenever the
  // reload token is bumped (e.g. after a dispatcher submits a wait
  // report through the inline form below).
  useEffect(() => {
    if (!expanded) return;
    if (waitReloadToken === 0 && (waitSummary || waitLoading)) return;
    let cancelled = false;
    setWaitLoading(true);
    setWaitError(null);
    getTerminalWaitSummary(candidate.terminal_id)
      .then((res) => {
        if (!cancelled) setWaitSummary(res);
      })
      .catch((err) => {
        if (cancelled) return;
        const message =
          err instanceof Error ? err.message : "Failed to load wait summary.";
        setWaitError(message);
      })
      .finally(() => {
        if (!cancelled) setWaitLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // `waitSummary` / `waitLoading` are intentionally omitted from the
    // dep list so a re-fetch driven by `waitReloadToken` is not
    // short-circuited by the stale-memoized summary.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded, candidate.terminal_id, waitReloadToken]);

  return (
    <div
      data-testid={`sourcing-candidate-${candidate.terminal_id}`}
      className={`border rounded-lg overflow-hidden ${
        isBest ? "border-success bg-success-light" : "border-gray-200 bg-white"
      }`}
    >
      <button
        type="button"
        onClick={handleToggle}
        aria-expanded={expanded}
        aria-label={
          expanded
            ? `Collapse ${candidate.terminal_id}`
            : `Expand ${candidate.terminal_id}`
        }
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-gray-50/50"
      >
        <div className="flex items-center gap-3 min-w-0">
          <div
            className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-semibold ${
              isBest ? "bg-success text-white" : "bg-gray-100 text-gray-700"
            }`}
            aria-hidden="true"
          >
            {rankLabel(rank)}
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm font-semibold text-primary truncate">
              <span className="font-mono">{candidate.terminal_id}</span>
              {candidate.branded_flag ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-info-light text-info-dark font-medium">
                  Branded
                </span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 font-medium">
                  Unbranded
                </span>
              )}
              {candidate.contract_id && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-brand-secondary-soft text-brand-secondary font-medium inline-flex items-center gap-1">
                  <FileText className="w-3 h-3" aria-hidden="true" />
                  Contract
                </span>
              )}
              {candidate.wait_warning && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-error-light text-error-dark font-medium inline-flex items-center gap-1">
                  <Clock className="w-3 h-3" aria-hidden="true" />
                  Wait warning
                </span>
              )}
            </div>
            <div className="text-xs text-gray-500 mt-0.5 flex items-center gap-4 flex-wrap">
              <span className="inline-flex items-center gap-1">
                <DollarSign className="w-3 h-3" aria-hidden="true" />
                {formatUsd(candidate.price_per_gallon_usd)} / gal
              </span>
              <span className="inline-flex items-center gap-1">
                <MapPin className="w-3 h-3" aria-hidden="true" />
                {formatKm(candidate.distance_km_from_start)}
              </span>
              <span className="inline-flex items-center gap-1">
                <Clock className="w-3 h-3" aria-hidden="true" />
                {formatMinutes(candidate.avg_wait_minutes)}
              </span>
              <span className="inline-flex items-center gap-1">
                <Gauge className="w-3 h-3" aria-hidden="true" />
                score {formatScore(candidate.score)}
              </span>
            </div>
          </div>
        </div>
        <div className="flex-shrink-0 text-gray-500 ml-2">
          {expanded ? (
            <ChevronUp className="w-4 h-4" aria-hidden="true" />
          ) : (
            <ChevronDown className="w-4 h-4" aria-hidden="true" />
          )}
        </div>
      </button>

      {expanded && (
        <div className="px-4 pb-4 pt-0 border-t border-gray-100 space-y-3">
          <div className="pt-3 text-sm">
            <span className="text-[10px] uppercase tracking-wide text-gray-500 mr-2">
              Terminal
            </span>
            <EntityLink
              type="terminal"
              id={candidate.terminal_id}
              data-testid={`sourcing-candidate-link-${candidate.terminal_id}`}
            />
            {candidate.contract_id && (
              <span className="ml-3 text-xs text-gray-600">
                <span className="text-[10px] uppercase tracking-wide text-gray-500 mr-1">
                  Contract
                </span>
                <span className="font-mono">{candidate.contract_id}</span>
              </span>
            )}
          </div>
          {candidate.reasons.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                Ranking reasons
              </div>
              <ul className="space-y-1 text-sm text-gray-700">
                {candidate.reasons.map((reason, idx) => (
                  <li key={idx} className="flex items-start gap-2">
                    <Check
                      className="w-3.5 h-3.5 text-success mt-0.5 flex-shrink-0"
                      aria-hidden="true"
                    />
                    <span>{reason}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div>
            <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
              Terminal wait summary
            </div>
            {waitLoading ? (
              <div className="inline-flex items-center gap-2 text-sm text-gray-500">
                <Loader2 className="w-4 h-4 animate-spin" aria-hidden="true" />
                Loading wait summary…
              </div>
            ) : waitError ? (
              <div className="text-sm text-gray-700 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
                {waitError}
              </div>
            ) : waitSummary ? (
              <dl className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-sm">
                <div>
                  <dt className="text-[10px] uppercase text-gray-500">Avg</dt>
                  <dd className="font-semibold text-gray-900">
                    {formatMinutes(waitSummary.avg_wait_minutes)}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase text-gray-500">Max</dt>
                  <dd className="font-semibold text-gray-900">
                    {formatMinutes(waitSummary.max_wait_minutes)}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase text-gray-500">
                    Samples
                  </dt>
                  <dd className="font-semibold text-gray-900">
                    {waitSummary.sample_count}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase text-gray-500">
                    Threshold
                  </dt>
                  <dd
                    className={
                      waitSummary.wait_warning_exceeded
                        ? "font-semibold text-error-dark"
                        : "font-semibold text-gray-900"
                    }
                  >
                    {formatMinutes(waitSummary.wait_warning_threshold_minutes)}
                    {waitSummary.wait_warning_exceeded ? " ⚠" : ""}
                  </dd>
                </div>
                <div className="col-span-2 sm:col-span-4">
                  <dt className="text-[10px] uppercase text-gray-500">
                    Most recent report
                  </dt>
                  <dd className="text-gray-700">
                    {formatTimestamp(waitSummary.most_recent_report_at)}
                  </dd>
                </div>
              </dl>
            ) : null}
          </div>

          <div className="pt-3 border-t border-gray-100">
            <WaitReportForm
              terminalId={candidate.terminal_id}
              onSubmitted={refreshWaitSummary}
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Rack-prices sidebar ─────────────────────────────────────────────────────

const rackPriceColumns: Column<RackPrice>[] = [
  {
    key: "terminal",
    label: "Terminal",
    className: "font-mono text-xs text-gray-700 break-all",
    render: (price) => (
      <EntityLink
        type="terminal"
        id={price.terminal_id}
        data-testid={`rack-price-row-${price.rack_price_id}`}
      />
    ),
  },
  {
    key: "price",
    label: "Price",
    align: "right",
    className: "font-mono text-gray-900",
    render: (price) => formatUsd(price.price_per_gallon_usd),
  },
  {
    key: "brand",
    label: "Brand",
    className: "text-xs",
    render: (price) =>
      price.branded_flag ? (
        <span className="inline-flex items-center text-[10px] px-1.5 py-0.5 rounded bg-info-light text-info-dark font-medium">
          {price.supplier_brand || "Branded"}
        </span>
      ) : (
        <span className="text-[10px] text-gray-500">Unbranded</span>
      ),
  },
  {
    key: "effective",
    label: "Effective",
    className: "text-xs text-gray-500 whitespace-nowrap",
    render: (price) => formatTimestamp(price.effective_at),
  },
];

function RackPricesPanel({
  prices,
  loading,
  error,
  productFilter,
}: {
  prices: RackPrice[];
  loading: boolean;
  error: string | null;
  productFilter: string;
}) {
  return (
    <div
      data-testid="sourcing-rack-prices-panel"
      className="border border-gray-200 rounded-lg overflow-hidden bg-white"
    >
      <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold text-primary">
            Latest rack prices
          </div>
          <div className="text-xs text-gray-500">
            Filtered by <span className="font-medium">{productFilter}</span>
          </div>
        </div>
        <DollarSign className="w-4 h-4 text-gray-500" aria-hidden="true" />
      </div>
      <div className="max-h-[360px] overflow-auto">
        {loading ? (
          <div className="flex items-center justify-center py-8 text-sm text-gray-500">
            <Loader2 className="w-4 h-4 animate-spin mr-2" aria-hidden="true" />
            Loading rack prices…
          </div>
        ) : error ? (
          <div className="px-4 py-3 text-xs text-error-dark bg-error-light">
            {error}
          </div>
        ) : prices.length === 0 ? (
          <div className="px-4 py-6 text-sm text-gray-500 text-center">
            No rack prices found for this product.
          </div>
        ) : (
          <Table<RackPrice>
            ariaLabel="Latest rack prices"
            variant="compact"
            columns={rackPriceColumns}
            data={prices}
            getRowId={(price) => price.rack_price_id}
          />
        )}
      </div>
    </div>
  );
}

// ─── Supplier-contracts sidebar ──────────────────────────────────────────────

function SupplierContractsPanel({
  contracts,
  loading,
  error,
  productFilter,
}: {
  contracts: SupplierContractResponse[];
  loading: boolean;
  error: string | null;
  productFilter: string;
}) {
  return (
    <div
      data-testid="sourcing-supplier-contracts-panel"
      className="border border-gray-200 rounded-lg overflow-hidden bg-white"
    >
      <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold text-primary">
            Supplier contracts
          </div>
          <div className="text-xs text-gray-500">
            Filtered by <span className="font-medium">{productFilter}</span>
          </div>
        </div>
        <Building2 className="w-4 h-4 text-gray-500" aria-hidden="true" />
      </div>
      <div className="max-h-[360px] overflow-auto">
        {loading ? (
          <div className="flex items-center justify-center py-8 text-sm text-gray-500">
            <Loader2 className="w-4 h-4 animate-spin mr-2" aria-hidden="true" />
            Loading contracts…
          </div>
        ) : error ? (
          <div className="px-4 py-3 text-xs text-error-dark bg-error-light">
            {error}
          </div>
        ) : contracts.length === 0 ? (
          <div className="px-4 py-6 text-sm text-gray-500 text-center">
            No active supplier contracts for this product.
          </div>
        ) : (
          <ul className="divide-y divide-gray-100">
            {contracts.map(({ contract, lift_summary }) => (
              <li
                key={contract.contract_id}
                data-testid={`supplier-contract-row-${contract.contract_id}`}
                className="px-4 py-3 text-sm hover:bg-gray-50/60"
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="font-medium text-gray-900 truncate">
                      {contract.supplier_name}
                    </div>
                    <div className="text-xs text-gray-500 font-mono truncate">
                      {contract.contract_id}
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    {contract.branded_required && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-info-light text-info-dark font-medium">
                        Branded
                      </span>
                    )}
                    {lift_summary.below_minimum && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-error-light text-error-dark font-medium">
                        Below min
                      </span>
                    )}
                  </div>
                </div>
                <dl className="mt-2 grid grid-cols-2 gap-2 text-xs text-gray-700">
                  <div>
                    <dt className="text-[10px] uppercase text-gray-500">
                      Contract price
                    </dt>
                    <dd className="font-mono">
                      {contract.contract_price_per_gallon_usd == null
                        ? "—"
                        : formatUsd(contract.contract_price_per_gallon_usd)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[10px] uppercase text-gray-500">
                      Monthly min
                    </dt>
                    <dd>
                      {contract.minimum_lift_gallons_per_month == null
                        ? "—"
                        : `${formatGallons(contract.minimum_lift_gallons_per_month)} gal`}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[10px] uppercase text-gray-500">
                      Lifted ({lift_summary.yyyy_mm})
                    </dt>
                    <dd>
                      {formatGallons(lift_summary.gallons_lifted_this_month)}{" "}
                      gal
                      {lift_summary.percent_of_minimum != null && (
                        <span className="ml-1 text-gray-500">
                          (
                          {pct(lift_summary.percent_of_minimum, {
                            decimals: 1,
                          })}
                          )
                        </span>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[10px] uppercase text-gray-500">
                      Effective
                    </dt>
                    <dd>
                      {contract.effective_from}
                      {contract.effective_to
                        ? ` → ${contract.effective_to}`
                        : " →"}
                    </dd>
                  </div>
                </dl>
                {(contract.preferred_terminal_ids ?? []).length > 0 && (
                  <div className="mt-1 text-[11px] text-gray-600 flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="uppercase text-[9px] text-gray-500 mr-1">
                      Terminals:
                    </span>
                    {(contract.preferred_terminal_ids ?? []).map((tid) => (
                      <EntityLink
                        key={tid}
                        type="terminal"
                        id={tid}
                        className="text-xs"
                      />
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ─── Page ────────────────────────────────────────────────────────────────────

export interface SourcingPageProps {
  /** Initial form state — useful when linking in from a Loading_Plan. */
  initialQuery?: Partial<QueryFormState>;
}

/**
 * Top-level terminal-sourcing operations page wiring the form, the
 * recommendation list, and the rack-price / contracts side panels.
 * Consumers mount this via the dashboard sidebar (see
 * :file:`app/dashboard/(today)/page.tsx`).
 */
export default function SourcingPage({ initialQuery }: SourcingPageProps = {}) {
  const { toasts, addToast, dismissToast } = useToasts();

  const [form, setForm] = useState<QueryFormState>({
    ...EMPTY_FORM,
    ...initialQuery,
  });
  const [recommendation, setRecommendation] =
    useState<SourcingRecommendation | null>(null);
  const [loadingRec, setLoadingRec] = useState(false);
  const [recError, setRecError] = useState<string | null>(null);

  const [rackPrices, setRackPrices] = useState<RackPrice[]>([]);
  const [rackLoading, setRackLoading] = useState(false);
  const [rackError, setRackError] = useState<string | null>(null);

  const [contracts, setContracts] = useState<SupplierContractResponse[]>([]);
  const [contractsLoading, setContractsLoading] = useState(false);
  const [contractsError, setContractsError] = useState<string | null>(null);

  // Canonical terminals backing the terminal-restriction picker (replacing the
  // free-text id box — cross-module-entity-linkage Req 9.2). Best-effort: a
  // failed/empty load degrades the picker to an "all terminals" hint rather
  // than blocking the page.
  const [terminals, setTerminals] = useState<Terminal[]>([]);

  // Pre-load the rack-price and supplier-contract side panels on mount so
  // dispatchers see the latest market data without first running a
  // recommendation query. Once a query is submitted, handleSubmit refreshes
  // both panels filtered to the queried product_code. Guarded by a ref so
  // the unfiltered preload only runs once and never clobbers query-scoped
  // results.
  const sidePanelsPreloaded = useRef(false);
  useEffect(() => {
    if (sidePanelsPreloaded.current) return;
    sidePanelsPreloaded.current = true;
    let cancelled = false;

    setRackLoading(true);
    listRackPrices({ size: SIDE_PANEL_PAGE_SIZE })
      .then((res) => {
        if (!cancelled) setRackPrices(res.items);
      })
      .catch((err) => {
        if (!cancelled) {
          setRackError(
            err instanceof Error ? err.message : "Failed to load rack prices.",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setRackLoading(false);
      });

    setContractsLoading(true);
    listSupplierContracts({ status: "active", size: SIDE_PANEL_PAGE_SIZE })
      .then((res) => {
        if (!cancelled) setContracts(res.items);
      })
      .catch((err) => {
        if (!cancelled) {
          setContractsError(
            err instanceof Error
              ? err.message
              : "Failed to load supplier contracts.",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setContractsLoading(false);
      });

    // Load the canonical terminals that back the picker. Best-effort — a
    // failure leaves the picker in its "all terminals" fallback state.
    listTerminals({ status: "active", size: SIDE_PANEL_PAGE_SIZE })
      .then((res) => {
        if (!cancelled) setTerminals(res.items);
      })
      .catch(() => {
        /* picker degrades to the "all terminals" hint */
      });

    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Load the recommendation, then refresh the rack-prices and contracts
   * side-panels using the same canonical product_code the backend
   * returned. Side-panel failures don't block the main recommendation
   * render — they surface inline error chips.
   */
  const [dialogOpen, setDialogOpen] = useState(false);
  const runQuery = useCallback(async (next: QueryFormState) => {
    setForm(next);
    const result = validateQueryForm(next);
    if (!result.ok) throw new Error(result.error);
    const { query } = result.value;

    setLoadingRec(true);
    setRecError(null);
    let persisted: SourcingRecommendation | null = null;
    try {
      persisted = await getSourcingRecommendations(query);
      setRecommendation(persisted);
    } catch (err) {
      const message =
        err instanceof ApiError && err.message
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to load terminal recommendations.";
      setRecError(message);
      setRecommendation(null);
      throw new Error(message);
    } finally {
      setLoadingRec(false);
    }

    // Refresh rack prices + contracts using the canonical product_code
    // the backend returned after product catalog canonicalization.
    const productCode = persisted?.product_code ?? query.product_code;
    const brandedFilter = query.branded;

    setRackLoading(true);
    setRackError(null);
    try {
      const res = await listRackPrices({
        product_code: productCode,
        branded_flag: brandedFilter,
        size: SIDE_PANEL_PAGE_SIZE,
      });
      setRackPrices(res.items);
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to load rack prices.";
      setRackError(message);
    } finally {
      setRackLoading(false);
    }

    setContractsLoading(true);
    setContractsError(null);
    try {
      const res = await listSupplierContracts({
        product_code: productCode,
        status: "active",
        size: SIDE_PANEL_PAGE_SIZE,
      });
      setContracts(res.items);
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to load supplier contracts.";
      setContractsError(message);
    } finally {
      setContractsLoading(false);
    }
  }, []);

  // Re-rank with the last query (errors show in the page's error panel).
  const handleRerank = useCallback(() => {
    runQuery(form).catch((err: unknown) =>
      addToast(
        err instanceof Error ? err.message : "Failed to rank terminals.",
        "error",
      ),
    );
  }, [runQuery, form, addToast]);

  const handleReset = useCallback(() => {
    setForm({ ...EMPTY_FORM });
    setRecommendation(null);
    setRecError(null);
    // Re-preload the unfiltered side panels so Reset returns to the
    // initial "latest market data" view rather than blank panels.
    sidePanelsPreloaded.current = false;
    setRackPrices([]);
    setRackError(null);
    setContracts([]);
    setContractsError(null);
    setRackLoading(true);
    listRackPrices({ size: SIDE_PANEL_PAGE_SIZE })
      .then((res) => setRackPrices(res.items))
      .catch((err) =>
        setRackError(
          err instanceof Error ? err.message : "Failed to load rack prices.",
        ),
      )
      .finally(() => setRackLoading(false));
    setContractsLoading(true);
    listSupplierContracts({ status: "active", size: SIDE_PANEL_PAGE_SIZE })
      .then((res) => setContracts(res.items))
      .catch((err) =>
        setContractsError(
          err instanceof Error
            ? err.message
            : "Failed to load supplier contracts.",
        ),
      )
      .finally(() => setContractsLoading(false));
  }, []);

  const candidateCount = (recommendation?.candidates ?? []).length;

  const summary = useMemo(() => {
    if (!recommendation) return null;
    const best = (recommendation.candidates ?? [])[0];
    return {
      candidates: candidateCount,
      waitWarnings: (recommendation.wait_warning_terminal_ids ?? []).length,
      bestPrice: best?.price_per_gallon_usd,
      bestTerminal: best?.terminal_id,
    };
  }, [recommendation, candidateCount]);

  const productCode = recommendation?.product_code ?? form.product_code.trim();
  const productLabel = productCode ? productName(productCode) : "";

  const actions = useMemo(
    () => (
      <Button
        type="button"
        size="sm"
        onClick={() => setDialogOpen(true)}
        icon={<Search className="h-3.5 w-3.5" aria-hidden="true" />}
      >
        Rank terminals
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  const querySummary = form.product_code ? (
    <span className="flex min-w-0 items-center gap-2 text-xs text-slate-700">
      <ProductChip code={form.product_code} variant="chip" />
      {form.volume_gallons && (
        <span className="whitespace-nowrap tabular-nums">
          {formatGallons(Number(form.volume_gallons))} gal
        </span>
      )}
      {form.origin_lat && form.origin_lon && (
        <span className="inline-flex items-center gap-1 whitespace-nowrap tabular-nums">
          <MapPin aria-hidden="true" className="h-3 w-3 text-slate-500" />
          {number(Number(form.origin_lat), { decimals: 4 })},{" "}
          {number(Number(form.origin_lon), { decimals: 4 })}
        </span>
      )}
    </span>
  ) : (
    <span className="text-xs text-text-muted">No ranking yet</span>
  );

  return (
    <div className="flex h-full flex-col bg-canvas">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />

      <Toolbar
        label="Sourcing"
        search={querySummary}
        filters={
          summary ? (
            <span className="flex items-center gap-3 whitespace-nowrap text-xs text-text-muted">
              <span>
                <b className="font-semibold text-text tabular-nums">
                  {summary.candidates}
                </b>{" "}
                candidates
              </span>
              <span
                className={
                  summary.waitWarnings > 0
                    ? "inline-flex items-center gap-1 font-semibold text-red-800"
                    : undefined
                }
              >
                {summary.waitWarnings > 0 && (
                  <AlertTriangle aria-hidden="true" className="h-3 w-3" />
                )}
                <b className="font-semibold tabular-nums">
                  {summary.waitWarnings}
                </b>{" "}
                wait warnings
              </span>
              <span>
                Best{" "}
                <b className="font-semibold text-text tabular-nums">
                  {formatUsd(summary.bestPrice)}
                </b>
                /gal
              </span>
              {summary.bestTerminal && (
                <span className="inline-flex items-center gap-1">
                  at <EntityLink type="terminal" id={summary.bestTerminal} />
                </span>
              )}
            </span>
          ) : undefined
        }
        end={
          <>
            {!embedded && actions}
            {recommendation && (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={handleRerank}
                disabled={loadingRec}
                aria-label="Re-rank terminals"
                icon={
                  <RefreshCw
                    className={`h-3.5 w-3.5 ${loadingRec ? "animate-spin" : ""}`}
                    aria-hidden="true"
                  />
                }
              >
                Re-rank
              </Button>
            )}
            {(recommendation || form.product_code) && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={handleReset}
              >
                Reset
              </Button>
            )}
          </>
        }
      />
      <QueryDialog
        open={dialogOpen}
        form={form}
        onClose={() => setDialogOpen(false)}
        onSubmit={runQuery}
        terminals={terminals}
      />

      <div className="flex-1 space-y-4 overflow-auto px-4 py-4">
        {recommendation && (
          <RecommendationBanner recommendation={recommendation} />
        )}

        {recError && !loadingRec && (
          <div className="flex items-start gap-2 p-3 rounded-lg bg-error-light border border-error-light text-sm text-error-dark">
            <AlertTriangle
              className="w-4 h-4 mt-0.5 flex-shrink-0"
              aria-hidden="true"
            />
            <div>
              <div className="font-medium">
                Could not load terminal recommendations.
              </div>
              <div className="text-xs mt-0.5">{recError}</div>
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <div className="xl:col-span-2 space-y-2">
            <h2 className="text-sm font-semibold text-primary">
              Ranked terminals
            </h2>
            {loadingRec ? (
              <div className="flex items-center justify-center py-12 text-sm text-gray-500 border border-dashed border-gray-200 rounded-lg">
                <Loader2
                  className="w-5 h-5 animate-spin mr-2"
                  aria-hidden="true"
                />
                Ranking terminals…
              </div>
            ) : recommendation ? (
              (recommendation.candidates ?? []).length === 0 ? (
                <div className="border border-dashed border-gray-200 rounded-lg px-6 py-12 text-center text-sm text-gray-500">
                  Every eligible terminal was disqualified for this query. Widen
                  the product, time-of-day, or branded filters and try again.
                </div>
              ) : (
                <div className="space-y-2">
                  {(recommendation.candidates ?? []).map((candidate, idx) => (
                    <CandidateRow
                      key={candidate.terminal_id}
                      candidate={candidate}
                      rank={idx}
                      isBest={idx === 0}
                    />
                  ))}
                </div>
              )
            ) : (
              <div className="border border-dashed border-gray-200 rounded-lg px-6 py-12 text-center text-sm text-gray-500">
                Choose Rank terminals to enter a product, volume, and origin and
                rank loading terminals.
              </div>
            )}
          </div>

          <div className="space-y-4">
            <RackPricesPanel
              prices={rackPrices}
              loading={rackLoading}
              error={rackError}
              productFilter={productLabel || "All products"}
            />
            <SupplierContractsPanel
              contracts={contracts}
              loading={contractsLoading}
              error={contractsError}
              productFilter={productLabel || "All products"}
            />
          </div>
        </div>

        {recommendation && (
          <div className="text-xs text-gray-500 pt-2">
            Recommendation id:{" "}
            <span className="font-mono">
              {recommendation.recommendation_id}
            </span>
            {" · "}
            Generated {formatTimestamp(recommendation.generated_at)}
          </div>
        )}
      </div>
    </div>
  );
}
