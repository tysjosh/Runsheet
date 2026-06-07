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
  Loader2,
  Plus,
  RefreshCw,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { type Column, Modal, ModalFooter, Table } from "@/components/ui";
import { ApiError } from "../../services/api";
import {
  type CreateIntakeChannelPayload,
  createIntakeChannel,
  deleteIntakeChannel,
  type IntakeChannel,
  type IntakeChannelType,
  type IntakeChannelWithSecret,
  listIntakeChannels,
  rotateIntakeChannelSecret,
  updateIntakeChannel,
} from "../../services/intakeChannelsApi";

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

  // Create form
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [createForm, setCreateForm] = useState<CreateIntakeChannelPayload>({
    channel_id: "",
    channel_type: "api_partner",
    display_name: "",
    supported_schema_versions: ["1.0"],
    enabled: true,
  });
  const [createError, setCreateError] = useState<string | null>(null);

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

  const handleCreate = useCallback(async () => {
    if (!createForm.channel_id.trim() || !createForm.display_name.trim()) {
      setCreateError("Channel ID and display name are required");
      return;
    }
    setWorking("create");
    setCreateError(null);
    try {
      const result: IntakeChannelWithSecret =
        await createIntakeChannel(createForm);
      setSecretModal({
        isOpen: true,
        secret: result.hmac_secret,
        channelId: result.channel_id,
        action: "create",
      });
      setShowCreateForm(false);
      setCreateForm({
        channel_id: "",
        channel_type: "api_partner",
        display_name: "",
        supported_schema_versions: ["1.0"],
        enabled: true,
      });
      await fetchChannels();
    } catch (err) {
      setCreateError(
        err instanceof ApiError ? err.message : "Failed to create channel",
      );
    } finally {
      setWorking(null);
    }
  }, [createForm, fetchChannels]);

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

  const channelColumns: Column<IntakeChannel>[] = [
    {
      key: "channel_id",
      label: "Channel ID",
      className: "text-sm font-mono text-primary",
      render: (channel) => channel.channel_id,
    },
    {
      key: "display_name",
      label: "Name",
      className: "text-sm text-gray-700",
      render: (channel) => channel.display_name,
    },
    {
      key: "channel_type",
      label: "Type",
      className: "text-sm text-gray-700",
      render: (channel) => channel.channel_type.replace("_", " "),
    },
    {
      key: "status",
      label: "Status",
      render: (channel) => (
        <span
          className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${channel.enabled ? "bg-success-light text-success-dark" : "bg-gray-100 text-gray-600"}`}
        >
          {channel.enabled ? "Enabled" : "Disabled"}
        </span>
      ),
    },
    {
      key: "versions",
      label: "Versions",
      className: "text-xs text-gray-600",
      render: (channel) => channel.supported_schema_versions.join(", "),
    },
    {
      key: "actions",
      label: "Actions",
      align: "right",
      render: (channel) => (
        <div className="flex items-center justify-end gap-1">
          <button
            type="button"
            onClick={() => handleRotate(channel.channel_id)}
            disabled={working === channel.channel_id}
            className="p-1.5 rounded hover:bg-gray-100 text-gray-600"
            title="Rotate secret"
            aria-label={`Rotate secret for ${channel.channel_id}`}
          >
            <Key className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={() => handleToggleEnabled(channel)}
            disabled={working === channel.channel_id}
            className="px-2 py-1 text-xs border border-gray-200 rounded hover:bg-gray-50"
            aria-label={`${channel.enabled ? "Disable" : "Enable"} ${channel.channel_id}`}
          >
            {channel.enabled ? "Disable" : "Enable"}
          </button>
          <button
            type="button"
            onClick={() => setChannelToDelete(channel)}
            disabled={working === channel.channel_id}
            className="p-1.5 rounded hover:bg-error-light text-error"
            title="Delete channel"
            aria-label={`Delete ${channel.channel_id}`}
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-primary">
            Intake Channels
          </h2>
          <p className="text-sm text-gray-500 mt-0.5">
            Register and manage webhook intake channels for order ingestion.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={fetchChannels}
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-100 border border-gray-200"
            aria-label="Refresh channels"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
            />
          </button>
          <button
            type="button"
            onClick={() => setShowCreateForm(true)}
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white rounded-lg bg-primary hover:bg-primary-hover"
            aria-label="Register channel"
          >
            <Plus className="w-4 h-4" />
            Register Channel
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div
          role="alert"
          className="rounded-lg bg-error-light border border-error-light px-4 py-3 text-sm text-error-dark flex items-center gap-2"
        >
          <AlertTriangle className="w-4 h-4" />
          {error}
        </div>
      )}

      {/* Create Form */}
      {showCreateForm && (
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4 space-y-3">
          <h3 className="text-sm font-semibold text-primary">
            Register New Channel
          </h3>
          {createError && <p className="text-xs text-error">{createError}</p>}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div>
              <label
                htmlFor="ic-channel-id"
                className="block text-xs text-gray-600 mb-1"
              >
                Channel ID *
              </label>
              <input
                id="ic-channel-id"
                type="text"
                value={createForm.channel_id}
                onChange={(e) =>
                  setCreateForm((f) => ({ ...f, channel_id: e.target.value }))
                }
                placeholder="my-voice-provider"
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
              />
            </div>
            <div>
              <label
                htmlFor="ic-display-name"
                className="block text-xs text-gray-600 mb-1"
              >
                Display Name *
              </label>
              <input
                id="ic-display-name"
                type="text"
                value={createForm.display_name}
                onChange={(e) =>
                  setCreateForm((f) => ({ ...f, display_name: e.target.value }))
                }
                placeholder="Voice AI Provider"
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
              />
            </div>
            <div>
              <label
                htmlFor="ic-channel-type"
                className="block text-xs text-gray-600 mb-1"
              >
                Type
              </label>
              <select
                id="ic-channel-type"
                value={createForm.channel_type}
                onChange={(e) =>
                  setCreateForm((f) => ({
                    ...f,
                    channel_type: e.target.value as IntakeChannelType,
                  }))
                }
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
              >
                <option value="voice">Voice</option>
                <option value="web_portal">Web Portal</option>
                <option value="dispatcher">Dispatcher</option>
                <option value="csv">CSV</option>
                <option value="edi">EDI</option>
                <option value="api_partner">API Partner</option>
              </select>
            </div>
            <div className="flex items-end">
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={handleCreate}
                  disabled={working === "create"}
                  className="px-4 py-2 text-sm font-medium text-white rounded-lg disabled:opacity-50 bg-primary hover:bg-primary-hover"
                >
                  {working === "create" ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    "Create"
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setShowCreateForm(false)}
                  className="px-3 py-2 text-sm border border-gray-200 rounded-lg hover:bg-gray-50"
                >
                  Cancel
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Channels List */}
      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="w-5 h-5 text-gray-500 animate-spin" />
        </div>
      ) : channels.length === 0 ? (
        <div className="text-center py-12 text-gray-500">
          <p className="text-sm">No intake channels registered yet.</p>
        </div>
      ) : (
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
          <Table<IntakeChannel>
            ariaLabel="Intake channels list"
            columns={channelColumns}
            data={channels}
            getRowId={(channel) => channel.channel_id}
            variant="compact"
            emptyState={
              <span className="text-gray-500">
                No intake channels registered yet.
              </span>
            }
          />
        </div>
      )}

      {/* Secret Modal — shown exactly once on create + rotate */}
      {secretModal.isOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          role="dialog"
          aria-modal="true"
          aria-labelledby="secret-modal-title"
        >
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4 p-6">
            <div className="flex items-center gap-2 mb-4">
              <Key className="w-5 h-5 text-warning" />
              <h3
                id="secret-modal-title"
                className="text-lg font-semibold text-primary"
              >
                {secretModal.action === "create"
                  ? "Channel Created"
                  : "Secret Rotated"}
              </h3>
            </div>
            <div className="bg-warning-light border border-warning-light rounded-lg p-3 mb-4">
              <p className="text-xs text-warning-dark font-medium mb-1">
                ⚠️ This secret will only be shown once. Copy it now.
              </p>
            </div>
            <div className="flex items-center gap-2 mb-4">
              <code
                className="flex-1 bg-gray-100 rounded-lg px-3 py-2 text-sm font-mono text-primary break-all"
                data-testid="secret-value"
              >
                {secretModal.secret}
              </code>
              <button
                type="button"
                onClick={handleCopy}
                className="flex-shrink-0 inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium border border-gray-200 rounded-lg hover:bg-gray-50"
                aria-label="Copy to clipboard"
              >
                {copied ? (
                  <Check className="w-4 h-4 text-success" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <p className="text-xs text-gray-500 mb-4">
              Channel:{" "}
              <span className="font-mono">{secretModal.channelId}</span>
            </p>
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => setSecretModal((s) => ({ ...s, isOpen: false }))}
                className="px-4 py-2 text-sm font-medium text-white rounded-lg bg-primary hover:bg-primary-hover"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
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
        <div className="flex items-start gap-3">
          <AlertTriangle className="w-5 h-5 text-error flex-shrink-0 mt-0.5" />
          <div className="text-sm text-gray-700">
            <p className="mb-2">
              Are you sure you want to delete{" "}
              <span className="font-semibold text-primary">
                {channelToDelete?.display_name}
              </span>{" "}
              (<span className="font-mono">{channelToDelete?.channel_id}</span>
              )?
            </p>
            <p className="text-gray-500">
              This permanently removes the channel and invalidates its HMAC
              secret. This action cannot be undone.
            </p>
          </div>
        </div>
      </Modal>
    </div>
  );
}
