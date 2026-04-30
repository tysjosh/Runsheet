"use client";

/**
 * Agent Activity Feed panel.
 *
 * Displays real-time autonomous agent actions with timestamps and outcomes.
 * Subscribes to the `/ws/agent-activity` WebSocket channel for live updates
 * and falls back to polling the REST API for initial data.
 *
 * Validates:
 * - Requirement 9.2: Agent activity feed panel with real-time updates
 * - Requirement 9.7: Show agent name, action summary, and outcome
 */

import { Activity, AlertCircle, CheckCircle2, Clock, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useAgentWebSocket } from "../../hooks/useAgentWebSocket";
import type { ActivityLogEntry } from "../../services/agentApi";
import { getActivityLog } from "../../services/agentApi";

const MAX_FEED_ITEMS = 50;

/** Human-readable agent name mapping */
const AGENT_LABELS: Record<string, string> = {
  delay_response_agent: "Delay Response",
  fuel_management_agent: "Fuel Management",
  sla_guardian_agent: "SLA Guardian",
  ai_agent: "AI Assistant",
  system: "System",
};

/** Outcome badge colors */
const OUTCOME_STYLES: Record<string, { bg: string; text: string; icon: typeof CheckCircle2 }> = {
  success: { bg: "bg-emerald-50", text: "text-emerald-700", icon: CheckCircle2 },
  failure: { bg: "bg-red-50", text: "text-red-700", icon: XCircle },
  pending_approval: { bg: "bg-amber-50", text: "text-amber-700", icon: Clock },
  rejected: { bg: "bg-gray-100", text: "text-gray-600", icon: XCircle },
};

function getAgentLabel(agentId: string): string {
  return AGENT_LABELS[agentId] ?? agentId;
}

function formatTimestamp(iso: string): string {
  try {
    const date = new Date(iso);
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}

function buildActionSummary(entry: ActivityLogEntry): string {
  if (entry.action_type === "monitoring_cycle") {
    const detections = (entry.details as Record<string, unknown>)?.detection_count ?? 0;
    const actions = (entry.details as Record<string, unknown>)?.action_count ?? 0;
    return `Monitoring cycle: ${detections} detections, ${actions} actions`;
  }
  if (entry.tool_name) {
    return `${entry.action_type}: ${entry.tool_name}`;
  }
  return entry.action_type;
}

export default function AgentActivityFeed() {
  const [entries, setEntries] = useState<ActivityLogEntry[]>([]);
  const [loading, setLoading] = useState(true);

  // Load initial data from REST API
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const result = await getActivityLog({ size: MAX_FEED_ITEMS });
        if (!cancelled) {
          setEntries(result.entries ?? []);
        }
      } catch (error) {
        console.error("Failed to load activity log:", error);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  // Subscribe to real-time updates
  const handleActivity = useCallback((entry: ActivityLogEntry) => {
    setEntries((prev) => {
      const next = [entry, ...prev];
      return next.slice(0, MAX_FEED_ITEMS);
    });
  }, []);

  const handleAgentAction = useCallback((entry: ActivityLogEntry) => {
    setEntries((prev) => {
      const next = [entry, ...prev];
      return next.slice(0, MAX_FEED_ITEMS);
    });
  }, []);

  const { isConnected } = useAgentWebSocket({
    onActivity: handleActivity,
    onAgentAction: handleAgentAction,
  });

  return (
    <div className="bg-white rounded-xl border border-gray-100 flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-[#232323] rounded-lg flex items-center justify-center">
            <Activity className="w-4 h-4 text-white" />
          </div>
          <h3 className="text-sm font-semibold text-[#232323]">Agent Activity</h3>
        </div>
        <div className="flex items-center gap-1.5">
          <span
            className={`w-2 h-2 rounded-full ${isConnected ? "bg-emerald-500" : "bg-gray-300"}`}
          />
          <span className="text-xs text-gray-400">
            {isConnected ? "Live" : "Offline"}
          </span>
        </div>
      </div>

      {/* Feed */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-1.5">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <div className="w-5 h-5 border-2 border-gray-300 border-t-[#232323] rounded-full animate-spin" />
          </div>
        ) : entries.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 text-gray-400">
            <Activity className="w-8 h-8 mb-2" />
            <p className="text-sm">No agent activity yet</p>
          </div>
        ) : (
          entries.map((entry) => {
            const outcomeStyle = OUTCOME_STYLES[entry.outcome] ?? OUTCOME_STYLES.success;
            const OutcomeIcon = outcomeStyle.icon;
            return (
              <div
                key={entry.log_id}
                className="flex items-start gap-3 px-3 py-2.5 rounded-lg hover:bg-gray-50 transition-colors"
              >
                <OutcomeIcon className={`w-4 h-4 mt-0.5 flex-shrink-0 ${outcomeStyle.text}`} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-xs font-medium text-[#232323]">
                      {getAgentLabel(entry.agent_id)}
                    </span>
                    {entry.risk_level && (
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                          entry.risk_level === "high"
                            ? "bg-red-50 text-red-600"
                            : entry.risk_level === "medium"
                              ? "bg-amber-50 text-amber-600"
                              : "bg-emerald-50 text-emerald-600"
                        }`}
                      >
                        {entry.risk_level}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-gray-500 truncate">
                    {buildActionSummary(entry)}
                  </p>
                </div>
                <span className="text-[10px] text-gray-400 flex-shrink-0 mt-0.5">
                  {formatTimestamp(entry.timestamp)}
                </span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
