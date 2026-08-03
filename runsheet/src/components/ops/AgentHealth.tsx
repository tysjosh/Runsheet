"use client";

/**
 * Agent Health panel.
 *
 * Displays the status of each autonomous agent (running, paused/stopped, error)
 * with last activity timestamp. Provides pause/resume controls wired to the
 * POST `/agent/{agent_id}/pause` and `/agent/{agent_id}/resume` endpoints.
 *
 * Audience: this panel renders inside `OperationsControlView` and `/ops/command`,
 * both of which are `admin` + `dispatcher` surfaces — but pausing an agent is a
 * tenant-wide lifecycle change, so the backend restricts it to `admin` via
 * `agent_admin_dependency` (see `Agents/api_authz.py`). The status list stays
 * visible to dispatchers; only the pause/resume control is admin-gated, so a
 * dispatcher is not shown a button that can only return 403.
 *
 * The role check here is presentation-only. The backend re-checks on every
 * pause/resume and answers 403 regardless of what this component renders.
 *
 * Validates:
 * - Requirement 9.5: Agent health indicators with status and last activity
 * - Requirement 9.6: Pause and resume individual autonomous agents
 */

import { AlertCircle, HeartPulse, Pause, Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { AgentHealthEntry } from "../../services/agentApi";
import {
  getAgentHealth,
  pauseAgent,
  resumeAgent,
} from "../../services/agentApi";
import { getCurrentUserRoles } from "../../utils/auth";

/** Human-readable agent name and description mapping */
const AGENT_META: Record<string, { label: string; description: string }> = {
  delay_response_agent: {
    label: "Delay Response",
    description: "Monitors delayed jobs and proposes reassignments",
  },
  fuel_management_agent: {
    label: "Fuel Management",
    description: "Monitors fuel levels and triggers refill requests",
  },
  sla_guardian_agent: {
    label: "SLA Guardian",
    description: "Monitors SLA breaches and escalates shipments",
  },
};

function getAgentMeta(agentId: string) {
  return (
    AGENT_META[agentId] ?? {
      label: agentId,
      description: "Autonomous agent",
    }
  );
}

const STATUS_STYLES: Record<
  string,
  { dot: string; bg: string; text: string; label: string }
> = {
  running: {
    dot: "bg-success",
    bg: "bg-success-light",
    text: "text-success-dark",
    label: "Running",
  },
  stopped: {
    dot: "bg-gray-400",
    bg: "bg-gray-100",
    text: "text-gray-600",
    label: "Paused",
  },
  error: {
    dot: "bg-error",
    bg: "bg-error-light",
    text: "text-error-dark",
    label: "Error",
  },
};

export default function AgentHealth() {
  const [agents, setAgents] = useState<Record<string, AgentHealthEntry>>({});
  const [loading, setLoading] = useState(true);
  const [actionInProgress, setActionInProgress] = useState<string | null>(null);
  // Defaults to false so a dispatcher never briefly sees an actionable control
  // before the session's claims resolve.
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const roles = await getCurrentUserRoles();
      if (!cancelled) setIsAdmin(roles.includes("admin"));
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const loadHealth = useCallback(async () => {
    try {
      const result = await getAgentHealth();
      setAgents(result.agents ?? {});
    } catch (error) {
      console.error("Failed to load agent health:", error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadHealth();
    // Poll every 15 seconds for health updates
    const interval = setInterval(loadHealth, 15000);
    return () => clearInterval(interval);
  }, [loadHealth]);

  const handlePause = useCallback(async (agentId: string) => {
    setActionInProgress(agentId);
    try {
      const result = await pauseAgent(agentId);
      setAgents((prev) => ({
        ...prev,
        [agentId]: {
          ...prev[agentId],
          status: result.status === "already_stopped" ? "stopped" : "stopped",
        },
      }));
    } catch (error) {
      console.error(`Failed to pause agent ${agentId}:`, error);
    } finally {
      setActionInProgress(null);
    }
  }, []);

  const handleResume = useCallback(async (agentId: string) => {
    setActionInProgress(agentId);
    try {
      const result = await resumeAgent(agentId);
      setAgents((prev) => ({
        ...prev,
        [agentId]: {
          ...prev[agentId],
          status: result.status === "already_running" ? "running" : "running",
        },
      }));
    } catch (error) {
      console.error(`Failed to resume agent ${agentId}:`, error);
    } finally {
      setActionInProgress(null);
    }
  }, []);

  const agentEntries = Object.values(agents);

  return (
    <div className="bg-white rounded-xl border border-gray-100 flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-primary rounded-lg flex items-center justify-center">
            <HeartPulse className="w-4 h-4 text-white" />
          </div>
          <h3 className="text-sm font-semibold text-primary">Agent Health</h3>
        </div>
        <button
          onClick={loadHealth}
          className="p-1.5 rounded-md text-gray-500 hover:text-primary hover:bg-gray-100 transition-colors"
          title="Refresh"
        >
          <RefreshCw className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Agent list */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <div className="w-5 h-5 border-2 border-gray-300 border-t-primary rounded-full animate-spin" />
          </div>
        ) : agentEntries.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 text-gray-500">
            <HeartPulse className="w-8 h-8 mb-2" />
            <p className="text-sm">No agents registered</p>
          </div>
        ) : (
          agentEntries.map((agent) => {
            const meta = getAgentMeta(agent.agent_id);
            const style = STATUS_STYLES[agent.status] ?? STATUS_STYLES.stopped;
            const isProcessing = actionInProgress === agent.agent_id;
            const isRunning = agent.status === "running";

            return (
              <div
                key={agent.agent_id}
                className="flex items-center gap-3 px-3 py-3 rounded-lg border border-gray-100 hover:border-gray-200 transition-colors"
              >
                {/* Status indicator */}
                <div className="flex-shrink-0">
                  {agent.status === "error" ? (
                    <AlertCircle className="w-5 h-5 text-error" />
                  ) : (
                    <span
                      className={`block w-3 h-3 rounded-full ${style.dot} ${
                        isRunning ? "animate-pulse" : ""
                      }`}
                    />
                  )}
                </div>

                {/* Agent info */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-xs font-medium text-primary">
                      {meta.label}
                    </span>
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${style.bg} ${style.text}`}
                    >
                      {style.label}
                    </span>
                  </div>
                  <p className="text-[11px] text-gray-500 truncate">
                    {meta.description}
                  </p>
                </div>

                {/* Pause/Resume button — admin only. Agent lifecycle is a
                    tenant-wide change, so `agent_admin_dependency` answers 403
                    for a dispatcher; omitting the control rather than disabling
                    it keeps the panel honest about what this role can do. */}
                {isAdmin && (
                  <button
                    type="button"
                    onClick={() =>
                      isRunning
                        ? handlePause(agent.agent_id)
                        : handleResume(agent.agent_id)
                    }
                    disabled={isProcessing || agent.status === "error"}
                    className={`flex-shrink-0 p-2 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                      isRunning
                        ? "text-warning hover:bg-warning-light"
                        : "text-success hover:bg-success-light"
                    }`}
                    title={isRunning ? "Pause agent" : "Resume agent"}
                    aria-label={
                      isRunning ? `Pause ${meta.label}` : `Resume ${meta.label}`
                    }
                  >
                    {isProcessing ? (
                      <div className="w-4 h-4 border-2 border-gray-400 border-t-transparent rounded-full animate-spin" />
                    ) : isRunning ? (
                      <Pause className="w-4 h-4" />
                    ) : (
                      <Play className="w-4 h-4" />
                    )}
                  </button>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
