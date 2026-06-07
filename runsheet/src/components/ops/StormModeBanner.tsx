"use client";

/**
 * Storm_Mode banner pinned to the top of operations control pages.
 *
 * The banner polls ``GET /api/fuel/storm-mode/status`` every 60 seconds
 * and renders a visible posture-change advisory whenever the backend
 * reports ``state === "active"`` (Req 9.4.1). It surfaces:
 *
 *   * The triggering :class:`WeatherAlert` (headline, severity, source,
 *     expected window, affected ZIP footprint).
 *   * The activation window (``activated_at`` / ``clears_at``,
 *     ``severity_threshold``, ``lookahead_hours``) so operators see
 *     *why* the platform flipped state.
 *   * A "full details" link to the /admin/integrations → weather page
 *     (configurable via ``detailsHref`` prop).
 *   * A role-gated override form (Req 9.4.4) that submits through
 *     ``POST /api/fuel/storm-mode/override`` to flip ``activate`` /
 *     ``deactivate`` / ``snooze`` / ``clear`` with a mandatory audit
 *     reason.
 *
 * The banner is intentionally self-contained — it owns its own polling
 * loop and fetches state independently of the page it is mounted on.
 * Callers only need to drop ``<StormModeBanner />`` at the top of their
 * layout; passing ``roles`` scopes the override form to operators who
 * may submit one.
 *
 * HTTP 503 ``storm_mode_evaluator_unavailable`` is treated as "hidden"
 * rather than an error — early-bootstrap environments where the
 * evaluator is not yet wired should not surface a red banner to every
 * dispatcher. Other transport errors render a small inline warning so
 * operators know telemetry is stale.
 *
 * Validates: Requirements 9.1.6, 9.4.1, 9.4.2, 9.4.3, 9.4.4.
 */

