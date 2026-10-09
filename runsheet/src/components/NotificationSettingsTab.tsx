import {
  AlertTriangle,
  Check,
  ChevronRight,
  Edit3,
  Eye,
  FileText,
  Mail,
  MessageSquare,
  Phone,
  Save,
  Search,
  Shield,
  ToggleLeft,
  ToggleRight,
  Users,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  getNotificationPreferences,
  getNotificationRules,
  getNotificationTemplates,
  type NotificationChannel,
  type NotificationPreference,
  type NotificationRule,
  type NotificationTemplate,
  type PreferenceUpsertPayload,
  updateNotificationRule,
  updateNotificationTemplate,
  upsertNotificationPreference,
} from "../services/notificationApi";
import {
  type Column,
  DataTable,
  Field,
  FormDialog,
  INPUT_CLASS,
  type Tab,
} from "./ui";

// ─── Types ───────────────────────────────────────────────────────────────────

type SettingsSubTab = "rules" | "preferences" | "templates";

const SUB_TABS: Tab[] = [
  { id: "rules", label: "Rules", icon: <Shield className="w-4 h-4" /> },
  {
    id: "preferences",
    label: "Preferences",
    icon: <Users className="w-4 h-4" />,
  },
  {
    id: "templates",
    label: "Templates",
    icon: <FileText className="w-4 h-4" />,
  },
];

