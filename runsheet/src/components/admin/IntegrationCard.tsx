"use client";

/**
 * Per-provider card used by the Integration Marketplace
 * (:route:`/admin/integrations`).
 *
 * Renders a single :class:`ProviderCatalogEntry` along with the
 * currently-configured :class:`IntegrationInstance` (when one exists)
 * and the last :class:`SyncRun` summary (Req 5.6.1, 5.6.4). Surfaces
 * the enable / disable / sync-now / disconnect controls (Req 5.6.5)
 * and a Connect CTA that opens either an API-key form or hands off to
 * an OAuth authorization URL (Req 5.6.3).
 *
 * This component is deliberately dumb: every mutation is delegated to
 * callbacks supplied by the parent Marketplace page so the page can
 * coordinate optimistic updates, toasts, and re-fetches in one place.
 *
 * Validates: Requirements 5.6.1, 5.6.3, 5.6.4, 5.6.5.
 */

import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  ExternalLink,
  Key,
  Link2,
  Loader2,
  Play,
  Power,
  PowerOff,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  deriveMarketplaceStatus,
  INTEGRATION_CATEGORY_LABELS,
  type IntegrationInstance,
  type MarketplaceStatus,
  type ProviderCatalogEntry,
  type SyncRun,
} from "../../services/integrationsApi";

// ─── Status Badge ────────────────────────────────────────────────────────────

const STATUS_BADGE_CONFIG: Record<
  MarketplaceStatus,
  { label: string; fg: string; bg: string; Icon: typeof CheckCircle2 }
> = {
  available: {
    label: "Available",
    fg: "text-gray-700",
    bg: "bg-gray-100",
    Icon: CircleDashed,
  },
  connected: {
    label: "Connected",
    fg: "text-green-700",
    bg: "bg-green-100",
    Icon: CheckCircle2,
  },
  pending: {
    label: "Pending",
    fg: "text-blue-700",
    bg: "bg-blue-100",
    Icon: Loader2,
  },
  disabled: {
    label: "Disabled",
    fg: "text-gray-600",
    bg: "bg-gray-100",
    Icon: PowerOff,
  },
  error: {
    label: "Error",
    fg: "text-red-700",
    bg: "bg-red-100",
    Icon: AlertTriangle,
  },
};