import {
  AlertTriangle,
  ExternalLink,
  Loader2,
  ShieldAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../../services/api";
import type {
  StormModeActiveOverride,
  StormModeOverrideAction,
  StormModeStatusResponse,
  StormModeTriggeringAlert,
  WeatherAlertSeverity,
} from "../../services/fuelApi";
import {
  getStormModeStatus,
  submitStormModeOverride,
} from "../../services/fuelApi";
import { getCurrentUserId, getCurrentUserRoles } from "../../utils/auth";

// ─── Role gate (Req 9.4.4) ───────────────────────────────────────────────────

/**
 * Roles permitted to submit Storm_Mode overrides. Mirrors the backend
 * ``_STORM_MODE_OVERRIDE_ROLES`` frozenset in
 * :mod:`fuel.api.fuel_ops_endpoints`. The UI gate is the first line of
 * defense; the backend re-checks the JWT context so a non-permitted
 * caller who bypasses the UI still gets HTTP 403 ``forbidden_role``.
 */
const OVERRIDE_ROLE_MARKERS = ["dispatcher", "admin"] as const;

/** Case-insensitive substring match on any role-token in the list. */
export function canSubmitStormModeOverride(
  roles: readonly string[] | null | undefined,
): boolean {
  if (!roles || roles.length === 0) return false;
  for (const raw of roles) {
    if (typeof raw !== "string") continue;
    const normalized = raw.trim().toLowerCase();
    if (!normalized) continue;
    for (const marker of OVERRIDE_ROLE_MARKERS) {
      if (normalized.includes(marker)) return true;
    }
  }
  return false;
}

// ─── Poll interval ───────────────────────────────────────────────────────────

/**
 * The evaluator ticks every 5 minutes, but the banner's override path
 * can persist state within one request — poll at a 60-second cadence so
 * a just-submitted override is visible within one refresh while not
 * flooding the API.
 */
const STATUS_POLL_INTERVAL_MS = 60_000;

// ─── Helpers ─────────────────────────────────────────────────────────────────

const SEVERITY_BADGE: Record<WeatherAlertSeverity, string> = {
  minor: "bg-warning-light text-warning-dark border-warning-light",
  moderate: "bg-warning-light text-warning-dark border-warning-light",
  severe: "bg-error-light text-error-dark border-error-light",
  extreme: "bg-error-light text-error-dark border-error",
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

function describeOverride(override: StormModeActiveOverride): string {
  const label = override.action[0].toUpperCase() + override.action.slice(1);
  if (override.expires_at) {
    return `${label} until ${formatDateTime(override.expires_at)} (${override.actor_id})`;
  }
  return `${label} (${override.actor_id})`;
}

// ─── Override Form ───────────────────────────────────────────────────────────

interface OverrideFormProps {
  actorId: string;
  onSubmitted: () => void;
  onClose: () => void;
}

/**
 * Role-restricted override form. Posts through
 * :func:`submitStormModeOverride` and closes on success. Displays a
 * structured error message on HTTP 403 ``forbidden_role`` / HTTP 422
 * ``validation_error`` so the operator understands why the submission
 * was rejected without having to dig into network traces.
 */
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
    "w-full px-3 py-2 text-sm border border-error-light rounded-lg bg-white focus:ring-2 focus:ring-error-light focus:border-error text-gray-900";

  return (
    <form
      onSubmit={handleSubmit}
      className="mt-4 bg-white border border-error-light rounded-lg p-4 space-y-3"
      aria-label="Submit Storm_Mode override"
    >
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-semibold text-error-dark">
          Submit Storm_Mode override
        </h4>
        <button
          type="button"
          onClick={onClose}
          className="p-1 rounded hover:bg-error-light text-error-dark"
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
          className="inline-flex items-center gap-2 px-3 py-1.5 text-sm font-medium bg-error text-white rounded-lg hover:bg-error-dark disabled:bg-error"
          disabled={submitting}
        >
          {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
          Submit override
        </button>
      </div>
    </form>
  );
}

// ─── Main Banner ─────────────────────────────────────────────────────────────

export interface StormModeBannerProps {
  /**
   * Optional override for the caller's role list driving the UI-side gate
   * (Req 8.6, 9.4.4). When omitted, the banner reads roles from the verified
   * SuperTokens session's access-token claims (`getCurrentUserRoles`) rather
   * than relying on a hardcoded prop. The backend independently re-verifies
   * the session so a caller with no roles still receives HTTP 403 on submit;
   * the gate only hides the control from operators who may not use it.
   * Primarily injected by tests.
   */
  roles?: readonly string[] | null;
  /**
   * Optional override for the actor identifier stamped on submitted overrides.
   * When omitted, the banner derives it from the verified session's user id
   * (`getCurrentUserId`). The backend derives the audit actor from the session
   * and ignores any client-supplied value, so this is presentation-only.
   * Primarily injected by tests.
   */
  actorId?: string;
  /**
   * Link rendered on the banner to open the full storm-mode detail
   * view. Defaults to the weather-alerts admin page.
   */
  detailsHref?: string;
  /**
   * Inject a status loader for unit tests. Defaults to
   * :func:`getStormModeStatus`.
   */
  fetchStatus?: () => Promise<StormModeStatusResponse>;
  /**
   * Polling cadence in milliseconds. Exposed for tests — production
   * callers should leave this at the default 60-second interval.
   */
  pollIntervalMs?: number;
}

/**
 * Storm_Mode banner. Hidden when state is ``inactive`` or the
 * evaluator is unavailable; pinned to the top of the container when
 * ``state === "active"`` (Req 9.4.1).
 */
export default function StormModeBanner({
  roles: rolesProp,
  actorId: actorIdProp,
  detailsHref = "/admin/weather-alerts",
  fetchStatus = getStormModeStatus,
  pollIntervalMs = STATUS_POLL_INTERVAL_MS,
}: StormModeBannerProps) {
  const [status, setStatus] = useState<StormModeStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [transientError, setTransientError] = useState<string | null>(null);
  const [showOverrideForm, setShowOverrideForm] = useState(false);
  // Roles/actor read from the verified session's claims (Req 8.6). When the
  // caller passes explicit props (e.g. tests) those win; otherwise we hydrate
  // from `getCurrentUserRoles` / `getCurrentUserId` on mount.
  const [sessionRoles, setSessionRoles] = useState<string[]>([]);
  const [sessionActorId, setSessionActorId] = useState<string>("");
  const cancelledRef = useRef(false);

  const reload = useCallback(async () => {
    try {
      const next = await fetchStatus();
      if (cancelledRef.current) return;
      setStatus(next);
      setTransientError(null);
    } catch (err) {
      if (cancelledRef.current) return;
      // 503 means the evaluator is not wired — hide rather than error.
      if (err instanceof ApiError && err.status === 503) {
        setStatus(null);
        setTransientError(null);
      } else {
        setTransientError(
          err instanceof Error
            ? err.message
            : "Unknown error loading Storm_Mode status",
        );
      }
    } finally {
      if (!cancelledRef.current) setLoading(false);
    }
  }, [fetchStatus]);

  useEffect(() => {
    cancelledRef.current = false;
    reload();
    const interval = window.setInterval(reload, pollIntervalMs);
    return () => {
      cancelledRef.current = true;
      window.clearInterval(interval);
    };
  }, [reload, pollIntervalMs]);

  // Hydrate roles + actor from the verified session unless the caller supplied
  // explicit props (Req 8.6). Role-gating is presentation-only; the backend
  // re-verifies the session and derives the audit actor on every request.
  useEffect(() => {
    if (rolesProp !== undefined && actorIdProp !== undefined) return;
    let cancelled = false;
    void Promise.all([getCurrentUserRoles(), getCurrentUserId()]).then(
      ([r, id]) => {
        if (cancelled) return;
        setSessionRoles(r);
        if (id) setSessionActorId(id);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [rolesProp, actorIdProp]);

  const effectiveRoles = rolesProp ?? sessionRoles;
  const effectiveActorId = actorIdProp ?? sessionActorId ?? "";

  const canOverride = useMemo(
    () => canSubmitStormModeOverride(effectiveRoles),
    [effectiveRoles],
  );

  // Keep the hook order stable: only render-gate after memoization.
  if (loading && !status) {
    return null;
  }

  if (!status || status.state !== "active") {
    // Optional: surface a small stale-telemetry note on transport error
    // but do not block operators. Callers who care about network state
    // already have <WebSocketStatus /> — we stay quiet by default.
    return null;
  }

  const triggeringAlert: StormModeTriggeringAlert | undefined =
    (status.triggering_alerts ?? [])[0];
  const severity = triggeringAlert?.severity;
  const severityBadge = severity
    ? SEVERITY_BADGE[severity]
    : "bg-error-light text-error-dark border-error-light";

  return (
    <div
      role="alert"
      aria-live="polite"
      data-testid="storm-mode-banner"
      className="sticky top-0 z-30 w-full bg-error-light border-b-2 border-error shadow-sm"
    >
      <div className="px-6 py-3">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 bg-error rounded-lg flex items-center justify-center flex-shrink-0">
            <AlertTriangle className="w-5 h-5 text-white" />
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-error-dark">
                Storm_Mode active
              </h3>
              {severity && (
                <span
                  className={`inline-flex items-center text-[10px] uppercase tracking-wide px-2 py-0.5 rounded font-medium border ${severityBadge}`}
                >
                  {severity}
                </span>
              )}
              {status.override_active && status.override && (
                <span className="inline-flex items-center gap-1 text-[11px] text-brand-secondary bg-brand-secondary-soft border border-brand-secondary-soft rounded px-2 py-0.5">
                  <ShieldAlert className="w-3 h-3" />
                  Override: {describeOverride(status.override)}
                </span>
              )}
            </div>

            {triggeringAlert ? (
              <p className="text-sm text-error-dark mt-1">
                <span className="font-medium">
                  {humanizeAlertType(triggeringAlert.alert_type)}
                </span>
                {triggeringAlert.headline
                  ? ` — ${triggeringAlert.headline}`
                  : ""}
              </p>
            ) : (
              <p className="text-sm text-error-dark mt-1">
                Heightened operations posture active.
              </p>
            )}

            <dl className="mt-2 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-1 text-xs text-error-dark">
              <div>
                <dt className="inline font-medium">Activated:</dt>{" "}
                <dd className="inline">
                  {formatDateTime(status.activation_window.activated_at)}
                </dd>
              </div>
              <div>
                <dt className="inline font-medium">Expected clear:</dt>{" "}
                <dd className="inline">
                  {formatDateTime(status.activation_window.clears_at)}
                </dd>
              </div>
              <div>
                <dt className="inline font-medium">Threshold:</dt>{" "}
                <dd className="inline">
                  {status.activation_window.severity_threshold} (
                  {status.activation_window.lookahead_hours}h lookahead)
                </dd>
              </div>
              {triggeringAlert && (
                <div className="truncate">
                  <dt className="inline font-medium">Source:</dt>{" "}
                  <dd className="inline">
                    {triggeringAlert.source.toUpperCase()}
                    {(triggeringAlert.affected_zip_codes ?? []).length > 0
                      ? ` · ${
                          (triggeringAlert.affected_zip_codes ?? []).length
                        } ZIP${
                          (triggeringAlert.affected_zip_codes ?? []).length ===
                          1
                            ? ""
                            : "s"
                        }`
                      : ""}
                  </dd>
                </div>
              )}
            </dl>

            {transientError && (
              <p className="mt-2 text-xs text-error-dark">
                Status telemetry stale: {transientError}
              </p>
            )}
          </div>

          <div className="flex items-center gap-2 flex-shrink-0">
            <a
              href={detailsHref}
              className="inline-flex items-center gap-1 text-xs font-medium text-error-dark hover:text-error-dark underline"
            >
              Full details
              <ExternalLink className="w-3 h-3" />
            </a>
            {canOverride && !showOverrideForm && (
              <button
                type="button"
                onClick={() => setShowOverrideForm(true)}
                className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-medium bg-white border border-error text-error-dark rounded-lg hover:bg-error-light"
              >
                Override
              </button>
            )}
          </div>
        </div>

        {canOverride && showOverrideForm && (
          <OverrideForm
            actorId={effectiveActorId}
            onSubmitted={() => {
              setShowOverrideForm(false);
              // Immediately re-poll so the banner reflects the change
              // without waiting for the next scheduled tick.
              reload();
            }}
            onClose={() => setShowOverrideForm(false)}
          />
        )}
      </div>
    </div>
  );
}
