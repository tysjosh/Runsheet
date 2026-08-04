"use client";

/**
 * Storm_Mode road-restrictions admin panel.
 *
 * Surfaces the Capability 9 outputs from the Fuel Ops Hardening spec so
 * dispatchers and admins can:
 *
 *  * List the tenant's currently active road-closure polygons via
 *    ``GET /api/fuel/storm-mode/road-restrictions``. The backend caps
 *    the response size so runaway polygon counts don't blow up the UI
 *    (Task 10.8 / Req 9.3.5).
 *  * Upload a new :class:`StormRoadRestriction` via
 *    ``POST /api/fuel/storm-mode/road-restrictions``. Dispatchers and
 *    admins only — the component hides the upload form for other
 *    roles and the backend re-checks the role on submit (Req 9.3.3).
 *
 * Styling mirrors the peer admin surfaces under
 * :file:`components/admin/` (Tailwind utility classes, inline status
 * chips, toast system, ``bg-black/30`` modal overlays) so this panel
 * sits alongside :file:`DepotsPage.tsx` without visual drift.
 *
 * Validates: Requirements 9.3.3, 9.3.5.
 */

import {
  Check,
  Loader2,
  Map as MapIcon,
  Plus,
  RefreshCw,
  ShieldAlert,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ToastContainer, useToasts } from "@/components/ui";
import { hasAnyRole } from "../../config/modules";
import { ApiError } from "../../services/api";
import type {
  StormRoadRestriction,
  StormRoadRestrictionCreateRequest,
  WeatherAlertSeverity,
} from "../../services/fuelApi";
import {
  listStormRoadRestrictions,
  uploadStormRoadRestriction,
} from "../../services/fuelApi";

// ─── Role gate (mirrors StormModeBanner) ─────────────────────────────────────

/**
 * Roles permitted to upload Storm_Mode road restrictions.
 *
 * Mirrors ``require_role(tenant, "dispatcher", "admin")`` on
 * ``POST /api/fuel/storm-mode/road-restrictions``, which is an **exact** match.
 * The previous docstring referenced a ``_STORM_MODE_OVERRIDE_ROLES`` frozenset
 * that no longer exists, and the gate was a substring match — so the upload form
 * appeared for role names like ``ops-admin-eu`` that the backend then rejected
 * with 403.
 */
const UPLOAD_ROLES = ["dispatcher", "admin"] as const;

/**
 * Exact role match, case- and whitespace-insensitive.
 *
 * Shares {@link hasAnyRole} with `config/modules.ts` and
 * {@link canSubmitStormModeOverride} so all three agree on what holding a role
 * means.
 */
export function canUploadRoadRestriction(
  roles: readonly string[] | null | undefined,
): boolean {
  return hasAnyRole(roles, UPLOAD_ROLES);
}

// ─── Constants ───────────────────────────────────────────────────────────────

const SEVERITIES: { value: WeatherAlertSeverity; label: string }[] = [
  { value: "minor", label: "Minor" },
  { value: "moderate", label: "Moderate" },
  { value: "severe", label: "Severe" },
  { value: "extreme", label: "Extreme" },
];

export const SEVERITY_BADGE_CONFIG: Record<
  WeatherAlertSeverity,
  { color: string; bg: string; border: string }
> = {
  minor: {
    color: "text-warning-dark",
    bg: "bg-warning-light",
    border: "border-warning-light",
  },
  moderate: {
    color: "text-warning-dark",
    bg: "bg-warning-light",
    border: "border-warning-light",
  },
  severe: {
    color: "text-error-dark",
    bg: "bg-error-light",
    border: "border-error-light",
  },
  extreme: {
    color: "text-error-dark",
    bg: "bg-error-light",
    border: "border-error",
  },
};

const DEFAULT_UPLOAD_SOURCE = "dispatcher";

// ─── GeoJSON parsing ─────────────────────────────────────────────────────────

export interface ParsedGeoJson {
  type: "Polygon" | "MultiPolygon";
  coordinates: unknown;
}

/**
 * Parse a user-supplied GeoJSON string into a plain object and verify
 * the ``type`` is a supported polygon shape. Returns ``{ ok: true,
 * value }`` on success or ``{ ok: false, error }`` with a human-
 * readable message otherwise. Exported so unit tests can pin the
 * exact validation contract without re-rendering the UI.
 */
