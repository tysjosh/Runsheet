"use client";

/**
 * Weather Alerts / Storm_Mode detail page (``/admin/weather-alerts``).
 *
 * This is the "Full details" destination linked from
 * :file:`components/ops/StormModeBanner.tsx`. The banner is a condensed,
 * always-on advisory pinned to operations pages; this page is the full
 * surface where dispatchers and admins can:
 *
 *   * Read the current Storm_Mode posture (effective + computed state,
 *     activation window, active override) from
 *     ``GET /api/fuel/storm-mode/status`` (Req 9.1.6, 9.4.3).
 *   * Inspect every triggering :class:`WeatherAlert` in a sortable
 *     table — severity, source, window, and affected ZIP footprint.
 *   * Submit a Storm_Mode override (activate / deactivate / snooze /
 *     clear) through ``POST /api/fuel/storm-mode/override`` with a
 *     mandatory audit reason (Req 9.4.2, 9.4.4). The form is gated to
 *     dispatcher / admin roles; the backend re-checks the JWT.
 *   * Manage road-closure polygons via the embedded
 *     :class:`RoadRestrictionsPanel` (Req 9.3.3, 9.3.5).
 *
 * HTTP 503 ``storm_mode_evaluator_unavailable`` is treated as "not
 * configured yet" rather than a hard error — the page renders an
 * informational empty state so early-bootstrap environments don't show
 * a red failure screen.
 */

