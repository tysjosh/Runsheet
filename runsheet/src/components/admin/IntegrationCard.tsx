"use client";

/**
 * Per-provider card used by the Integration Marketplace
 * (:route:`/dashboard/settings?tab=integrations`).
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
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { humanize } from "../../lib/format";
import {
  deriveMarketplaceStatus,
  INTEGRATION_CATEGORY_LABELS,
  type IntegrationInstance,
  type MarketplaceStatus,
  type ProviderCatalogEntry,
  type SyncRun,
} from "../../services/integrationsApi";
import {
  Button,
  Field,
  FormDialog,
  INPUT_CLASS,
  Modal,
  NumberField,
  Select,
} from "../ui";

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
    fg: "text-success-dark",
    bg: "bg-success-light",
    Icon: CheckCircle2,
  },
  pending: {
    label: "Pending",
    fg: "text-info-dark",
    bg: "bg-info-light",
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
    fg: "text-error-dark",
    bg: "bg-error-light",
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
  onSubmit: (
    credentials: Record<string, string>,
    options?: IntegrationConnectOptions,
  ) => Promise<void>;
}

export interface IntegrationConnectOptions {
  config?: Record<string, unknown>;
  scheduleCron?: string;
}

const FIELD_LABELS: Record<string, string> = {
  api_token: "API token",
  api_key: "API key",
  client_id: "Client ID",
  client_secret: "Client secret",
  refresh_token: "Refresh token",
  realm_id: "Realm ID",
  security_code: "Security code",
  database: "Database",
  username: "Username",
  password: "Password",
  webhook_secret: "Webhook secret",
  secret_key: "Secret key",
  publishable_key: "Publishable key",
  portal_id: "Portal ID",
  access_token: "Access token",
};

/** "client_secret" → "Client secret" (no raw field codes as labels). */
export function credentialLabel(field: string): string {
  return (
    FIELD_LABELS[field] ??
    humanize(field).replace(/\b(id|api|url)\b/gi, (w) => w.toUpperCase())
  );
}

type ConnectValues = {
  creds: Record<string, string>;
  mode: "api_token" | "tls_401_tcp";
  endpoint_url: string;
  host: string;
  port: number | null;
  schedule_cron: string;
  tank_map: string;
};

/**
 * The credential form (task 3.8: an md FormDialog). Fields are driven by the
 * provider's ``required_credential_fields`` schema (Req 5.6.2). Values are
 * never persisted locally; on submit they go to the parent, which POSTs them
 * to the server. State is discarded on unmount.
 *
 * OAuth providers (QBO, Geotab) still see this dialog so a user can paste
 * the refresh token / database + username returned by the consent flow.
 */