export function parseGeoJsonPolygon(
  raw: string,
): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { ok: false, error: "GeoJSON polygon is required." };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (err) {
    return {
      ok: false,
      error: `GeoJSON must be valid JSON (${err instanceof Error ? err.message : "parse error"}).`,
    };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {
      ok: false,
      error: "GeoJSON must be an object with a 'type' and 'coordinates'.",
    };
  }
  const obj = parsed as Record<string, unknown>;
  if (obj.type !== "Polygon" && obj.type !== "MultiPolygon") {
    return {
      ok: false,
      error: "GeoJSON type must be 'Polygon' or 'MultiPolygon'.",
    };
  }
  if (!Array.isArray(obj.coordinates)) {
    return {
      ok: false,
      error: "GeoJSON coordinates must be an array.",
    };
  }
  return { ok: true, value: obj };
}

// ─── Formatters ──────────────────────────────────────────────────────────────

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
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

/** Truncated, pretty-printed preview of a GeoJSON polygon. */
export function previewGeoJson(
  polygon: Record<string, unknown>,
  maxChars = 240,
): string {
  let text: string;
  try {
    text = JSON.stringify(polygon);
  } catch {
    text = "<unserializable geometry>";
  }
  if (text.length <= maxChars) return text;
  return `${text.slice(0, maxChars)}…`;
}

// ─── Severity Badge ──────────────────────────────────────────────────────────

function SeverityBadge({ severity }: { severity: WeatherAlertSeverity }) {
  const config = SEVERITY_BADGE_CONFIG[severity] ?? SEVERITY_BADGE_CONFIG.minor;
  return (
    <span
      className={`inline-flex items-center text-[10px] uppercase tracking-wide px-2 py-0.5 rounded font-medium border ${config.bg} ${config.color} ${config.border}`}
      data-testid={`road-restriction-severity-${severity}`}
    >
      {severity}
    </span>
  );
}

// ─── Restriction Card ────────────────────────────────────────────────────────

interface RestrictionCardProps {
  restriction: StormRoadRestriction;
}

function RestrictionCard({ restriction }: RestrictionCardProps) {
  const isActive = useMemo(() => {
    // A restriction is considered active when its effective window
    // includes "now" — the list endpoint already filters for this, but
    // we render an explicit chip so operators see the state at a glance.
    if (!restriction.effective_to) return true;
    try {
      return new Date(restriction.effective_to).getTime() > Date.now();
    } catch {
      return true;
    }
  }, [restriction.effective_to]);

  return (
    <article
      data-testid={`road-restriction-card-${restriction.restriction_id}`}
      className="border border-gray-200 rounded-lg p-4 bg-white space-y-3"
    >
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-primary truncate">
              {restriction.reason?.trim() || "Untitled restriction"}
            </h3>
            <SeverityBadge severity={restriction.severity} />
            {isActive ? (
              <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium bg-success-light text-success-dark">
                <Check className="w-3 h-3" aria-hidden="true" />
                Active
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium bg-gray-100 text-gray-600">
                Expired
              </span>
            )}
          </div>
          <p className="text-xs text-gray-500 mt-1 font-mono truncate">
            {restriction.restriction_id}
          </p>
        </div>
        <span className="text-[10px] uppercase tracking-wide text-gray-500">
          {restriction.source}
        </span>
      </header>

      <dl className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
        <div>
          <dt className="text-[10px] uppercase text-gray-500">
            Effective from
          </dt>
          <dd className="text-gray-700">
            {formatTimestamp(restriction.effective_from)}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase text-gray-500">Effective to</dt>
          <dd className="text-gray-700">
            {formatTimestamp(restriction.effective_to)}
          </dd>
        </div>
      </dl>

      <div>
        <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
          GeoJSON preview
        </div>
        <pre
          className="text-[11px] bg-gray-50 border border-gray-100 rounded p-2 font-mono text-gray-700 whitespace-pre-wrap break-all"
          data-testid={`road-restriction-preview-${restriction.restriction_id}`}
        >
          {previewGeoJson(restriction.polygon)}
        </pre>
      </div>
    </article>
  );
}

// ─── Upload Form ─────────────────────────────────────────────────────────────

interface UploadFormValues {
  name: string;
  severity: WeatherAlertSeverity;
  active: boolean;
  polygon: string;
}

const EMPTY_UPLOAD_FORM: UploadFormValues = {
  name: "",
  severity: "severe",
  active: true,
  polygon: "",
};

interface UploadFormProps {
  onSuccess: (restriction: StormRoadRestriction) => void;
  onError: (message: string) => void;
}