import {
  AlertTriangle,
  CloudLightning,
  Loader2,
  RefreshCw,
  ShieldAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import RoadRestrictionsPanel from "../../../components/admin/RoadRestrictionsPanel";
import { canSubmitStormModeOverride } from "../../../components/ops/StormModeBanner";
import { Badge, type Column, Table } from "../../../components/ui";
import { ApiError } from "../../../services/api";
import {
  getStormModeStatus,
  type StormModeOverrideAction,
  type StormModeStatusResponse,
  type StormModeTriggeringAlert,
  submitStormModeOverride,
  type WeatherAlertSeverity,
} from "../../../services/fuelApi";
import { getCurrentUserRoles } from "../../../utils/auth";

// ─── Helpers ─────────────────────────────────────────────────────────────────

const SEVERITY_BADGE_VARIANT: Record<
  WeatherAlertSeverity,
  "warning" | "error"
> = {
  minor: "warning",
  moderate: "warning",
  severe: "error",
  extreme: "error",
};

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function humanizeAlertType(alertType: string): string {
  return alertType.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ─── Triggering-alert table columns ──────────────────────────────────────────

const alertColumns: Column<StormModeTriggeringAlert>[] = [
  {
    key: "alert_type",
    label: "Alert",
    className: "text-sm font-medium text-gray-900",
    render: (alert) => (
      <div className="min-w-0">
        <div className="font-medium text-gray-900">
          {humanizeAlertType(alert.alert_type)}
        </div>
        {alert.headline ? (
          <div className="text-xs text-gray-500 truncate max-w-[280px]">
            {alert.headline}
          </div>
        ) : null}
      </div>
    ),
  },
  {
    key: "severity",
    label: "Severity",
    render: (alert) => (
      <Badge variant={SEVERITY_BADGE_VARIANT[alert.severity] ?? "warning"}>
        {alert.severity}
      </Badge>
    ),
  },
  {
    key: "activation_status",
    label: "Status",
    className: "text-sm text-gray-700 capitalize",
    render: (alert) => alert.activation_status,
  },
  {
    key: "source",
    label: "Source",
    className: "text-sm text-gray-700",
    render: (alert) => alert.source.toUpperCase(),
  },
  {
    key: "expected_start_at",
    label: "Window",
    className: "text-sm text-gray-700",
    render: (alert) =>
      `${formatDateTime(alert.expected_start_at)} → ${formatDateTime(
        alert.expected_end_at,
      )}`,
  },
  {
    key: "affected_zip_codes",
    label: "ZIPs",
    align: "right",
    className: "text-sm text-gray-700",
    render: (alert) => alert.affected_zip_codes.length,
  },
];

// ─── Override Form ───────────────────────────────────────────────────────────

interface OverrideFormProps {
  actorId: string;
  onSubmitted: () => void;
  onClose: () => void;
}

function OverrideForm({ actorId, onSubmitted, onClose }: OverrideFormProps) {
  const [action, setAction] = useState<StormModeOverrideAction>("deactivate");
  const [reason, setReason] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setError(null);
      const trimmedReason = reason.trim();
      if (!trimmedReason) {
        setError("Reason is required for every Storm_Mode override.");
        return;
      }
      setSubmitting(true);
      try {
        await submitStormModeOverride({
          action,
          reason: trimmedReason,
          actor_id: actorId,
          expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        });
        onSubmitted();
      } catch (err) {
        if (err instanceof ApiError) {
          if (err.status === 403) {
            setError(
              "You don't have permission to submit Storm_Mode overrides. Ask a dispatcher or admin.",
            );
          } else if (err.status === 422) {
            setError(`Rejected by backend: ${err.message}`);
          } else {
            setError(err.message);
          }
        } else {
          setError(
            err instanceof Error
              ? err.message
              : "Unknown error submitting override",
          );
        }
      } finally {
        setSubmitting(false);
      }
    },
    [action, actorId, expiresAt, onSubmitted, reason],
  );

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white focus:ring-2 focus:ring-gray-200 focus:border-gray-300 text-gray-900";

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white border border-gray-200 rounded-xl p-4 space-y-3"
      aria-label="Submit Storm_Mode override"
    >
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-primary">
          Submit Storm_Mode override
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="p-1 rounded hover:bg-gray-100 text-gray-500"
          aria-label="Close override form"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <label className="block">
          <span className="text-xs font-medium text-gray-700">Action</span>
          <select
            value={action}
            onChange={(e) =>
              setAction(e.target.value as StormModeOverrideAction)
            }
            className={inputClass}
            disabled={submitting}
          >
            <option value="activate">Activate (force on)</option>
            <option value="deactivate">Deactivate (force off)</option>
            <option value="snooze">Snooze (suppress until expiry)</option>
            <option value="clear">Clear (remove prior override)</option>
          </select>
        </label>

        <label className="block">
          <span className="text-xs font-medium text-gray-700">
            Expires at (optional)
          </span>
          <input
            type="datetime-local"
            value={expiresAt}
            onChange={(e) => setExpiresAt(e.target.value)}
            className={inputClass}
            disabled={submitting}
          />
        </label>
      </div>

      <label className="block">
        <span className="text-xs font-medium text-gray-700">
          Reason <span className="text-error">*</span>
        </span>
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          rows={2}
          className={inputClass}
          placeholder="Explain why this override is being applied (captured for audit)"
          disabled={submitting}
          required
          aria-required="true"
        />
      </label>

      {error && (
        <p
          className="text-xs text-error-dark bg-error-light border border-error-light rounded px-2 py-1"
          role="alert"
        >
          {error}
        </p>
      )}

      <div className="flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-100 rounded-lg"
          disabled={submitting}
        >
          Cancel
        </button>
        <button
          type="submit"
          className="inline-flex items-center gap-2 px-3 py-1.5 text-sm font-medium bg-primary text-white rounded-lg hover:bg-primary-hover disabled:opacity-50"
          disabled={submitting}
        >
          {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
          Submit override
        </button>
      </div>
    </form>
  );
}

// ─── Status Summary ──────────────────────────────────────────────────────────

