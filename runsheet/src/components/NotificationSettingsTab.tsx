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
  Settings,
  Shield,
  ToggleLeft,
  ToggleRight,
  Users,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getNotificationPreferences,
  getNotificationRules,
  getNotificationTemplates,
  updateNotificationRule,
  updateNotificationTemplate,
  upsertNotificationPreference,
  type NotificationChannel,
  type NotificationPreference,
  type NotificationRule,
  type NotificationTemplate,
  type PreferenceUpsertPayload,
} from "../services/notificationApi";

// ─── Types ───────────────────────────────────────────────────────────────────

type SettingsSubTab = "rules" | "preferences" | "templates";

const SUB_TABS: { key: SettingsSubTab; label: string; icon: React.ReactNode }[] = [
  { key: "rules", label: "Rules", icon: <Shield className="w-4 h-4" /> },
  { key: "preferences", label: "Preferences", icon: <Users className="w-4 h-4" /> },
  { key: "templates", label: "Templates", icon: <FileText className="w-4 h-4" /> },
];

const ALL_CHANNELS: NotificationChannel[] = ["sms", "email", "whatsapp"];

const EVENT_TYPES = [
  "delivery_confirmation",
  "delay_alert",
  "eta_change",
  "order_status_update",
];

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
    <div className="flex-1 flex flex-col bg-white overflow-hidden">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <Settings className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-[#232323]">
              Notification Settings
            </h1>
            <p className="text-gray-500">
              Manage rules, preferences, and templates
            </p>
          </div>
        </div>

        {/* Sub-tab navigation */}
        <nav className="flex gap-4" aria-label="Settings sub-tabs">
          {SUB_TABS.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActiveSubTab(tab.key)}
              className={`flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg transition-colors ${
                activeSubTab === tab.key
                  ? "bg-[#232323] text-white"
                  : "text-gray-600 hover:bg-gray-100"
              }`}
              aria-selected={activeSubTab === tab.key}
              role="tab"
            >
              {tab.icon}
              {tab.label}
            </button>
          ))}
        </nav>
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
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-[#232323] mx-auto mb-3" />
          <p className="text-sm text-gray-500">Loading rules...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 px-8 py-8">
        <div className="bg-red-50 text-red-700 px-4 py-3 rounded-xl text-sm">
          {error}
          <button onClick={loadRules} className="ml-3 underline hover:no-underline">
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
                    <span className="text-sm font-semibold text-[#232323]">
                      {getTypeLabel(rule.event_type)}
                    </span>
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                        rule.enabled
                          ? "bg-green-50 text-green-700"
                          : "bg-gray-100 text-gray-500"
                      }`}
                    >
                      {rule.enabled ? "Active" : "Disabled"}
                    </span>
                  </div>

                  <div className="flex items-center gap-4 text-sm text-gray-500">
                    <div className="flex items-center gap-1.5">
                      <span className="text-gray-400">Channels:</span>
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
                      <span className="text-gray-400">
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
                    <ToggleRight className="w-10 h-10 text-green-600" />
                  ) : (
                    <ToggleLeft className="w-10 h-10 text-gray-400" />
                  )}
                </button>
              </div>

              {/* Error message for this rule */}
              {ruleErrors[rule.rule_id] && (
                <div className="mt-3 bg-red-50 text-red-700 px-3 py-2 rounded-lg text-sm flex items-center gap-2">
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
            <p className="text-lg font-medium text-gray-400">
              No notification rules found
            </p>
            <p className="text-sm text-gray-400 mt-1">
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
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
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
          <div className="mx-8 mt-4 bg-red-50 text-red-700 px-4 py-3 rounded-xl text-sm">
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
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-[#232323] mx-auto mb-3" />
                <p className="text-sm text-gray-500">Loading preferences...</p>
              </div>
            </div>
          ) : preferences.length === 0 ? (
            <div className="text-center py-16 text-gray-500">
              <Users className="w-16 h-16 mx-auto mb-4 text-gray-300" />
              <p className="text-lg font-medium text-gray-400">
                No customer preferences found
              </p>
              <p className="text-sm text-gray-400 mt-1">
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
                        <p className="text-sm font-medium text-[#232323]">
                          {pref.customer_name}
                        </p>
                        <p className="text-xs text-gray-500 mt-0.5">
                          {pref.customer_id}
                        </p>
                      </div>
                      <ChevronRight className="w-4 h-4 text-gray-400" />
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
                          <span className="text-xs text-gray-400">
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
                            className="px-2 py-0.5 bg-blue-50 text-blue-700 rounded text-xs"
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
                <h3 className="font-semibold text-[#232323]">
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
                className="text-gray-400 hover:text-[#232323] p-2 rounded-lg hover:bg-white transition-colors"
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
                        ch === "email"
                          ? "user@example.com"
                          : "+254 7XX XXX XXX"
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
                    <p className="text-sm font-medium text-[#232323] mb-2">
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
                                ? "bg-[#232323] text-white"
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
                  <p className="text-sm text-gray-400">
                    No event preferences configured
                  </p>
                )}
              </div>
            </div>

            {/* Save error */}
            {saveError && (
              <div className="bg-red-50 text-red-700 px-3 py-2 rounded-lg text-sm flex items-center gap-2">
                <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                {saveError}
              </div>
            )}

            {/* Save success */}
            {saveSuccess && (
              <div className="bg-green-50 text-green-700 px-3 py-2 rounded-lg text-sm flex items-center gap-2">
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
              className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm text-white rounded-lg transition-colors hover:opacity-90 disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              <Save className="w-4 h-4" />
              {saving ? "Saving..." : "Save Preferences"}
            </button>
          </div>
        </div>
      ) : (
        <div className="w-[420px] flex items-center justify-center bg-gray-50 text-gray-400">
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
  const [selectedTemplate, setSelectedTemplate] =
    useState<NotificationTemplate | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saveSuccess, setSaveSuccess] = useState(false);

  // Editable form state
  const [editSubject, setEditSubject] = useState("");
  const [editBody, setEditBody] = useState("");

  const loadTemplates = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await getNotificationTemplates();
      setTemplates(response.items);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load templates",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadTemplates();
  }, [loadTemplates]);

  const handleSelectTemplate = (tmpl: NotificationTemplate) => {
    setSelectedTemplate(tmpl);
    setSaveError("");
    setSaveSuccess(false);
    setEditSubject(tmpl.subject_template || "");
    setEditBody(tmpl.body_template);
  };

  const handleSave = async () => {
    if (!selectedTemplate) return;
    setSaving(true);
    setSaveError("");
    setSaveSuccess(false);

    try {
      const updated = await updateNotificationTemplate(
        selectedTemplate.template_id,
        {
          subject_template: editSubject || undefined,
          body_template: editBody,
        },
      );
      setTemplates((prev) =>
        prev.map((t) =>
          t.template_id === updated.template_id ? updated : t,
        ),
      );
      setSelectedTemplate(updated);
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "Failed to save template",
      );
    } finally {
      setSaving(false);
    }
  };

  // Group templates by event_type
  const groupedTemplates = useMemo(() => {
    const groups: Record<string, NotificationTemplate[]> = {};
    for (const tmpl of templates) {
      if (!groups[tmpl.event_type]) {
        groups[tmpl.event_type] = [];
      }
      groups[tmpl.event_type].push(tmpl);
    }
    return groups;
  }, [templates]);

  // Live preview
  const subjectPreview = useMemo(
    () => (editSubject ? renderPreview(editSubject) : ""),
    [editSubject],
  );
  const bodyPreview = useMemo(
    () => (editBody ? renderPreview(editBody) : ""),
    [editBody],
  );

  return (
    <div className="flex-1 flex overflow-hidden">
      {/* List panel */}
      <div className="flex-1 flex flex-col border-r border-gray-100">
        {/* Error */}
        {error && (
          <div className="mx-8 mt-4 bg-red-50 text-red-700 px-4 py-3 rounded-xl text-sm">
            {error}
            <button
              onClick={loadTemplates}
              className="ml-3 underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        )}

        {/* Template list grouped by event type */}
        <div className="flex-1 overflow-y-auto px-8 py-6">
          {loading ? (
            <div className="flex items-center justify-center py-16">
              <div className="text-center">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-[#232323] mx-auto mb-3" />
                <p className="text-sm text-gray-500">Loading templates...</p>
              </div>
            </div>
          ) : Object.keys(groupedTemplates).length === 0 ? (
            <div className="text-center py-16 text-gray-500">
              <FileText className="w-16 h-16 mx-auto mb-4 text-gray-300" />
              <p className="text-lg font-medium text-gray-400">
                No templates found
              </p>
              <p className="text-sm text-gray-400 mt-1">
                Templates will be created automatically when the system
                initializes
              </p>
            </div>
          ) : (
            <div className="space-y-6">
              {Object.entries(groupedTemplates).map(
                ([eventType, eventTemplates]) => (
                  <div key={eventType}>
                    <h3 className="text-sm font-semibold text-[#232323] mb-3">
                      {getTypeLabel(eventType)}
                    </h3>
                    <div className="space-y-2">
                      {eventTemplates.map((tmpl) => (
                        <button
                          key={tmpl.template_id}
                          onClick={() => handleSelectTemplate(tmpl)}
                          className={`w-full text-left border rounded-xl p-4 transition-colors ${
                            selectedTemplate?.template_id === tmpl.template_id
                              ? "border-[#232323] bg-gray-50"
                              : "border-gray-200 hover:border-gray-300"
                          }`}
                        >
                          <div className="flex items-center justify-between mb-2">
                            <span className="inline-flex items-center gap-1.5 text-sm text-gray-700">
                              {getChannelIcon(tmpl.channel)}
                              {getChannelLabel(tmpl.channel)}
                            </span>
                            <Edit3 className="w-3.5 h-3.5 text-gray-400" />
                          </div>
                          <p className="text-xs text-gray-500 line-clamp-2">
                            {tmpl.body_template}
                          </p>
                        </button>
                      ))}
                    </div>
                  </div>
                ),
              )}
            </div>
          )}
        </div>
      </div>

      {/* Edit panel */}
      {selectedTemplate ? (
        <div className="w-[480px] flex flex-col bg-gray-50">
          <div className="px-6 py-4 border-b border-gray-100">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-semibold text-[#232323]">Edit Template</h3>
                <p className="text-xs text-gray-500 mt-0.5">
                  {getTypeLabel(selectedTemplate.event_type)} ·{" "}
                  {getChannelLabel(selectedTemplate.channel)}
                </p>
              </div>
              <button
                onClick={() => {
                  setSelectedTemplate(null);
                  setSaveError("");
                  setSaveSuccess(false);
                }}
                className="text-gray-400 hover:text-[#232323] p-2 rounded-lg hover:bg-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-6 space-y-6">
            {/* Subject template */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1.5">
                Subject Template
              </label>
              <input
                type="text"
                value={editSubject}
                onChange={(e) => setEditSubject(e.target.value)}
                placeholder="e.g. Delivery Update for {order_id}"
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
              />
            </div>

            {/* Body template */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1.5">
                Body Template
              </label>
              <textarea
                value={editBody}
                onChange={(e) => setEditBody(e.target.value)}
                rows={6}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white font-mono"
              />
            </div>

            {/* Placeholders */}
            {selectedTemplate.placeholders.length > 0 && (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5">
                  Available Placeholders
                </label>
                <div className="flex flex-wrap gap-1.5">
                  {selectedTemplate.placeholders.map((ph) => (
                    <span
                      key={ph}
                      className="px-2 py-1 bg-gray-100 text-gray-600 rounded text-xs font-mono"
                    >
                      {`{${ph}}`}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Live Preview */}
            <div>
              <label className="flex items-center gap-1.5 text-sm font-medium text-gray-700 mb-1.5">
                <Eye className="w-4 h-4" />
                Live Preview
              </label>
              <div className="bg-white border border-gray-200 rounded-lg p-4">
                {subjectPreview && (
                  <p className="text-sm font-semibold text-[#232323] mb-2">
                    {subjectPreview}
                  </p>
                )}
                <p className="text-sm text-gray-700 whitespace-pre-wrap leading-relaxed">
                  {bodyPreview || (
                    <span className="text-gray-400 italic">
                      Enter a body template to see the preview
                    </span>
                  )}
                </p>
              </div>
            </div>

            {/* Save error */}
            {saveError && (
              <div className="bg-red-50 text-red-700 px-3 py-2 rounded-lg text-sm flex items-center gap-2">
                <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                {saveError}
              </div>
            )}

            {/* Save success */}
            {saveSuccess && (
              <div className="bg-green-50 text-green-700 px-3 py-2 rounded-lg text-sm flex items-center gap-2">
                <Check className="w-4 h-4 flex-shrink-0" />
                Template saved successfully
              </div>
            )}
          </div>

          {/* Save button */}
          <div className="px-6 py-4 border-t border-gray-100">
            <button
              onClick={handleSave}
              disabled={saving}
              className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm text-white rounded-lg transition-colors hover:opacity-90 disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              <Save className="w-4 h-4" />
              {saving ? "Saving..." : "Save Template"}
            </button>
          </div>
        </div>
      ) : (
        <div className="w-[480px] flex items-center justify-center bg-gray-50 text-gray-400">
          <div className="text-center">
            <Edit3 className="w-12 h-12 mx-auto mb-3 text-gray-300" />
            <p className="text-sm font-medium">Select a template</p>
            <p className="text-xs mt-1">
              Click a template to edit and preview it
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