function StatusBadge({ status }: { status: MarketplaceStatus }) {
  const config = STATUS_BADGE_CONFIG[status];
  const { Icon, label, fg, bg } = config;
  return (
    <span
      data-testid={`status-badge-${status}`}
      className={`inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded font-medium ${bg} ${fg}`}
    >
      <Icon
        className={`w-3 h-3 ${status === "pending" ? "animate-spin" : ""}`}
        aria-hidden="true"
      />
      {label}
    </span>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

export function formatProviderName(name: string): string {
  // `quickbooks_online` → `Quickbooks Online`
  return name
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatCategoryLabel(category: string): string {
  const known = INTEGRATION_CATEGORY_LABELS as Record<string, string>;
  return known[category] ?? formatProviderName(category);
}

function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return iso ?? "—";
  const deltaSec = Math.max(0, (Date.now() - ts) / 1000);
  if (deltaSec < 60) return "just now";
  if (deltaSec < 3600)
    return `${Math.floor(deltaSec / 60)} min${deltaSec < 120 ? "" : "s"} ago`;
  if (deltaSec < 86400)
    return `${Math.floor(deltaSec / 3600)} hr${deltaSec < 7200 ? "" : "s"} ago`;
  return `${Math.floor(deltaSec / 86400)}d ago`;
}

function summarizeRecordCounts(counts: Record<string, number>): string {
  const entries = Object.entries(counts).filter(([, v]) => Number.isFinite(v));
  if (entries.length === 0) return "";
  return entries.map(([k, v]) => `${k}: ${v}`).join(" · ");
}

/**
 * Is this provider's connect flow an OAuth handoff (QBO, Geotab) or an
 * API-key entry (Veeder-Root, Stripe)? Task 11.6 requires both forms;
 * the component picks the right one off ``auth_mode``.
 */
export function isOAuthProvider(provider: ProviderCatalogEntry): boolean {
  return provider.auth_mode === "oauth2";
}

// ─── Connect Modal ───────────────────────────────────────────────────────────

interface ConnectModalProps {
  provider: ProviderCatalogEntry;
  onCancel: () => void;
  onSubmit: (credentials: Record<string, string>) => Promise<void>;
}

/**
 * Render a credential form whose fields are driven by the provider's
 * ``required_credential_fields`` schema (Req 5.6.2). The form never
 * persists values anywhere — on submit it hands them to the parent,
 * which immediately POSTs them to the server. The local state is
 * discarded on unmount.
 *
 * OAuth providers (QBO, Geotab) still see this modal so a user can
 * paste the refresh-token / database + username tuple returned by the
 * provider's consent flow (the Marketplace links out to the
 * authorization URL via the "Open OAuth consent" button above).
 */
function ConnectModal({ provider, onCancel, onSubmit }: ConnectModalProps) {
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const field of provider.required_credential_fields) {
      initial[field] = "";
    }
    return initial;
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white font-mono";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    // Refuse blank required fields. Trim prevents accidental whitespace.
    for (const field of provider.required_credential_fields) {
      if (!values[field] || !values[field].trim()) {
        setError(`${field} is required.`);
        return;
      }
    }
    setSubmitting(true);
    try {
      const trimmed: Record<string, string> = {};
      for (const [key, value] of Object.entries(values)) {
        trimmed[key] = value.trim();
      }
      await onSubmit(trimmed);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Connect failed.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      role="dialog"
      aria-modal="true"
      aria-labelledby={`ic-connect-title-${provider.provider_name}`}
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2
            id={`ic-connect-title-${provider.provider_name}`}
            className="text-lg font-semibold text-[#232323]"
          >
            Connect {formatProviderName(provider.provider_name)}
          </h2>
          <button
            type="button"
            onClick={onCancel}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close connect form"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          <p className="text-xs text-gray-500">
            {provider.description}{" "}
            {provider.doc_url && (
              <a
                href={provider.doc_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-600 hover:underline inline-flex items-center gap-0.5"
              >
                Setup guide
                <ExternalLink className="w-3 h-3" aria-hidden="true" />
              </a>
            )}
          </p>

          {error && (
            <p
              role="alert"
              className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg"
            >
              {error}
            </p>
          )}

          <div className="space-y-3">
            {provider.required_credential_fields.map((field) => (
              <div key={field}>
                <label
                  htmlFor={`ic-cred-${provider.provider_name}-${field}`}
                  className="block text-xs font-medium text-gray-600 mb-1"
                >
                  {field}
                </label>
                <input
                  id={`ic-cred-${provider.provider_name}-${field}`}
                  type={
                    /secret|password|token|key/i.test(field)
                      ? "password"
                      : "text"
                  }
                  className={inputClass}
                  value={values[field] ?? ""}
                  onChange={(e) =>
                    setValues((prev) => ({ ...prev, [field]: e.target.value }))
                  }
                  autoComplete="off"
                  spellCheck={false}
                  required
                />
              </div>
            ))}
          </div>

          <p className="text-[11px] text-gray-500 bg-gray-50 border border-gray-100 rounded-lg px-3 py-2">
            Credentials are wrapped by the tenant credentials vault on save. The
            server never returns them on any subsequent request — only an opaque
            reference.
          </p>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onCancel}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="inline-flex items-center gap-1.5 px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              {submitting ? (
                <Loader2
                  className="w-3.5 h-3.5 animate-spin"
                  aria-hidden="true"
                />
              ) : (
                <Link2 className="w-3.5 h-3.5" aria-hidden="true" />
              )}
              {submitting ? "Connecting..." : "Connect"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Disconnect Confirmation ─────────────────────────────────────────────────

interface DisconnectModalProps {
  providerDisplayName: string;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}

function DisconnectModal({
  providerDisplayName,
  onCancel,
  onConfirm,
}: DisconnectModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const handleConfirm = async () => {
    setError("");
    setSubmitting(true);
    try {
      await onConfirm();
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to disconnect integration.",
      );
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      role="dialog"
      aria-modal="true"
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-[#232323]">
            Disconnect {providerDisplayName}?
          </h2>
          <button
            type="button"
            onClick={onCancel}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close disconnect confirmation"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="px-6 py-4 space-y-3">
          <p className="text-sm text-gray-700">
            This removes the stored credentials reference and stops the cron
            schedule. You will need to re-enter credentials to reconnect.
          </p>
          {error && (
            <p
              role="alert"
              className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg"
            >
              {error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-3 px-6 pb-4">
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={submitting}
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50 bg-red-600 hover:bg-red-700"
          >
            <Trash2 className="w-3.5 h-3.5" aria-hidden="true" />
            {submitting ? "Disconnecting..." : "Disconnect"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Sync Run Summary ────────────────────────────────────────────────────────

const SYNC_STATUS_CONFIG: Record<
  SyncRun["status"],
  { label: string; fg: string; bg: string }
> = {
  running: { label: "Running", fg: "text-blue-700", bg: "bg-blue-100" },
  success: { label: "Success", fg: "text-green-700", bg: "bg-green-100" },
  partial: { label: "Partial", fg: "text-yellow-700", bg: "bg-yellow-100" },
  error: { label: "Error", fg: "text-red-700", bg: "bg-red-100" },
};

function SyncRunSummary({
  run,
  totalRuns,
}: {
  run: SyncRun | null;
  totalRuns: number;
}) {
  if (!run) {
    return (
      <div className="rounded-lg border border-dashed border-gray-200 bg-gray-50/60 px-3 py-2 text-xs text-gray-500">
        No sync runs recorded yet. Use "Sync now" to trigger the first one.
      </div>
    );
  }
  const cfg = SYNC_STATUS_CONFIG[run.status];
  const countSummary = summarizeRecordCounts(run.record_counts);
  return (
    <div
      data-testid="sync-run-summary"
      className="rounded-lg border border-gray-100 bg-gray-50/60 px-3 py-2 text-xs space-y-1"
    >
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className={`inline-flex items-center text-[10px] px-1.5 py-0.5 rounded font-medium ${cfg.bg} ${cfg.fg}`}
        >
          {cfg.label}
        </span>
        <span className="font-medium text-gray-700">{run.operation}</span>
        <span className="text-gray-500">
          {formatRelativeTime(run.started_at)}
        </span>
        {totalRuns > 1 && (
          <span className="text-gray-400">
            ({totalRuns} recent run{totalRuns === 1 ? "" : "s"})
          </span>
        )}
      </div>
      {countSummary && (
        <p className="text-gray-500 truncate" title={countSummary}>
          {countSummary}
        </p>
      )}
      {run.status === "error" && run.error_details && (
        <p
          className="text-red-600 truncate"
          title={run.error_details}
          data-testid="sync-run-error"
        >
          {run.error_details}
        </p>
      )}
    </div>
  );
}

// ─── Main Card ───────────────────────────────────────────────────────────────

export interface IntegrationCardProps {
  provider: ProviderCatalogEntry;
  instance: IntegrationInstance | null;
  syncRuns: SyncRun[];
  /**
   * True while the parent is loading the tail of sync runs for this
   * provider. Kept separate from the action-level ``working`` flag so
   * a background re-fetch doesn't grey out the action buttons.
   */
  syncRunsLoading?: boolean;
  /**
   * Per-action busy signal — parent flips this true while a single
   * mutation (enable, disable, sync-now, connect, disconnect) is in
   * flight so the card can disable every button uniformly.
   */
  working?: boolean;
  /**
   * Invoked with the tenant's credential payload after the user
   * submits the connect form (or pastes OAuth tokens). The parent
   * POSTs it to ``/api/integrations`` and refreshes state. OAuth
   * providers see the same callback — the modal just labels the
   * fields appropriately.
   */
  onConnect: (credentials: Record<string, string>) => Promise<void>;
  onEnable: () => Promise<void>;
  onDisable: () => Promise<void>;
  onSyncNow: () => Promise<void>;
  onDisconnect: () => Promise<void>;
  /**
   * Optional OAuth authorization URL. When supplied for an
   * ``auth_mode === 'oauth2'`` provider the card renders an "Open
   * consent" anchor alongside the Connect button so operators can
   * complete the provider handoff in a new tab before returning to
   * paste the refresh-token into the modal.
   */
  oauthAuthorizationUrl?: string;
}

export default function IntegrationCard({
  provider,
  instance,
  syncRuns,
  syncRunsLoading,
  working,
  onConnect,
  onEnable,
  onDisable,
  onSyncNow,
  onDisconnect,
  oauthAuthorizationUrl,
}: IntegrationCardProps) {
  const [showConnect, setShowConnect] = useState(false);
  const [showDisconnect, setShowDisconnect] = useState(false);

  const status = useMemo(() => deriveMarketplaceStatus(instance), [instance]);
  const displayName = useMemo(
    () => formatProviderName(provider.provider_name),
    [provider.provider_name],
  );
  const categoryLabel = useMemo(
    () => formatCategoryLabel(provider.category),
    [provider.category],
  );

  // Most-recent run + total for context line.
  const latestRun = syncRuns[0] ?? null;

  const lastSyncAt = instance?.last_sync_at ?? latestRun?.started_at ?? null;

  const handleConnectSubmit = useCallback(
    async (creds: Record<string, string>) => {
      await onConnect(creds);
      setShowConnect(false);
    },
    [onConnect],
  );

  const handleDisconnectConfirm = useCallback(async () => {
    await onDisconnect();
    setShowDisconnect(false);
  }, [onDisconnect]);

  // Close modals automatically if the card unmounts or the instance
  // disappears while open (e.g. parent optimistically removed it).
  useEffect(() => {
    if (!instance) setShowDisconnect(false);
  }, [instance]);

  const buttonBase =
    "inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg border transition-colors disabled:opacity-50 disabled:cursor-not-allowed";
  const primaryButton = `${buttonBase} bg-[#232323] text-white border-[#232323] hover:opacity-90`;
  const secondaryButton = `${buttonBase} bg-white text-gray-700 border-gray-200 hover:bg-gray-50`;
  const dangerButton = `${buttonBase} bg-white text-red-600 border-red-200 hover:bg-red-50`;

  return (
    <div
      data-testid={`integration-card-${provider.provider_name}`}
      data-status={status}
      className="bg-white rounded-xl shadow-sm border border-gray-100 p-4 flex flex-col gap-3"
    >
      {/* Header */}
      <div className="flex items-start gap-3">
        <div
          className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
          style={{ backgroundColor: "#23232310" }}
          aria-hidden="true"
        >
          {isOAuthProvider(provider) ? (
            <Link2 className="w-4.5 h-4.5 text-[#232323]" />
          ) : (
            <Key className="w-4.5 h-4.5 text-[#232323]" />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-[#232323] truncate">
              {displayName}
            </h3>
            <StatusBadge status={status} />
          </div>
          <p className="text-[11px] text-gray-500 mt-0.5">{categoryLabel}</p>
        </div>
      </div>

      {/* Description */}
      <p
        className="text-xs text-gray-600 line-clamp-2"
        title={provider.description}
      >
        {provider.description}
      </p>

      {/* Connection meta */}
      <div className="grid grid-cols-2 gap-2 text-[11px] text-gray-500">
        <div>
          <p className="text-gray-400 uppercase tracking-wide">Auth</p>
          <p className="font-medium text-gray-700">
            {provider.auth_mode === "oauth2"
              ? "OAuth 2.0"
              : provider.auth_mode === "api_key"
                ? "API key"
                : provider.auth_mode === "basic"
                  ? "Basic auth"
                  : "Custom"}
          </p>
        </div>
        <div>
          <p className="text-gray-400 uppercase tracking-wide">Last sync</p>
          <p className="font-medium text-gray-700">
            {formatRelativeTime(lastSyncAt)}
          </p>
        </div>
      </div>

      {/* Error banner (rolling status) */}
      {status === "error" && instance?.last_error && (
        <div
          role="alert"
          data-testid="integration-error"
          className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
        >
          <AlertTriangle
            className="w-3.5 h-3.5 mt-0.5 shrink-0"
            aria-hidden="true"
          />
          <span className="flex-1 truncate" title={instance.last_error}>
            {instance.last_error}
          </span>
        </div>
      )}

      {/* Last sync run summary (Req 5.6.4) */}
      {instance && (
        <div>
          <div className="flex items-center justify-between mb-1">
            <p className="text-[10px] font-medium text-gray-400 uppercase tracking-wide">
              Recent activity
            </p>
            {syncRunsLoading && (
              <Loader2
                className="w-3 h-3 text-gray-400 animate-spin"
                aria-hidden="true"
              />
            )}
          </div>
          <SyncRunSummary run={latestRun} totalRuns={syncRuns.length} />
        </div>
      )}

      {/* Controls (Req 5.6.5) */}
      <div className="flex items-center gap-2 flex-wrap pt-1 border-t border-gray-100 mt-auto">
        {!instance ? (
          <>
            {isOAuthProvider(provider) && oauthAuthorizationUrl && (
              <a
                href={oauthAuthorizationUrl}
                target="_blank"
                rel="noopener noreferrer"
                className={secondaryButton}
                data-testid="oauth-consent-link"
              >
                <ExternalLink className="w-3.5 h-3.5" aria-hidden="true" />
                Open consent
              </a>
            )}
            <button
              type="button"
              onClick={() => setShowConnect(true)}
              disabled={working}
              className={primaryButton}
              data-testid="connect-button"
            >
              <Link2 className="w-3.5 h-3.5" aria-hidden="true" />
              Connect
            </button>
          </>
        ) : (
          <>
            {instance.enabled ? (
              <button
                type="button"
                onClick={onDisable}
                disabled={working}
                className={secondaryButton}
                data-testid="disable-button"
              >
                <PowerOff className="w-3.5 h-3.5" aria-hidden="true" />
                Disable
              </button>
            ) : (
              <button
                type="button"
                onClick={onEnable}
                disabled={working}
                className={secondaryButton}
                data-testid="enable-button"
              >
                <Power className="w-3.5 h-3.5" aria-hidden="true" />
                Enable
              </button>
            )}
            <button
              type="button"
              onClick={onSyncNow}
              disabled={working || !instance.enabled}
              className={secondaryButton}
              title={
                instance.enabled
                  ? "Run a sync now"
                  : "Enable this integration before running a sync"
              }
              data-testid="sync-now-button"
            >
              {working ? (
                <Loader2
                  className="w-3.5 h-3.5 animate-spin"
                  aria-hidden="true"
                />
              ) : (
                <Play className="w-3.5 h-3.5" aria-hidden="true" />
              )}
              Sync now
            </button>
            <button
              type="button"
              onClick={() => setShowConnect(true)}
              disabled={working}
              className={secondaryButton}
              data-testid="rotate-credentials-button"
              title="Rotate credentials"
            >
              <RefreshCw className="w-3.5 h-3.5" aria-hidden="true" />
              Rotate
            </button>
            <button
              type="button"
              onClick={() => setShowDisconnect(true)}
              disabled={working}
              className={dangerButton}
              data-testid="disconnect-button"
            >
              <Trash2 className="w-3.5 h-3.5" aria-hidden="true" />
              Disconnect
            </button>
          </>
        )}
      </div>

      {/* Modals */}
      {showConnect && (
        <ConnectModal
          provider={provider}
          onCancel={() => setShowConnect(false)}
          onSubmit={handleConnectSubmit}
        />
      )}
      {showDisconnect && instance && (
        <DisconnectModal
          providerDisplayName={displayName}
          onCancel={() => setShowDisconnect(false)}
          onConfirm={handleDisconnectConfirm}
        />
      )}
    </div>
  );
}