function UploadForm({ onSuccess, onError }: UploadFormProps) {
  const [form, setForm] = useState<UploadFormValues>(EMPTY_UPLOAD_FORM);
  const [polygonError, setPolygonError] = useState<string | null>(null);
  const [nameError, setNameError] = useState<string | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";
  const errorInputClass =
    "w-full px-3 py-2 text-sm border border-error rounded-lg focus:ring-2 focus:ring-error-light focus:border-error bg-white";

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setApiError(null);
    setNameError(null);
    setPolygonError(null);

    const name = form.name.trim();
    if (!name) {
      setNameError("Name is required.");
      return;
    }

    const parsed = parseGeoJsonPolygon(form.polygon);
    if (!parsed.ok) {
      setPolygonError(parsed.error);
      return;
    }

    const now = new Date().toISOString();
    const body: StormRoadRestrictionCreateRequest = {
      polygon: parsed.value,
      effective_from: now,
      effective_to: form.active ? null : now,
      source: DEFAULT_UPLOAD_SOURCE,
      severity: form.severity,
      reason: name,
    };

    setSubmitting(true);
    try {
      const created = await uploadStormRoadRestriction(body);
      setForm(EMPTY_UPLOAD_FORM);
      onSuccess(created);
    } catch (err) {
      let message: string;
      if (err instanceof ApiError) {
        message = err.message || `Request failed (HTTP ${err.status}).`;
      } else {
        message =
          err instanceof Error
            ? err.message
            : "Failed to upload road restriction.";
      }
      setApiError(message);
      onError(message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white border border-gray-200 rounded-lg p-4 space-y-3"
      data-testid="road-restriction-upload-form"
      aria-label="Upload road restriction"
    >
      <div className="flex items-center gap-2">
        <Upload className="w-4 h-4 text-gray-500" aria-hidden="true" />
        <h2 className="text-sm font-semibold text-primary">
          Upload a road restriction
        </h2>
      </div>

      {apiError && (
        <p
          role="alert"
          className="text-sm text-error bg-error-light px-3 py-2 rounded-lg"
          data-testid="road-restriction-api-error"
        >
          {apiError}
        </p>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="sm:col-span-2">
          <label
            htmlFor="rr-name"
            className="block text-xs font-medium text-gray-600 mb-1"
          >
            Name
          </label>
          <input
            id="rr-name"
            type="text"
            className={nameError ? errorInputClass : inputClass}
            value={form.name}
            onChange={(e) => {
              setForm((prev) => ({ ...prev, name: e.target.value }));
              if (nameError) setNameError(null);
            }}
            placeholder="e.g. Broad St bridge closure"
            required
          />
          {nameError && <p className="text-xs text-error mt-1">{nameError}</p>}
        </div>

        <div>
          <label
            htmlFor="rr-severity"
            className="block text-xs font-medium text-gray-600 mb-1"
          >
            Severity
          </label>
          <select
            id="rr-severity"
            className={inputClass}
            value={form.severity}
            onChange={(e) =>
              setForm((prev) => ({
                ...prev,
                severity: e.target.value as WeatherAlertSeverity,
              }))
            }
          >
            {SEVERITIES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <input
          id="rr-active"
          type="checkbox"
          checked={form.active}
          onChange={(e) =>
            setForm((prev) => ({ ...prev, active: e.target.checked }))
          }
          className="w-4 h-4 rounded border-gray-300"
        />
        <label htmlFor="rr-active" className="text-xs text-gray-700">
          Active (no effective_to set)
        </label>
      </div>

      <div>
        <label
          htmlFor="rr-polygon"
          className="block text-xs font-medium text-gray-600 mb-1"
        >
          GeoJSON polygon
        </label>
        <textarea
          id="rr-polygon"
          rows={6}
          className={`${polygonError ? errorInputClass : inputClass} font-mono text-[11px]`}
          value={form.polygon}
          onChange={(e) => {
            setForm((prev) => ({ ...prev, polygon: e.target.value }));
            if (polygonError) setPolygonError(null);
          }}
          placeholder={'{"type":"Polygon","coordinates":[[[...],[...],...]]}'}
          data-testid="road-restriction-polygon-input"
          required
        />
        {polygonError && (
          <p
            className="text-xs text-error mt-1"
            data-testid="road-restriction-polygon-error"
          >
            {polygonError}
          </p>
        )}
        <p className="text-[10px] text-gray-500 mt-1">
          Paste a GeoJSON Polygon or MultiPolygon with WGS84 [lon, lat]
          coordinates. The backend validates the geometry before persisting.
        </p>
      </div>

      <div className="flex items-center justify-end">
        <button
          type="submit"
          disabled={submitting}
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50"
        >
          {submitting ? (
            <>
              <Loader2
                className="w-3.5 h-3.5 animate-spin"
                aria-hidden="true"
              />
              Uploading...
            </>
          ) : (
            <>
              <Plus className="w-3.5 h-3.5" aria-hidden="true" />
              Upload restriction
            </>
          )}
        </button>
      </div>
    </form>
  );
}

// ─── Main Panel ──────────────────────────────────────────────────────────────

export interface RoadRestrictionsPanelProps {
  /**
   * Caller's role list for the UI-side gate (Req 9.3.3). The backend
   * re-checks the JWT context so a caller with no roles will still get
   * HTTP 403 on upload; the prop exists so non-permitted operators
   * don't see an upload form that would only error on submit.
   */
  roles?: readonly string[] | null;
}

/**
 * Admin panel for Storm_Mode road restrictions. Lists active polygons
 * and lets dispatchers / admins upload new ones. Mounts as a dashboard
 * case in :file:`app/dashboard/page.tsx`.
 */
export default function RoadRestrictionsPanel({
  roles,
}: RoadRestrictionsPanelProps = {}) {
  const { toasts, addToast, dismissToast } = useToasts();
  const [items, setItems] = useState<StormRoadRestriction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const canUpload = useMemo(() => canUploadRoadRestriction(roles), [roles]);

  const fetchRestrictions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await listStormRoadRestrictions();
      setItems(resp.items);
    } catch (err) {
      setItems([]);
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load road restrictions.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchRestrictions();
  }, [fetchRestrictions]);

  const handleUploadSuccess = useCallback(
    (restriction: StormRoadRestriction) => {
      addToast(
        `Road restriction "${restriction.reason ?? restriction.restriction_id}" uploaded.`,
        "success",
      );
      void fetchRestrictions();
    },
    [addToast, fetchRestrictions],
  );

  const handleUploadError = useCallback(
    (message: string) => {
      addToast(message, "error");
    },
    [addToast],
  );

  return (
    <div className="flex-1 flex flex-col p-6 bg-gray-50 overflow-auto">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
      <div className="max-w-5xl w-full mx-auto space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-primary mb-1 flex items-center gap-2">
              <MapIcon className="w-5 h-5" aria-hidden="true" />
              Road restrictions
            </h1>
            <p className="text-sm text-gray-500">
              Dispatcher-authored road-closure polygons surfaced on the
              Storm_Mode map overlay and respected by the route solver.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void fetchRestrictions()}
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 border border-gray-200 disabled:opacity-50"
            aria-label="Refresh road restrictions"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
              aria-hidden="true"
            />
            Refresh
          </button>
        </div>

        {!canUpload && (
          <div
            className="flex items-start gap-2 p-3 rounded-lg bg-gray-50 border border-gray-200 text-xs text-gray-600"
            data-testid="road-restriction-role-gate-notice"
          >
            <ShieldAlert
              className="w-4 h-4 text-gray-500 mt-0.5 flex-shrink-0"
              aria-hidden="true"
            />
            <span>
              Uploading road restrictions requires a dispatcher or admin role.
              Ask a dispatcher to file the polygon on your behalf.
            </span>
          </div>
        )}

        {canUpload && (
          <UploadForm
            onSuccess={handleUploadSuccess}
            onError={handleUploadError}
          />
        )}

        <div className="bg-white border border-gray-200 rounded-xl p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-primary">
              Active restrictions
            </h2>
            <span className="text-xs text-gray-500">
              {loading ? "Loading…" : `${items.length} total`}
            </span>
          </div>

          {error && (
            <p
              role="alert"
              className="text-sm text-error bg-error-light px-3 py-2 rounded-lg mb-3"
            >
              {error}
            </p>
          )}

          {loading ? (
            <div className="flex items-center justify-center py-10 text-sm text-gray-500">
              <Loader2
                className="w-4 h-4 animate-spin mr-2"
                aria-hidden="true"
              />
              Loading road restrictions…
            </div>
          ) : items.length === 0 && !error ? (
            <div className="text-center py-10 border border-dashed border-gray-200 rounded-lg text-sm text-gray-500">
              No road restrictions configured for this tenant.
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {items.map((r) => (
                <RestrictionCard key={r.restriction_id} restriction={r} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
