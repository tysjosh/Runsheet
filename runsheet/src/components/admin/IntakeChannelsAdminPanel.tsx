"use client";

/**
 * Intake Channels Admin Panel — register, rotate, and disable intake
 * channels. Plaintext secret shown in a modal exactly once on create
 * and rotate with a "Copy to clipboard" button.
 *
 * Validates: Requirements 2.1.1, 2.1.4, 2.1.6
 */

import {
  AlertTriangle,
  Check,
  Copy,
  Key,
  Plus,
  Power,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  Field,
  FormDialog,
  IconButton,
  INPUT_CLASS,
  InlineBanner,
  Modal,
  ModalFooter,
  Select,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { humanize } from "../../lib/format";
import { ApiError } from "../../services/api";
import {
  createIntakeChannel,
  deleteIntakeChannel,
  type IntakeChannel,
  type IntakeChannelType,
  type IntakeChannelWithSecret,
  listIntakeChannels,
  rotateIntakeChannelSecret,
  updateIntakeChannel,
} from "../../services/intakeChannelsApi";
import { PageTitle } from "../ui/PageHeader";

// ─── Types ───────────────────────────────────────────────────────────────────

interface SecretModalState {
  isOpen: boolean;
  secret: string;
  channelId: string;
  action: "create" | "rotate";
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function IntakeChannelsAdminPanel() {
  const [channels, setChannels] = useState<IntakeChannel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState<string | null>(null);

  // Register dialog
  const [creating, setCreating] = useState(false);

  // Secret modal
  const [secretModal, setSecretModal] = useState<SecretModalState>({
    isOpen: false,
    secret: "",
    channelId: "",
    action: "create",
  });
  const [copied, setCopied] = useState(false);

  // Delete confirmation
  const [channelToDelete, setChannelToDelete] = useState<IntakeChannel | null>(
    null,
  );

  // ── Data loading ──────────────────────────────────────────────────────────

  const fetchChannels = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listIntakeChannels();
      setChannels(response.items ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load channels");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchChannels();
  }, [fetchChannels]);

  // ── Create channel ────────────────────────────────────────────────────────

  const submitCreate = async (
    v: ChannelValues,
  ): Promise<IntakeChannelWithSecret> =>
    createIntakeChannel({
      ...v,
      channel_id: v.channel_id.trim(),
      display_name: v.display_name.trim(),
    });

  const afterCreate = async (result: IntakeChannelWithSecret) => {
    setSecretModal({
      isOpen: true,
      secret: result.hmac_secret,
      channelId: result.channel_id,
      action: "create",
    });
    await fetchChannels();
  };

  // ── Rotate secret ─────────────────────────────────────────────────────────

  const handleRotate = useCallback(async (channelId: string) => {
    setWorking(channelId);
    try {
      const result: IntakeChannelWithSecret =
        await rotateIntakeChannelSecret(channelId);
      setSecretModal({
        isOpen: true,
        secret: result.hmac_secret,
        channelId: result.channel_id,
        action: "rotate",
      });
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to rotate secret",
      );
    } finally {
      setWorking(null);
    }
  }, []);

  // ── Toggle enabled ────────────────────────────────────────────────────────

  const handleToggleEnabled = useCallback(
    async (channel: IntakeChannel) => {
      setWorking(channel.channel_id);
      try {
        await updateIntakeChannel(channel.channel_id, {
          enabled: !channel.enabled,
        });
        await fetchChannels();
      } catch (err) {
        setError(
          err instanceof ApiError ? err.message : "Failed to update channel",
        );
      } finally {
        setWorking(null);
      }
    },
    [fetchChannels],
  );

  // ── Delete channel ────────────────────────────────────────────────────────

  const handleConfirmDelete = useCallback(async () => {
    if (!channelToDelete) return;
    const channelId = channelToDelete.channel_id;
    setWorking(channelId);
    try {
      await deleteIntakeChannel(channelId);
      setChannelToDelete(null);
      await fetchChannels();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to delete channel",
      );
    } finally {
      setWorking(null);
    }
  }, [channelToDelete, fetchChannels]);

  // ── Copy to clipboard ─────────────────────────────────────────────────────

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(secretModal.secret);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for environments without clipboard API
      const textarea = document.createElement("textarea");
      textarea.value = secretModal.secret;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [secretModal.secret]);

  // ── Render ────────────────────────────────────────────────────────────────

  const actions = useMemo(
    () => (
      <Button
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setCreating(true)}
      >
        Register Channel
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  const channelColumns: Column<IntakeChannel>[] = [
    {
      key: "display_name",
      header: "Name",
      truncate: true,
      className: "font-medium text-text",
      cell: (c) => c.display_name,
    },
    {
      key: "channel_id",
      header: "Channel ID",
      width: 220,
      className: "font-mono text-xs text-slate-700",
      cell: (c) => c.channel_id,
    },
    {
      key: "channel_type",
      header: "Type",
      width: 130,
      className: "text-slate-700",
      cell: (c) =>
        CHANNEL_TYPES.find((t) => t.value === c.channel_type)?.label ??
        humanize(c.channel_type),
    },
    {
      key: "status",
      header: "Status",
      width: 120,
      cell: (c) =>
        c.enabled ? (
          <StatusBadge status="ok" label="Enabled" />
        ) : (
          <StatusBadge status="draft" label="Disabled" />
        ),
    },
    {
      key: "versions",
      header: "Schema versions",
      width: 150,
      className: "text-xs text-slate-700",
      cell: (c) => c.supported_schema_versions.join(", "),
    },
  ];

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Intake Channels
          </PageTitle>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Intake channels"
        filters={
          <span className="truncate text-xs text-text-muted">
            Webhook channels that send orders in. Each signs requests with its
            own HMAC secret.
          </span>
        }
        end={
          <IconButton
            label="Refresh channels"
            size="sm"
            onClick={fetchChannels}
            icon={
              <RefreshCw
                className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
              />
            }
          />
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<IntakeChannel>
          ariaLabel="Intake channels list"
          columns={channelColumns}
          data={loading || error ? [] : channels}
          loading={loading}
          error={error ? { message: error, onRetry: fetchChannels } : null}
          getRowId={(c) => c.channel_id}
          rowLabel={(c) => c.display_name || c.channel_id}
          rowMenu={(c) => [
            {
              id: "rotate",
              label: "Rotate secret",
              icon: <Key className="h-3.5 w-3.5" />,
              disabled: working === c.channel_id,
              onSelect: () => void handleRotate(c.channel_id),
            },
            {
              id: "toggle",
              label: c.enabled ? "Disable" : "Enable",
              icon: <Power className="h-3.5 w-3.5" />,
              disabled: working === c.channel_id,
              onSelect: () => void handleToggleEnabled(c),
            },
            {
              id: "delete",
              label: "Delete",
              danger: true,
              icon: <Trash2 className="h-3.5 w-3.5" />,
              disabled: working === c.channel_id,
              onSelect: () => setChannelToDelete(c),
            },
          ]}
          emptyState={
            <span className="text-text-muted">
              No intake channels registered yet.
            </span>
          }
        />
      </div>

      {creating && (
        <FormDialog<ChannelValues, IntakeChannelWithSecret>
          open
          size="md"
          title="Register channel"
          help="The channel's HMAC secret is shown once after it's created."
          submitLabel="Register channel"
          successMessage={null}
          initialValues={{
            channel_id: "",
            channel_type: "api_partner",
            display_name: "",
            supported_schema_versions: ["1.0"],
            enabled: true,
          }}
          validate={(v) => ({
            channel_id: !v.channel_id.trim()
              ? "Enter a channel ID."
              : /\s/.test(v.channel_id.trim())
                ? "No spaces in the channel ID."
                : undefined,
            display_name: v.display_name.trim()
              ? undefined
              : "Enter a display name.",
          })}
          onSubmit={submitCreate}
          onSaved={(r) => void afterCreate(r)}
          onClose={() => setCreating(false)}
        >
          {({ values, set, errors }) => (
            <>
              <Field
                label="Display name"
                required
                span={1}
                error={errors.display_name}
              >
                <input
                  id="ic-display-name"
                  type="text"
                  value={values.display_name}
                  onChange={(e) => set("display_name", e.target.value)}
                  placeholder="Voice AI Provider"
                  className={INPUT_CLASS}
                />
              </Field>
              <Field
                label="Channel ID"
                required
                span={1}
                error={errors.channel_id}
              >
                <input
                  id="ic-channel-id"
                  type="text"
                  value={values.channel_id}
                  onChange={(e) => set("channel_id", e.target.value)}
                  placeholder="my-voice-provider"
                  className={INPUT_CLASS}
                />
              </Field>
              <Field label="Type" span={1}>
                <Select
                  id="ic-channel-type"
                  value={values.channel_type}
                  onChange={(v) => set("channel_type", v as IntakeChannelType)}
                  options={CHANNEL_TYPES}
                />
              </Field>
            </>
          )}
        </FormDialog>
      )}

      {/* Secret: shown exactly once on create and rotate */}
      <Modal
        isOpen={secretModal.isOpen}
        onClose={() => setSecretModal((s) => ({ ...s, isOpen: false }))}
        title={
          secretModal.action === "create" ? "Channel created" : "Secret rotated"
        }
        size="md"
        footer={
          <Button
            onClick={() => setSecretModal((s) => ({ ...s, isOpen: false }))}
          >
            Done
          </Button>
        }
      >
        <div className="space-y-3 px-6 py-4">
          <InlineBanner tone="warning">
            This secret is shown only once. Copy it now.
          </InlineBanner>
          <div className="flex items-center gap-2">
            <code
              className="flex-1 break-all rounded-lg bg-slate-100 px-3 py-2 font-mono text-sm text-text"
              data-testid="secret-value"
            >
              {secretModal.secret}
            </code>
            <Button
              variant="secondary"
              size="sm"
              onClick={handleCopy}
              aria-label="Copy to clipboard"
              icon={
                copied ? (
                  <Check className="h-3.5 w-3.5" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )
              }
            >
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
          <p className="text-xs text-text-muted">
            Channel: <span className="font-mono">{secretModal.channelId}</span>
          </p>
        </div>
      </Modal>

      {/* Delete confirmation */}
      <Modal
        isOpen={channelToDelete !== null}
        onClose={() => setChannelToDelete(null)}
        title="Delete Intake Channel"
        size="sm"
        footer={
          <ModalFooter
            onCancel={() => setChannelToDelete(null)}
            onConfirm={handleConfirmDelete}
            cancelText="Cancel"
            confirmText="Delete Channel"
            confirmVariant="danger"
            loading={
              channelToDelete !== null && working === channelToDelete.channel_id
            }
          />
        }
      >
        <div className="flex items-start gap-3 px-6 py-4">
          <AlertTriangle
            aria-hidden="true"
            className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-700"
          />
          <div className="text-sm text-slate-700">
            <p className="mb-2">
              Delete{" "}
              <span className="font-semibold text-text">
                {channelToDelete?.display_name}
              </span>{" "}
              (<span className="font-mono">{channelToDelete?.channel_id}</span>
              )?
            </p>
            <p className="text-text-muted">
              This permanently removes the channel and invalidates its HMAC
              secret. It can't be undone.
            </p>
          </div>
        </div>
      </Modal>
    </div>
  );
}

type ChannelValues = {
  channel_id: string;
  channel_type: IntakeChannelType;
  display_name: string;
  supported_schema_versions: string[];
  enabled: boolean;
};

const CHANNEL_TYPES: { value: IntakeChannelType; label: string }[] = [
  { value: "voice", label: "Voice" },
  { value: "web_portal", label: "Web portal" },
  { value: "dispatcher", label: "Dispatcher" },
  { value: "csv", label: "CSV" },
  { value: "edi", label: "EDI" },
  { value: "api_partner", label: "API partner" },
];