function StatusCard({ status }: { status: StormModeStatusResponse }) {
  const isActive = status.state === "active";
  return (
    <div
      className={`rounded-xl border p-5 ${
        isActive
          ? "bg-error-light border-error-light"
          : "bg-white border-gray-200"
      }`}
      data-testid="storm-mode-status-card"
    >
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <div
            className={`w-10 h-10 rounded-lg flex items-center justify-center ${
              isActive ? "bg-error" : "bg-gray-200"
            }`}
          >
            <CloudLightning
              className={`w-5 h-5 ${isActive ? "text-white" : "text-gray-500"}`}
              aria-hidden="true"
            />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2
                className={`text-lg font-semibold ${
                  isActive ? "text-error-dark" : "text-primary"
                }`}
              >
                Storm_Mode {isActive ? "active" : "inactive"}
              </h2>
              <Badge variant={isActive ? "error" : "default"}>
                {status.state}
              </Badge>
              {status.override_active && (
                <span className="inline-flex items-center gap-1 text-[11px] text-brand-secondary bg-brand-secondary-soft border border-brand-secondary-soft rounded px-2 py-0.5">
                  <ShieldAlert className="w-3 h-3" aria-hidden="true" />
                  Override active
                </span>
              )}
            </div>
            <p className="text-xs text-gray-500 mt-0.5">
              Computed state: {status.computed_state} · Updated{" "}
              {formatDateTime(status.updated_at)}
            </p>
          </div>
        </div>
      </div>

      <dl className="mt-4 grid grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-2 text-sm">
        <div>
          <dt className="text-[10px] uppercase tracking-wide text-gray-500">
            Activated
          </dt>
          <dd className="text-gray-800">
            {formatDateTime(status.activation_window.activated_at)}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wide text-gray-500">
            Expected clear
          </dt>
          <dd className="text-gray-800">
            {formatDateTime(status.activation_window.clears_at)}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wide text-gray-500">
            Threshold
          </dt>
          <dd className="text-gray-800 capitalize">
            {status.activation_window.severity_threshold}
          </dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wide text-gray-500">
            Lookahead
          </dt>
          <dd className="text-gray-800">
            {status.activation_window.lookahead_hours}h
          </dd>
        </div>
      </dl>

      {status.override_active && status.override && (
        <div className="mt-4 rounded-lg bg-white/70 border border-brand-secondary-soft p-3 text-xs text-gray-700">
          <span className="font-medium capitalize">
            {status.override.action}
          </span>{" "}
          override by {status.override.actor_id}
          {status.override.expires_at
            ? ` · expires ${formatDateTime(status.override.expires_at)}`
            : ""}
          {status.override.reason ? (
            <p className="mt-1 text-gray-500">“{status.override.reason}”</p>
          ) : null}
        </div>
      )}
    </div>
  );
}

// ─── Page ────────────────────────────────────────────────────────────────────

