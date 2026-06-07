"use client";

import {
  AlertCircle,
  Check,
  Copy,
  Eye,
  EyeOff,
  Plus,
  RefreshCw,
  Trash2,
  Webhook,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  createIntakeChannel,
  deleteIntakeChannel,
  getIntakeChannels,
  type IntakeChannel,
  rotateChannelSecret,
  updateIntakeChannel,
} from "../../services/adminApi";
import { Badge, Button, type Column, Modal, PageHeader, Table } from "../ui";

// ─── Helper Functions ────────────────────────────────────────────────────────

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function copyToClipboard(text: string): void {
  navigator.clipboard.writeText(text);
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function OrderWebhooksAdmin() {
  const [channels, setChannels] = useState<IntakeChannel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showSecretModal, setShowSecretModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);

  // Form states
  const [channelName, setChannelName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [adapterType, setAdapterType] = useState("generic_json");
  const [enabled, setEnabled] = useState(true);

  // Secret display
  const [currentSecret, setCurrentSecret] = useState<string | null>(null);
  const [currentChannelId, setCurrentChannelId] = useState<string | null>(null);
  const [secretVisible, setSecretVisible] = useState(false);
  const [secretCopied, setSecretCopied] = useState(false);

  // Delete confirmation
  const [channelToDelete, setChannelToDelete] = useState<IntakeChannel | null>(
    null,
  );

  const fetchChannels = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getIntakeChannels();
      setChannels(response.items);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load intake channels",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchChannels();
  }, [fetchChannels]);

  // ─── Create Channel ────────────────────────────────────────────────────────

  const handleCreateChannel = async () => {
    if (!channelName.trim()) {
      setError("Channel ID is required");
      return;
    }
    if (!displayName.trim()) {
      setError("Display name is required");
      return;
    }

    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      const newChannel = await createIntakeChannel({
        channel_id: channelName,
        channel_type: adapterType,
        display_name: displayName,
        supported_schema_versions: ["1.0"],
        enabled,
      });

      // Show the secret modal with the newly created secret
      // Backend returns the plaintext secret in 'hmac_secret' field on create only
      setCurrentChannelId(newChannel.channel_id);
      setCurrentSecret(newChannel.hmac_secret);
      setShowCreateModal(false);
      setShowSecretModal(true);

      // Reset form
      setChannelName("");
      setDisplayName("");
      setAdapterType("generic_json");
      setEnabled(true);

      // Refresh list
      await fetchChannels();
      setSuccess("Intake channel created successfully");
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to create intake channel",
      );
    } finally {
      setLoading(false);
    }
  };

  // ─── Toggle Channel ────────────────────────────────────────────────────────

  const handleToggleChannel = async (channel: IntakeChannel) => {
    setError(null);
    setSuccess(null);

    try {
      await updateIntakeChannel(channel.channel_id, {
        enabled: !channel.enabled,
      });
      await fetchChannels();
      setSuccess(
        `Channel ${channel.enabled ? "disabled" : "enabled"} successfully`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update channel");
    }
  };

  // ─── Rotate Secret ─────────────────────────────────────────────────────────

  const handleRotateSecret = async (channelId: string) => {
    setError(null);
    setSuccess(null);
    setLoading(true);

    try {
      const response = await rotateChannelSecret(channelId);
      setCurrentChannelId(channelId);
      setCurrentSecret(response.new_secret);
      setShowSecretModal(true);
      setSuccess("HMAC secret rotated successfully");
      await fetchChannels();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to rotate secret");
    } finally {
      setLoading(false);
    }
  };

  // ─── Delete Channel ────────────────────────────────────────────────────────

  const handleDeleteChannel = async () => {
    if (!channelToDelete) return;

    setError(null);
    setSuccess(null);
    setLoading(true);

    try {
      await deleteIntakeChannel(channelToDelete.channel_id);
      setShowDeleteModal(false);
      setChannelToDelete(null);
      await fetchChannels();
      setSuccess("Intake channel deleted successfully");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete channel");
    } finally {
      setLoading(false);
    }
  };

  // ─── Copy Secret ───────────────────────────────────────────────────────────

  const handleCopySecret = () => {
    if (currentSecret) {
      copyToClipboard(currentSecret);
      setSecretCopied(true);
      setTimeout(() => setSecretCopied(false), 2000);
    }
  };

  // ─── Table Columns ─────────────────────────────────────────────────────────

  const columns: Column<IntakeChannel>[] = [
    {
      key: "channel_id",
      label: "Channel ID",
      render: (channel) => (
        <div className="font-mono text-sm">{channel.channel_id}</div>
      ),
    },
    {
      key: "channel_type",
      label: "Channel Type",
      render: (channel) => (
        <Badge variant="default">{channel.channel_type}</Badge>
      ),
    },
    {
      key: "enabled",
      label: "Status",
      render: (channel) =>
        channel.enabled ? (
          <Badge variant="success">Enabled</Badge>
        ) : (
          <Badge variant="default">Disabled</Badge>
        ),
    },
    {
      key: "webhook_count",
      label: "Webhook Count",
      render: (channel) => (
        <span className="text-gray-700">{channel.webhook_count ?? 0}</span>
      ),
    },
    {
      key: "last_webhook_at",
      label: "Last Webhook",
      render: (channel) => (
        <span className="text-sm text-gray-600">
          {channel.last_webhook_at
            ? formatDate(channel.last_webhook_at)
            : "Never"}
        </span>
      ),
    },
    {
      key: "secret_version",
      label: "Secret Version",
      render: (channel) => (
        <span className="text-sm text-gray-700">v{channel.secret_version}</span>
      ),
    },
    {
      key: "created_at",
      label: "Created",
      render: (channel) => (
        <span className="text-sm text-gray-600">
          {formatDate(channel.created_at)}
        </span>
      ),
    },
    {
      key: "actions",
      label: "Actions",
      render: (channel) => (
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => handleToggleChannel(channel)}
            className="text-sm text-blue-600 hover:text-blue-800"
            title={channel.enabled ? "Disable" : "Enable"}
          >
            {channel.enabled ? "Disable" : "Enable"}
          </button>
          <button
            type="button"
            onClick={() => handleRotateSecret(channel.channel_id)}
            className="text-sm text-purple-600 hover:text-purple-800"
            title="Rotate Secret"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
          <button
            type="button"
            onClick={() => {
              setChannelToDelete(channel);
              setShowDeleteModal(true);
            }}
            className="text-sm text-red-600 hover:text-red-800"
            title="Delete"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ),
    },
  ];

  return (
    <div className="p-6">
      <PageHeader
        title="Order Webhook Channels"
        subtitle="Manage intake channels for external order webhooks"
        icon={<Webhook className="w-5 h-5" />}
      />

      {/* Info Banner */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-6 flex items-start gap-3">
        <AlertCircle className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
        <div className="text-sm text-blue-900">
          <p className="font-medium mb-1">About Intake Channels</p>
          <p>
            Intake channels allow external systems to send orders via webhooks.
            Each channel has a unique HMAC secret for request authentication.
            Secrets are shown only once upon creation or rotation.
          </p>
        </div>
      </div>

      {/* Error/Success Messages */}
      {error && (
        <div className="bg-error-light border border-error text-error-dark p-3 rounded-lg mb-4 flex items-center gap-2">
          <AlertCircle className="w-5 h-5 flex-shrink-0" />
          <span className="text-sm">{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="ml-auto"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {success && (
        <div className="bg-success-light border border-success text-success-dark p-3 rounded-lg mb-4 flex items-center gap-2">
          <Check className="w-5 h-5 flex-shrink-0" />
          <span className="text-sm">{success}</span>
          <button
            type="button"
            onClick={() => setSuccess(null)}
            className="ml-auto"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center justify-between mb-6">
        <div className="text-sm text-gray-600">
          {channels.length} channel{channels.length !== 1 ? "s" : ""} configured
        </div>
        <div className="flex gap-3">
          <Button variant="ghost" onClick={fetchChannels} disabled={loading}>
            <RefreshCw className="w-4 h-4 mr-2" />
            Refresh
          </Button>
          <Button onClick={() => setShowCreateModal(true)}>
            <Plus className="w-4 h-4 mr-2" />
            Create Channel
          </Button>
        </div>
      </div>

      {/* Loading State */}
      {loading && channels.length === 0 && (
        <div className="flex justify-center py-12">
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      )}

      {/* Channels Table */}
      {!loading && channels.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-200 p-12 text-center">
          <Webhook className="w-12 h-12 text-gray-500 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-gray-900 mb-2">
            No Intake Channels
          </h3>
          <p className="text-gray-600 mb-6">
            Create your first intake channel to start receiving webhook orders
          </p>
          <Button onClick={() => setShowCreateModal(true)}>
            <Plus className="w-4 h-4 mr-2" />
            Create Channel
          </Button>
        </div>
      ) : (
        <Table columns={columns} data={channels} />
      )}

      {/* Create Channel Modal */}
      <Modal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        title="Create Intake Channel"
      >
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Channel ID
            </label>
            <input
              type="text"
              value={channelName}
              onChange={(e) => setChannelName(e.target.value)}
              placeholder="e.g., shopify-store-1"
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
            />
            <p className="text-xs text-gray-500 mt-1">
              Unique identifier (3-64 characters, lowercase, hyphens allowed)
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Display Name
            </label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="e.g., Shopify Store 1"
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Channel Type
            </label>
            <select
              value={adapterType}
              onChange={(e) => setAdapterType(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
            >
              <option value="generic_json">Generic JSON</option>
              <option value="shopify">Shopify</option>
              <option value="woocommerce">WooCommerce</option>
              <option value="square">Square</option>
              <option value="stripe">Stripe</option>
            </select>
          </div>

          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="enabled"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="w-4 h-4 text-primary border-gray-300 rounded focus:ring-primary"
            />
            <label htmlFor="enabled" className="text-sm text-gray-700">
              Enable channel immediately
            </label>
          </div>

          <div className="flex justify-end gap-3 pt-4">
            <Button
              variant="ghost"
              onClick={() => setShowCreateModal(false)}
              disabled={loading}
            >
              Cancel
            </Button>
            <Button onClick={handleCreateChannel} disabled={loading}>
              {loading ? "Creating..." : "Create Channel"}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Secret Display Modal */}
      <Modal
        isOpen={showSecretModal}
        onClose={() => {
          setShowSecretModal(false);
          setCurrentSecret(null);
          setCurrentChannelId(null);
          setSecretVisible(false);
          setSecretCopied(false);
        }}
        title="HMAC Secret"
      >
        <div className="space-y-4">
          <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 flex items-start gap-3">
            <AlertCircle className="w-5 h-5 text-yellow-600 flex-shrink-0 mt-0.5" />
            <div className="text-sm text-yellow-900">
              <p className="font-medium mb-1">Important: Save This Secret</p>
              <p>
                This secret will only be shown once. Copy it now and store it
                securely. You'll need it to authenticate webhook requests.
              </p>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Channel ID
            </label>
            <div className="font-mono text-sm bg-gray-50 p-3 rounded border border-gray-200">
              {currentChannelId}
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              HMAC Secret
            </label>
            <div className="relative">
              <div className="font-mono text-sm bg-gray-50 p-3 rounded border border-gray-200 pr-20">
                {secretVisible ? currentSecret : "••••••••••••••••••••••••"}
              </div>
              <div className="absolute right-2 top-2 flex gap-2">
                <button
                  type="button"
                  onClick={() => setSecretVisible(!secretVisible)}
                  className="text-gray-600 hover:text-gray-800"
                  title={secretVisible ? "Hide" : "Show"}
                >
                  {secretVisible ? (
                    <EyeOff className="w-4 h-4" />
                  ) : (
                    <Eye className="w-4 h-4" />
                  )}
                </button>
                <button
                  type="button"
                  onClick={handleCopySecret}
                  className="text-gray-600 hover:text-gray-800"
                  title="Copy"
                >
                  {secretCopied ? (
                    <Check className="w-4 h-4 text-green-600" />
                  ) : (
                    <Copy className="w-4 h-4" />
                  )}
                </button>
              </div>
            </div>
          </div>

          <div className="flex justify-end pt-4">
            <Button
              onClick={() => {
                setShowSecretModal(false);
                setCurrentSecret(null);
                setCurrentChannelId(null);
                setSecretVisible(false);
                setSecretCopied(false);
              }}
            >
              I've Saved the Secret
            </Button>
          </div>
        </div>
      </Modal>

      {/* Delete Confirmation Modal */}
      <Modal
        isOpen={showDeleteModal}
        onClose={() => {
          setShowDeleteModal(false);
          setChannelToDelete(null);
        }}
        title="Delete Intake Channel"
      >
        <div className="space-y-4">
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3">
            <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
            <div className="text-sm text-red-900">
              <p className="font-medium mb-1">Warning: This Cannot Be Undone</p>
              <p>
                Deleting this channel will permanently remove it and invalidate
                its HMAC secret. Any webhooks sent to this channel will be
                rejected.
              </p>
            </div>
          </div>

          {channelToDelete && (
            <div>
              <p className="text-sm text-gray-700 mb-2">
                You are about to delete:
              </p>
              <div className="font-mono text-sm bg-gray-50 p-3 rounded border border-gray-200">
                {channelToDelete.channel_id}
              </div>
            </div>
          )}

          <div className="flex justify-end gap-3 pt-4">
            <Button
              variant="ghost"
              onClick={() => {
                setShowDeleteModal(false);
                setChannelToDelete(null);
              }}
              disabled={loading}
            >
              Cancel
            </Button>
            <Button
              onClick={handleDeleteChannel}
              disabled={loading}
              className="bg-red-600 hover:bg-red-700"
            >
              {loading ? "Deleting..." : "Delete Channel"}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