const ALL_CHANNELS: NotificationChannel[] = ["sms", "email", "whatsapp"];

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getTypeLabel(type: string) {
  return type
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function getChannelIcon(channel: string) {
  switch (channel) {
    case "sms":
      return <Phone className="w-3.5 h-3.5" />;
    case "email":
      return <Mail className="w-3.5 h-3.5" />;
    case "whatsapp":
      return <MessageSquare className="w-3.5 h-3.5" />;
    default:
      return null;
  }
}

function getChannelLabel(channel: string) {
  switch (channel) {
    case "sms":
      return "SMS";
    case "email":
      return "Email";
    case "whatsapp":
      return "WhatsApp";
    default:
      return channel;
  }
}

/** Sample placeholder values for live template preview */
const SAMPLE_PLACEHOLDERS: Record<string, string> = {
  customer_name: "John Doe",
  order_id: "ORD-12345",
  job_id: "JOB-67890",
  new_eta: "2:30 PM",
  previous_eta: "1:00 PM",
  delay_minutes: "45",
  previous_status: "in_transit",
  new_status: "completed",
  delivery_date: "Jan 15, 2025",
  driver_name: "James K.",
  vehicle_id: "KBZ-001",
  shipment_id: "SHP-54321",
};

/** Render a template string by replacing {placeholder} with sample values */
function renderPreview(template: string): string {
  return template.replace(/\{(\w+)\}/g, (match, key) => {
    return SAMPLE_PLACEHOLDERS[key] ?? match;
  });
}

// ─── Main Component ──────────────────────────────────────────────────────────

/**
 * NotificationSettingsTab — settings management for notification rules,
 * customer preferences, and message templates.
 *
 * Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8
 */
export default function NotificationSettingsTab() {
  const [activeSubTab, setActiveSubTab] = useState<SettingsSubTab>("rules");

  return (
    <div className="flex flex-1 flex-col overflow-hidden bg-surface">
      <h2 className="sr-only">Notification Settings</h2>
      {/* One 44 px row (task 3.8) instead of a nested header and tab bar. */}
      <div
        role="tablist"
        aria-label="Notification settings sections"
        className="flex h-11 shrink-0 items-center gap-1 border-b border-slate-200 px-4"
      >
        {SUB_TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={activeSubTab === t.id}
            onClick={() => setActiveSubTab(t.id as SettingsSubTab)}
            className={`h-7 rounded-full border px-2.5 text-xs font-semibold focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
              activeSubTab === t.id
                ? "border-primary bg-primary-soft text-brand-800"
                : "border-slate-300 bg-surface text-slate-700 hover:bg-slate-50"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Sub-tab content */}
      {activeSubTab === "rules" && <RulesSection />}
      {activeSubTab === "preferences" && <PreferencesSection />}
      {activeSubTab === "templates" && <TemplatesSection />}
    </div>
  );
}

// ─── Rules Section ───────────────────────────────────────────────────────────

function RulesSection() {
  const [rules, setRules] = useState<NotificationRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [ruleErrors, setRuleErrors] = useState<Record<string, string>>({});

  const loadRules = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await getNotificationRules();
      setRules(response.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load rules");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRules();
  }, [loadRules]);

  const handleToggle = async (rule: NotificationRule) => {
    const previousEnabled = rule.enabled;
    const newEnabled = !rule.enabled;

    // Optimistic update
    setRules((prev) =>
      prev.map((r) =>
        r.rule_id === rule.rule_id ? { ...r, enabled: newEnabled } : r,
      ),
    );
    // Clear any previous error for this rule
    setRuleErrors((prev) => {
      const next = { ...prev };
      delete next[rule.rule_id];
      return next;
    });

    try {
      await updateNotificationRule(rule.rule_id, { enabled: newEnabled });
    } catch (err) {
      // Revert on failure
      setRules((prev) =>
        prev.map((r) =>
          r.rule_id === rule.rule_id ? { ...r, enabled: previousEnabled } : r,
        ),
      );
      setRuleErrors((prev) => ({
        ...prev,
        [rule.rule_id]:
          err instanceof Error ? err.message : "Failed to update rule",
      }));
    }
  };

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center py-16">
        <div className="text-center">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary mx-auto mb-3" />
          <p className="text-sm text-gray-500">Loading rules...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 px-8 py-8">
        <div className="bg-error-light text-error-dark px-4 py-3 rounded-xl text-sm">
          {error}
          <button
            onClick={loadRules}
            className="ml-3 underline hover:no-underline"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="px-8 py-6">
        <p className="text-sm text-gray-500 mb-6">
          Control which event types trigger customer notifications. Disabling a
          rule stops all notifications for that event type.
        </p>

        <div className="space-y-3">
          {rules.map((rule) => (
            <div
              key={rule.rule_id}
              className="border border-gray-200 rounded-xl p-5 hover:border-gray-300 transition-colors"
            >
              <div className="flex items-center justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-3 mb-2">
                    <span className="text-sm font-semibold text-primary">
                      {getTypeLabel(rule.event_type)}
                    </span>
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                        rule.enabled
                          ? "bg-success-light text-success-dark"
                          : "bg-gray-100 text-gray-500"
                      }`}
                    >
                      {rule.enabled ? "Active" : "Disabled"}
                    </span>
                  </div>

                  <div className="flex items-center gap-4 text-sm text-gray-500">
                    <div className="flex items-center gap-1.5">
                      <span className="text-gray-500">Channels:</span>
                      <div className="flex gap-1.5">
                        {rule.default_channels.map((ch) => (
                          <span
                            key={ch}
                            className="inline-flex items-center gap-1 px-2 py-0.5 bg-gray-100 rounded text-xs text-gray-700"
                          >
                            {getChannelIcon(ch)}
                            {getChannelLabel(ch)}
                          </span>
                        ))}
                      </div>
                    </div>
                    {rule.template_id && (
                      <span className="text-gray-500">
                        Template: {rule.template_id}
                      </span>
                    )}
                  </div>
                </div>

                {/* Toggle */}
                <button
                  onClick={() => handleToggle(rule)}
                  className="ml-4 flex-shrink-0 focus:outline-none"
                  aria-label={`${rule.enabled ? "Disable" : "Enable"} ${getTypeLabel(rule.event_type)} notifications`}
                >
                  {rule.enabled ? (
                    <ToggleRight className="w-10 h-10 text-success" />
                  ) : (
                    <ToggleLeft className="w-10 h-10 text-gray-500" />
                  )}
                </button>
              </div>

              {/* Error message for this rule */}
              {ruleErrors[rule.rule_id] && (
                <div className="mt-3 bg-error-light text-error-dark px-3 py-2 rounded-lg text-sm flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                  {ruleErrors[rule.rule_id]}
                </div>
              )}
            </div>
          ))}
        </div>

        {rules.length === 0 && (
          <div className="text-center py-16 text-gray-500">
            <Shield className="w-16 h-16 mx-auto mb-4 text-gray-300" />
            <p className="text-lg font-medium text-gray-500">
              No notification rules found
            </p>
            <p className="text-sm text-gray-500 mt-1">
              Rules will be created automatically when the system initializes
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Preferences Section ─────────────────────────────────────────────────────

function PreferencesSection() {
  const [preferences, setPreferences] = useState<NotificationPreference[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [selectedPreference, setSelectedPreference] =
    useState<NotificationPreference | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saveSuccess, setSaveSuccess] = useState(false);

  // Editable form state
  const [editChannels, setEditChannels] = useState<Record<string, string>>({});
  const [editEventPrefs, setEditEventPrefs] = useState<
    { event_type: string; enabled_channels: NotificationChannel[] }[]
  >([]);

  const loadPreferences = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await getNotificationPreferences({
        search: searchTerm.trim() || undefined,
        size: 100,
      });
      setPreferences(response.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load preferences",
      );
    } finally {
      setLoading(false);
    }
  }, [searchTerm]);

  useEffect(() => {
    loadPreferences();
  }, [loadPreferences]);

  const handleSelectPreference = (pref: NotificationPreference) => {
    setSelectedPreference(pref);
    setSaveError("");
    setSaveSuccess(false);
    // Initialize editable form state from the preference
    setEditChannels({ ...pref.channels });
    setEditEventPrefs(
      pref.event_preferences.map((ep) => ({
        event_type: ep.event_type,
        enabled_channels: [...ep.enabled_channels],
      })),
    );
  };

  const handleChannelChange = (channel: string, value: string) => {
    setEditChannels((prev) => ({ ...prev, [channel]: value }));
  };

  const handleEventChannelToggle = (
    eventType: string,
    channel: NotificationChannel,
  ) => {
    setEditEventPrefs((prev) =>
      prev.map((ep) => {
        if (ep.event_type !== eventType) return ep;
        const has = ep.enabled_channels.includes(channel);
        return {
          ...ep,
          enabled_channels: has
            ? ep.enabled_channels.filter((c) => c !== channel)
            : [...ep.enabled_channels, channel],
        };
      }),
    );
  };

  const handleSave = async () => {
    if (!selectedPreference) return;
    setSaving(true);
    setSaveError("");
    setSaveSuccess(false);

    const payload: PreferenceUpsertPayload = {
      customer_name: selectedPreference.customer_name,
      channels: editChannels,
      event_preferences: editEventPrefs,
    };

    try {
      const updated = await upsertNotificationPreference(
        selectedPreference.customer_id,
        payload,
      );
      // Update in list
      setPreferences((prev) =>
        prev.map((p) =>
          p.preference_id === updated.preference_id ? updated : p,
        ),
      );
      setSelectedPreference(updated);
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "Failed to save preference",
      );
    } finally {
      setSaving(false);
    }
  };

  // Summarize channels for list display
  const getChannelSummary = (pref: NotificationPreference) => {
    return Object.keys(pref.channels).filter((ch) => pref.channels[ch]);
  };

  // Summarize event types for list display
  const getEventSummary = (pref: NotificationPreference) => {
    return pref.event_preferences
      .filter((ep) => ep.enabled_channels.length > 0)
      .map((ep) => ep.event_type);
  };

  return (
    <div className="flex-1 flex overflow-hidden">
      {/* List panel */}
      <div className="flex-1 flex flex-col border-r border-gray-100">
        {/* Search */}
        <div className="px-8 py-4 border-b border-gray-100">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-500" />
            <input
              type="text"
              placeholder="Search customers..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full pl-10 pr-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            />
          </div>
        </div>

        {/* Error */}
        {error && (
          <div className="mx-8 mt-4 bg-error-light text-error-dark px-4 py-3 rounded-xl text-sm">
            {error}
            <button
              onClick={loadPreferences}
              className="ml-3 underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        )}

        {/* List */}
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="flex items-center justify-center py-16">
              <div className="text-center">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary mx-auto mb-3" />
                <p className="text-sm text-gray-500">Loading preferences...</p>
              </div>
            </div>
          ) : preferences.length === 0 ? (
            <div className="text-center py-16 text-gray-500">
              <Users className="w-16 h-16 mx-auto mb-4 text-gray-300" />
              <p className="text-lg font-medium text-gray-500">
                No customer preferences found
              </p>
              <p className="text-sm text-gray-500 mt-1">
                {searchTerm
                  ? "Try adjusting your search"
                  : "Preferences will appear when customers are configured"}
              </p>
            </div>
          ) : (
            <div className="divide-y divide-gray-100">
              {preferences.map((pref) => {
                const channels = getChannelSummary(pref);
                const events = getEventSummary(pref);
                return (
                  <button
                    key={pref.preference_id}
                    onClick={() => handleSelectPreference(pref)}
                    className={`w-full text-left px-8 py-4 hover:bg-gray-50 transition-colors ${
                      selectedPreference?.preference_id === pref.preference_id
                        ? "bg-gray-50"
                        : ""
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm font-medium text-primary">
                          {pref.customer_name}
                        </p>
                        <p className="text-xs text-gray-500 mt-0.5">
                          {pref.customer_id}
                        </p>
                      </div>
                      <ChevronRight className="w-4 h-4 text-gray-500" />
                    </div>
                    <div className="flex gap-3 mt-2">
                      <div className="flex gap-1">
                        {channels.map((ch) => (
                          <span
                            key={ch}
                            className="inline-flex items-center gap-1 px-2 py-0.5 bg-gray-100 rounded text-xs text-gray-600"
                          >
                            {getChannelIcon(ch)}
                            {getChannelLabel(ch)}
                          </span>
                        ))}
                        {channels.length === 0 && (
                          <span className="text-xs text-gray-500">
                            No channels
                          </span>
                        )}
                      </div>
                    </div>
                    {events.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {events.map((evt) => (
                          <span
                            key={evt}
                            className="px-2 py-0.5 bg-info-light text-info-dark rounded text-xs"
                          >
                            {getTypeLabel(evt)}
                          </span>
                        ))}
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Detail / Edit panel */}
      {selectedPreference ? (
        <div className="w-[420px] flex flex-col bg-gray-50">
          <div className="px-6 py-4 border-b border-gray-100">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-semibold text-primary">
                  {selectedPreference.customer_name}
                </h3>
                <p className="text-xs text-gray-500">
                  {selectedPreference.customer_id}
                </p>
              </div>
              <button
                onClick={() => {
                  setSelectedPreference(null);
                  setSaveError("");
                  setSaveSuccess(false);
                }}
                className="text-gray-500 hover:text-primary p-2 rounded-lg hover:bg-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-6 space-y-6">
            {/* Channel Details */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-3">
                Channel Details
              </label>
              <div className="space-y-3">
                {ALL_CHANNELS.map((ch) => (
                  <div key={ch}>
                    <label className="flex items-center gap-2 text-xs font-medium text-gray-500 mb-1">
                      {getChannelIcon(ch)}
                      {getChannelLabel(ch)}
                    </label>
                    <input
                      type="text"
                      value={editChannels[ch] || ""}
                      onChange={(e) => handleChannelChange(ch, e.target.value)}
                      placeholder={
                        ch === "email" ? "user@example.com" : "+254 7XX XXX XXX"
                      }
                      className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
                    />
                  </div>
                ))}
              </div>
            </div>

            {/* Per-event channel selections */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-3">
                Event Notifications
              </label>
              <div className="space-y-4">
                {editEventPrefs.map((ep) => (
                  <div
                    key={ep.event_type}
                    className="bg-white border border-gray-200 rounded-lg p-3"
                  >
                    <p className="text-sm font-medium text-primary mb-2">
                      {getTypeLabel(ep.event_type)}
                    </p>
                    <div className="flex gap-2">
                      {ALL_CHANNELS.map((ch) => {
                        const isEnabled = ep.enabled_channels.includes(ch);
                        return (
                          <button
                            key={ch}
                            onClick={() =>
                              handleEventChannelToggle(ep.event_type, ch)
                            }
                            className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                              isEnabled
                                ? "bg-primary text-white"
                                : "bg-gray-100 text-gray-500 hover:bg-gray-200"
                            }`}
                          >
                            {getChannelIcon(ch)}
                            {getChannelLabel(ch)}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ))}
                {editEventPrefs.length === 0 && (
                  <p className="text-sm text-gray-500">
                    No event preferences configured
                  </p>
                )}
              </div>
            </div>

            {/* Save error */}
            {saveError && (
              <div className="bg-error-light text-error-dark px-3 py-2 rounded-lg text-sm flex items-center gap-2">
                <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                {saveError}
              </div>
            )}

            {/* Save success */}
            {saveSuccess && (
              <div className="bg-success-light text-success-dark px-3 py-2 rounded-lg text-sm flex items-center gap-2">
                <Check className="w-4 h-4 flex-shrink-0" />
                Preferences saved successfully
              </div>
            )}
          </div>

          {/* Save button */}
          <div className="px-6 py-4 border-t border-gray-100">
            <button
              onClick={handleSave}
              disabled={saving}
              className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm text-white rounded-lg transition-colors disabled:opacity-50 bg-primary hover:bg-primary-hover"
            >
              <Save className="w-4 h-4" />
              {saving ? "Saving..." : "Save Preferences"}
            </button>
          </div>
        </div>
      ) : (
        <div className="w-[420px] flex items-center justify-center bg-gray-50 text-gray-500">
          <div className="text-center">
            <Edit3 className="w-12 h-12 mx-auto mb-3 text-gray-300" />
            <p className="text-sm font-medium">Select a customer</p>
            <p className="text-xs mt-1">
              Click a customer to edit their preferences
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Templates Section ───────────────────────────────────────────────────────

function TemplatesSection() {
  const [templates, setTemplates] = useState<NotificationTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<NotificationTemplate | null>(null);

  const loadTemplates = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await getNotificationTemplates();
      setTemplates(response.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load templates");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadTemplates();
  }, [loadTemplates]);

  const columns: Column<NotificationTemplate>[] = [
    {
      key: "event_type",
      header: "Event",
      width: 200,
      className: "font-medium text-text",
      cell: (t) => getTypeLabel(t.event_type),
    },
    {
      key: "channel",
      header: "Channel",
      width: 140,
      cell: (t) => (
        <span className="inline-flex items-center gap-1.5 text-slate-700">
          {getChannelIcon(t.channel)}
          {getChannelLabel(t.channel)}
        </span>
      ),
    },
    {
      key: "body_template",
      header: "Message",
      truncate: true,
      title: (t) => t.body_template,
      className: "text-slate-700",
      cell: (t) => t.body_template,
    },
  ];

  return (
    <div className="min-h-0 flex-1 overflow-auto">
      <DataTable<NotificationTemplate>
        ariaLabel="Notification templates"
        columns={columns}
        data={loading || error ? [] : templates}
        loading={loading}
        error={error ? { message: error, onRetry: loadTemplates } : null}
        getRowId={(t) => t.template_id}
        rowLabel={(t) =>
          `${getTypeLabel(t.event_type)} ${getChannelLabel(t.channel)}`
        }
        onRowClick={setEditing}
        rowMenu={(t) => [
          {
            id: "edit",
            label: "Edit template",
            icon: <Edit3 className="h-3.5 w-3.5" />,
            onSelect: () => setEditing(t),
          },
        ]}
        emptyState={
          <div className="text-text-muted">
            <p className="text-sm font-medium">No templates found</p>
            <p className="mt-1 text-xs">
              Templates are created when the system initialises.
            </p>
          </div>
        }
      />
      {editing && (
        <TemplateDialog
          template={editing}
          onClose={() => setEditing(null)}
          onSaved={(updated) =>
            setTemplates((prev) =>
              prev.map((t) =>
                t.template_id === updated.template_id ? updated : t,
              ),
            )
          }
        />
      )}
    </div>
  );
}

type TemplateValues = { subject: string; body: string };

/** Edit a notification template (task 3.8, design.md §5: lg FormDialog). */
export function TemplateDialog({
  template,
  onClose,
  onSaved,
}: {
  template: NotificationTemplate;
  onClose: () => void;
  onSaved: (updated: NotificationTemplate) => void;
}) {
  return (
    <FormDialog<TemplateValues, NotificationTemplate>
      open
      size="lg"
      title="Edit template"
      help={`${getTypeLabel(template.event_type)} · ${getChannelLabel(template.channel)}`}
      submitLabel="Save template"
      successMessage="Template saved"
      initialValues={{
        subject: template.subject_template || "",
        body: template.body_template,
      }}
      validate={(v) => ({
        body: v.body.trim() ? undefined : "Enter the message body.",
      })}
      onSubmit={(v) =>
        updateNotificationTemplate(template.template_id, {
          subject_template: v.subject || undefined,
          body_template: v.body,
        })
      }
      onSaved={onSaved}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field label="Subject" help="Email only; leave blank for SMS">
            <input
              id="tmpl-subject"
              type="text"
              value={values.subject}
              onChange={(e) => set("subject", e.target.value)}
              placeholder="e.g. Delivery update for {order_id}"
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="Message" required error={errors.body}>
            <textarea
              id="tmpl-body"
              value={values.body}
              onChange={(e) => set("body", e.target.value)}
              rows={6}
              className={`${INPUT_CLASS} h-auto py-1.5 font-mono`}
            />
          </Field>
          {template.placeholders.length > 0 && (
            <div className="col-span-2">
              <p className="mb-1 text-xs font-medium text-slate-700">
                Placeholders
              </p>
              <div className="flex flex-wrap gap-1.5">
                {template.placeholders.map((ph) => (
                  <button
                    key={ph}
                    type="button"
                    onClick={() => set("body", `${values.body}{${ph}}`)}
                    className="rounded border border-slate-200 bg-slate-50 px-2 py-0.5 font-mono text-xs text-slate-800 hover:bg-slate-100"
                    aria-label={`Insert placeholder ${ph}`}
                  >
                    {`{${ph}}`}
                  </button>
                ))}
              </div>
            </div>
          )}
          <section className="col-span-2" aria-label="Live preview">
            <p className="mb-1 flex items-center gap-1.5 text-xs font-medium text-slate-700">
              <Eye aria-hidden="true" className="h-3.5 w-3.5" />
              Live preview
            </p>
            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
              {values.subject && (
                <p className="mb-2 text-sm font-semibold text-text">
                  {renderPreview(values.subject)}
                </p>
              )}
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-800">
                {values.body ? (
                  renderPreview(values.body)
                ) : (
                  <span className="italic text-text-muted">
                    Enter a message to see the preview
                  </span>
                )}
              </p>
            </div>
          </section>
        </>
      )}
    </FormDialog>
  );
}