function ConnectModal({ provider, onCancel, onSubmit }: ConnectModalProps) {
  const isVeederRoot = provider.provider_name === "veeder_root";
  const visibleFields = (mode: ConnectValues["mode"]) =>
    provider.required_credential_fields.filter(
      (field) =>
        !isVeederRoot ||
        (mode === "api_token"
          ? field === "api_token"
          : field === "security_code"),
    );

  const validate = (v: ConnectValues) => {
    const errors: Record<string, string | undefined> = {};
    const required = isVeederRoot
      ? v.mode === "api_token"
        ? ["api_token"]
        : []
      : provider.required_credential_fields;
    for (const field of required) {
      if (!v.creds[field]?.trim())
        errors[`cred_${field}`] = `${credentialLabel(field)} is required.`;
    }
    if (isVeederRoot && v.mode === "api_token" && !v.endpoint_url.trim())
      errors.endpoint_url = "The API endpoint is required for cloud API mode.";
    if (isVeederRoot && v.mode === "tls_401_tcp" && !v.host.trim())
      errors.host = "The host is required for TLS-401 mode.";
    if (isVeederRoot) {
      try {
        const parsed = JSON.parse(v.tank_map || "{}");
        if (!parsed || Array.isArray(parsed) || typeof parsed !== "object")
          errors.tank_map = "Tank map must be a JSON object.";
      } catch {
        errors.tank_map = "Tank map must be valid JSON.";
      }
    }
    return errors;
  };

  const submit = async (v: ConnectValues) => {
    const trimmed: Record<string, string> = {};
    for (const [key, value] of Object.entries(v.creds)) {
      if (value.trim()) trimmed[key] = value.trim();
    }
    if (!isVeederRoot) {
      await onSubmit(trimmed);
      return;
    }
    const options: IntegrationConnectOptions = {
      scheduleCron: v.schedule_cron.trim() || "*/15 * * * *",
      config: {
        mode: v.mode,
        tank_map: JSON.parse(v.tank_map || "{}") as Record<string, unknown>,
        ...(v.mode === "api_token"
          ? { endpoint_url: v.endpoint_url.trim() }
          : { host: v.host.trim(), port: v.port ?? 10001 }),
      },
    };
    await onSubmit(trimmed, options);
  };

  const initialCreds: Record<string, string> = {};
  for (const field of provider.required_credential_fields)
    initialCreds[field] = "";

  return (
    <FormDialog<ConnectValues, void>
      open
      size="md"
      title={`Connect ${formatProviderName(provider.provider_name)}`}
      help={provider.description}
      submitLabel="Connect"
      successMessage={null}
      initialValues={{
        creds: initialCreds,
        mode: "api_token",
        endpoint_url: "",
        host: "",
        port: 10001,
        schedule_cron: "*/15 * * * *",
        tank_map: "{}",
      }}
      validate={validate}
      onSubmit={submit}
      onClose={onCancel}
    >
      {({ values, set, errors }) => (
        <>
          {provider.doc_url && (
            <p className="col-span-2 text-xs">
              <a
                href={provider.doc_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 text-link hover:underline"
              >
                Setup guide
                <ExternalLink className="h-3 w-3" aria-hidden="true" />
              </a>
            </p>
          )}
          {isVeederRoot && (
            <Field label="Connection mode">
              <Select
                id={`ic-mode-${provider.provider_name}`}
                value={values.mode}
                onChange={(m) => set("mode", m as ConnectValues["mode"])}
                options={[
                  { value: "api_token", label: "Cloud API" },
                  { value: "tls_401_tcp", label: "TLS-401 TCP" },
                ]}
              />
            </Field>
          )}
          {visibleFields(values.mode).map((field) => (
            <Field
              key={field}
              label={credentialLabel(field)}
              required={
                !isVeederRoot ||
                (values.mode === "api_token" && field === "api_token")
              }
              id={`ic-cred-${provider.provider_name}-${field}`}
              error={errors[`cred_${field}`]}
            >
              <input
                id={`ic-cred-${provider.provider_name}-${field}`}
                type={
                  /secret|password|token|key/i.test(field) ? "password" : "text"
                }
                className={`${INPUT_CLASS} font-mono`}
                value={values.creds[field] ?? ""}
                onChange={(e) =>
                  set("creds", { ...values.creds, [field]: e.target.value })
                }
                autoComplete="off"
                spellCheck={false}
              />
            </Field>
          ))}
          {isVeederRoot &&
            (values.mode === "api_token" ? (
              <Field label="API endpoint" required error={errors.endpoint_url}>
                <input
                  id={`ic-endpoint-${provider.provider_name}`}
                  value={values.endpoint_url}
                  onChange={(e) => set("endpoint_url", e.target.value)}
                  placeholder="https://insite360.veeder-root.com"
                  className={INPUT_CLASS}
                />
              </Field>
            ) : (
              <>
                <Field label="Host" required span={1} error={errors.host}>
                  <input
                    id={`ic-host-${provider.provider_name}`}
                    value={values.host}
                    onChange={(e) => set("host", e.target.value)}
                    className={INPUT_CLASS}
                  />
                </Field>
                <Field label="Port" span={1}>
                  <NumberField
                    id={`ic-port-${provider.provider_name}`}
                    value={values.port}
                    onChange={(n) => set("port", n)}
                    decimals={0}
                    min={1}
                    max={65535}
                  />
                </Field>
              </>
            ))}
          {isVeederRoot && (
            <>
              <Field label="Polling schedule (cron)">
                <input
                  id={`ic-cron-${provider.provider_name}`}
                  value={values.schedule_cron}
                  onChange={(e) => set("schedule_cron", e.target.value)}
                  className={`${INPUT_CLASS} font-mono`}
                />
              </Field>
              <Field label="Tank map (JSON)" error={errors.tank_map}>
                <textarea
                  id={`ic-tankmap-${provider.provider_name}`}
                  value={values.tank_map}
                  onChange={(e) => set("tank_map", e.target.value)}
                  rows={4}
                  className={`${INPUT_CLASS} h-auto py-1.5 font-mono`}
                  placeholder='{"1":{"target":"customer_tank","id":"tank-100","product_code":"DIESEL_2"}}'
                />
              </Field>
            </>
          )}
          <p className="col-span-2 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2 text-[11px] text-slate-700">
            Credentials are wrapped by the tenant credentials vault on save. The
            server never returns them again, only an opaque reference.
          </p>
        </>
      )}
    </FormDialog>
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
    <Modal
      isOpen
      onClose={onCancel}
      title={`Disconnect ${providerDisplayName}?`}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={submitting}
            icon={<Trash2 className="h-3.5 w-3.5" aria-hidden="true" />}
            onClick={handleConfirm}
          >
            Disconnect
          </Button>
        </>
      }
    >
      <div className="space-y-3 px-6 py-4">
        <p className="text-sm text-slate-700">
          This removes the stored credentials reference and stops the cron
          schedule. You'll need to re-enter credentials to reconnect.
        </p>
        {error && (
          <p role="alert" className="text-sm text-red-800">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}

// ─── Sync Run Summary ────────────────────────────────────────────────────────

const SYNC_STATUS_CONFIG: Record<
  SyncRun["status"],
  { label: string; fg: string; bg: string }
> = {
  running: { label: "Running", fg: "text-info-dark", bg: "bg-info-light" },
  success: {
    label: "Success",
    fg: "text-success-dark",
    bg: "bg-success-light",
  },
  partial: {
    label: "Partial",
    fg: "text-warning-dark",
    bg: "bg-warning-light",
  },
  error: { label: "Error", fg: "text-error-dark", bg: "bg-error-light" },
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
          <span className="text-gray-500">
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
          className="text-error truncate"
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
  onConnect: (
    credentials: Record<string, string>,
    options?: IntegrationConnectOptions,
  ) => Promise<void>;
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
    async (
      creds: Record<string, string>,
      options?: IntegrationConnectOptions,
    ) => {
      await onConnect(creds, options);
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
  const primaryButton = `${buttonBase} bg-primary text-white border-primary hover:opacity-90`;
  const secondaryButton = `${buttonBase} bg-white text-gray-700 border-gray-200 hover:bg-gray-50`;
  const dangerButton = `${buttonBase} bg-white text-error border-error-light hover:bg-error-light`;

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
          style={{ backgroundColor: "var(--color-primary-soft)" }}
          aria-hidden="true"
        >
          {isOAuthProvider(provider) ? (
            <Link2 className="w-4.5 h-4.5 text-primary" />
          ) : (
            <Key className="w-4.5 h-4.5 text-primary" />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-primary truncate">
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
          <p className="text-gray-500 uppercase tracking-wide">Auth</p>
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
          <p className="text-gray-500 uppercase tracking-wide">Last sync</p>
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
          className="flex items-start gap-2 rounded-lg border border-error-light bg-error-light px-3 py-2 text-xs text-error-dark"
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
            <p className="text-[10px] font-medium text-gray-500 uppercase tracking-wide">
              Recent activity
            </p>
            {syncRunsLoading && (
              <Loader2
                className="w-3 h-3 text-gray-500 animate-spin"
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
