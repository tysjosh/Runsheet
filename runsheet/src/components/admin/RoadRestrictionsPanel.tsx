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
  RefreshCw,
  ShieldAlert,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Field,
  FormDialog,
  IconButton,
  INPUT_CLASS,
  Select,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { hasAnyRole } from "../../config/modules";
import { dateTime, number } from "../../lib/format";
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
import { PageTitle } from "../ui/PageHeader";
import { notify } from "../ui/toast/notify";

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
  return dateTime(iso);
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

// ─── Upload dialog ───────────────────────────────────────────────────────────

type UploadFormValues = {
  name: string;
  severity: WeatherAlertSeverity;
  active: boolean;
  polygon: string;
};

const EMPTY_UPLOAD_FORM: UploadFormValues = {
  name: "",
  severity: "severe",
  active: true,
  polygon: "",
};

/**
 * The restriction's attributes and polygon (task 3.8, design.md §5: md
 * FormDialog; the list stays the panel).
 */
function UploadDialog({
  onClose,
  onSuccess,
}: {
  onClose: () => void;
  onSuccess: (restriction: StormRoadRestriction) => void;
}) {
  const submit = async (v: UploadFormValues) => {
    const parsed = parseGeoJsonPolygon(v.polygon);
    if (!parsed.ok) throw new Error(parsed.error);
    const now = new Date().toISOString();
    const body: StormRoadRestrictionCreateRequest = {
      polygon: parsed.value,
      effective_from: now,
      effective_to: v.active ? null : now,
      source: DEFAULT_UPLOAD_SOURCE,
      severity: v.severity,
      reason: v.name.trim(),
    };
    try {
      return await uploadStormRoadRestriction(body);
    } catch (err) {
      if (err instanceof ApiError)
        throw new Error(err.message || `Request failed (HTTP ${err.status}).`);
      throw err;
    }
  };
  return (
    <FormDialog<UploadFormValues, StormRoadRestriction>
      open
      size="md"
      title="Upload road restriction"
      help="A road-closure polygon shown on the Storm mode map and respected by the route solver."
      submitLabel="Upload restriction"
      successMessage={null}
      initialValues={EMPTY_UPLOAD_FORM}
      validate={(v) => {
        const parsed = parseGeoJsonPolygon(v.polygon);
        return {
          name: v.name.trim() ? undefined : "Name is required.",
          polygon: parsed.ok ? undefined : parsed.error,
        };
      }}
      onSubmit={submit}
      onSaved={onSuccess}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Name" required span={1} error={errors.name}>
            <input
              id="rr-name"
              type="text"
              className={INPUT_CLASS}
              value={values.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="e.g. Broad St bridge closure"
            />
          </Field>
          <Field label="Severity" span={1}>
            <Select
              id="rr-severity"
              value={values.severity}
              onChange={(v) => set("severity", v as WeatherAlertSeverity)}
              options={SEVERITIES}
            />
          </Field>
          <label className="col-span-2 flex items-center gap-2 text-sm text-text">
            <input
              id="rr-active"
              type="checkbox"
              checked={values.active}
              onChange={(e) => set("active", e.target.checked)}
            />
            Active now (no end time)
          </label>
          <Field
            label="GeoJSON polygon"
            required
            help="A Polygon or MultiPolygon with WGS84 [lon, lat] coordinates. The server checks the geometry before saving."
            error={errors.polygon}
          >
            <textarea
              id="rr-polygon"
              rows={6}
              className={`${INPUT_CLASS} h-auto py-1.5 font-mono text-[11px]`}
              value={values.polygon}
              onChange={(e) => set("polygon", e.target.value)}
              placeholder={
                '{"type":"Polygon","coordinates":[[[...],[...],...]]}'
              }
              data-testid="road-restriction-polygon-input"
            />
          </Field>
        </>
      )}
    </FormDialog>
  );
}

// ─── Main Panel ──────────────────────────────────────────────────────────────

export interface RoadRestrictionsPanelProps {
  /**
   * Caller's role list for the UI-side gate (Req 9.3.3). The backend
   * re-checks the JWT context so a caller with no roles still gets HTTP 403
   * on upload; the prop exists so non-permitted operators don't see an
   * upload action that would only error on submit.
   */
  roles?: readonly string[] | null;
}

/**
 * Settings → Company → Road restrictions: Storm mode road-closure polygons.
 * Lists them and lets dispatchers / admins upload new ones.
 */
export default function RoadRestrictionsPanel({
  roles,
}: RoadRestrictionsPanelProps = {}) {
  const [items, setItems] = useState<StormRoadRestriction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

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

  const actions = useMemo(
    () =>
      canUpload ? (
        <Button
          size="sm"
          icon={<Upload className="h-3.5 w-3.5" />}
          onClick={() => setUploading(true)}
          data-testid="road-restriction-upload-button"
        >
          Upload restriction
        </Button>
      ) : null,
    [canUpload],
  );
  const embedded = usePageChrome({ actions });

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center gap-2 border-b border-slate-200 px-4">
          <MapIcon aria-hidden="true" className="h-4 w-4 text-slate-600" />
          <PageTitle className="text-base font-semibold text-text">
            Road restrictions
          </PageTitle>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Road restrictions"
        filters={
          <span className="text-xs text-text-muted">
            {loading ? "Loading…" : `${number(items.length)} active`}
          </span>
        }
        end={
          <IconButton
            label="Refresh road restrictions"
            size="sm"
            onClick={() => void fetchRestrictions()}
            icon={
              <RefreshCw
                className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
              />
            }
          />
        }
      />
      <div className="min-h-0 flex-1 space-y-3 overflow-auto p-4">
        {!canUpload && (
          <div
            className="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700"
            data-testid="road-restriction-role-gate-notice"
          >
            <ShieldAlert
              className="mt-0.5 h-4 w-4 flex-shrink-0 text-slate-500"
              aria-hidden="true"
            />
            <span>
              Uploading road restrictions needs a dispatcher or admin role. Ask
              a dispatcher to file the polygon for you.
            </span>
          </div>
        )}
        {error && (
          <p
            role="alert"
            className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800"
          >
            {error}
          </p>
        )}
        {loading ? (
          <div className="flex items-center justify-center py-10 text-sm text-text-muted">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
            Loading road restrictions…
          </div>
        ) : items.length === 0 && !error ? (
          <div className="rounded-lg border border-dashed border-slate-200 py-10 text-center text-sm text-text-muted">
            No road restrictions configured for this tenant.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {items.map((r) => (
              <RestrictionCard key={r.restriction_id} restriction={r} />
            ))}
          </div>
        )}
      </div>

      {uploading && (
        <UploadDialog
          onClose={() => setUploading(false)}
          onSuccess={(restriction) => {
            notify({
              type: "success",
              message: `Road restriction "${restriction.reason ?? restriction.restriction_id}" uploaded.`,
            });
            void fetchRestrictions();
          }}
        />
      )}
    </div>
  );
}