export default function WeatherAlertsPage() {
  const [status, setStatus] = useState<StormModeStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [roles, setRoles] = useState<string[]>([]);
  const [actorId, setActorId] = useState("dispatcher");
  const [showOverrideForm, setShowOverrideForm] = useState(false);
  const cancelledRef = useRef(false);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getStormModeStatus();
      if (cancelledRef.current) return;
      setStatus(next);
      setUnavailable(false);
    } catch (err) {
      if (cancelledRef.current) return;
      if (err instanceof ApiError && err.status === 503) {
        // Evaluator not wired yet — informational, not an error screen.
        setStatus(null);
        setUnavailable(true);
        setError(null);
      } else {
        setError(
          err instanceof Error
            ? err.message
            : "Failed to load Storm_Mode status.",
        );
      }
    } finally {
      if (!cancelledRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    void reload();
    void getCurrentUserRoles().then((r) => {
      if (cancelledRef.current) return;
      setRoles(r);
      if (r.length > 0) setActorId(r[0]);
    });
    return () => {
      cancelledRef.current = true;
    };
  }, [reload]);

  const canOverride = useMemo(() => canSubmitStormModeOverride(roles), [roles]);

  const triggeringAlerts = status?.triggering_alerts ?? [];

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-6xl mx-auto px-6 py-6 space-y-6">
        {/* Header */}
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl flex items-center justify-center bg-primary">
                <CloudLightning
                  className="w-5 h-5 text-white"
                  aria-hidden="true"
                />
              </div>
              <div>
                <h1 className="text-xl font-semibold text-primary">
                  Weather Alerts &amp; Storm_Mode
                </h1>
                <p className="text-sm text-gray-500 mt-0.5">
                  Severe-weather posture, triggering alerts, manual overrides,
                  and road-closure polygons for this tenant.
                </p>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {canOverride && !showOverrideForm && (
              <button
                type="button"
                onClick={() => setShowOverrideForm(true)}
                className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-white rounded-lg bg-primary hover:bg-primary-hover"
              >
                <ShieldAlert className="w-3.5 h-3.5" aria-hidden="true" />
                Override
              </button>
            )}
            <button
              type="button"
              onClick={() => void reload()}
              disabled={loading}
              className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 border border-gray-200 disabled:opacity-50"
              aria-label="Refresh Storm_Mode status"
            >
              <RefreshCw
                className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
                aria-hidden="true"
              />
              Refresh
            </button>
          </div>
        </div>

        {/* Load error */}
        {error && !loading && (
          <div
            role="alert"
            className="flex items-start gap-3 rounded-xl border border-error-light bg-error-light px-4 py-3"
          >
            <AlertTriangle
              className="h-4 w-4 text-error-dark mt-0.5 shrink-0"
              aria-hidden="true"
            />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-error-dark">
                Could not load Storm_Mode status
              </p>
              <p className="mt-1 text-xs text-error-dark" title={error}>
                {error}
              </p>
            </div>
            <button
              type="button"
              onClick={() => void reload()}
              className="shrink-0 px-3 py-1.5 text-xs font-medium text-white rounded-lg bg-primary hover:bg-primary-hover"
            >
              Retry
            </button>
          </div>
        )}

        {/* Override form */}
        {canOverride && showOverrideForm && (
          <OverrideForm
            actorId={actorId}
            onSubmitted={() => {
              setShowOverrideForm(false);
              void reload();
            }}
            onClose={() => setShowOverrideForm(false)}
          />
        )}

        {/* Loading */}
        {loading && !status && (
          <div className="flex items-center justify-center py-12">
            <Loader2
              className="w-6 h-6 text-gray-400 animate-spin"
              aria-hidden="true"
            />
          </div>
        )}

        {/* Evaluator not configured */}
        {unavailable && !loading && (
          <div className="rounded-xl border border-dashed border-gray-300 bg-white px-6 py-10 text-center">
            <CloudLightning
              className="w-8 h-8 text-gray-300 mx-auto mb-3"
              aria-hidden="true"
            />
            <p className="text-sm font-medium text-gray-700">
              Storm_Mode evaluator is not configured for this environment.
            </p>
            <p className="text-xs text-gray-500 mt-1">
              Weather-alert ingestion and posture evaluation come online once
              the evaluator is wired by bootstrap. Road restrictions below can
              still be managed.
            </p>
          </div>
        )}

        {/* Status + triggering alerts */}
        {status && !loading && (
          <>
            <StatusCard status={status} />

            <div className="bg-white rounded-xl border border-gray-200 p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-sm font-semibold text-primary">
                  Triggering weather alerts
                </h2>
                <span className="text-xs text-gray-500">
                  {triggeringAlerts.length} alert
                  {triggeringAlerts.length === 1 ? "" : "s"}
                </span>
              </div>
              <Table<StormModeTriggeringAlert>
                ariaLabel="Triggering weather alerts"
                columns={alertColumns}
                data={triggeringAlerts}
                getRowId={(alert) => alert.alert_id}
                variant="compact"
                emptyState={
                  <span className="text-gray-500">
                    No weather alerts are currently driving Storm_Mode.
                  </span>
                }
              />
            </div>
          </>
        )}

        {/* Road restrictions (self-contained data loading) */}
        <RoadRestrictionsPanel roles={roles} />
      </div>
    </div>
  );
}
