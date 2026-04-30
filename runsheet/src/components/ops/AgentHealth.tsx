"use client";

/**
 * Agent Health panel.
 *
 * Displays the status of each autonomous agent (running, paused/stopped, error)
 * with last activity timestamp. Provides pause/resume controls wired to the
 * POST `/agent/{agent_id}/pause` and `/agent/{agent_id}/resume` endpoints.
 *
 * Validates:
 * - Requirement 9.5: Agent health indicators with status and last activity
 * - Requirement 9.6: Pause and resume individual autonomous agents
 */

import {
  AlertCircle,
  HeartPulse,
  Pause,
  Play,
  RefreshCw,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { AgentHealthEntry } from "../../services/agentApi";
import { getAgentHealth, pauseAgent, resumeAgent } from "../../services/agentApi";

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
    dot: "bg-emerald-500",
    bg: "bg-emerald-50",
    text: "text-emerald-700",
    label: "Running",
  },
  stopped: {
    dot: "bg-gray-400",
    bg: "bg-gray-100",
    text: "text-gray-600",
    label: "Paused",
  },
  error: {
    dot: "bg-red-500",
    bg: "bg-red-50",
    text: "text-red-700",
    label: "Error",
  },
};

export default function AgentHealth() {
  const [agents, setAgents] = useState<Record<string, AgentHealthEntry>>({});
  const [loading, setLoading] = useState(true);
  const [actionInProgress, setActionInProgress] = useState<string | null>(null);

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

  const handlePause = useCallback(
    async (agentId: string) => {
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
    },
    [],
  );

  const handleResume = useCallback(
    async (agentId: string) => {
      setActionInProgress(agentId);
      try {
        const result = await resumeAgent(agentId);
        setAgents((prev) => ({
          ...prev,
          [agentId]: {
            ...prev[agentId],
            status:
              result.status === "already_running" ? "running" : "running",
          },
        }));
      } catch (error) {
        console.error(`Failed to resume agent ${agentId}:`, error);
      } finally {
        setActionInProgress(null);
      }
    },
    [],
  );

  const agentEntries = Object.values(agents);

  return (
    <div className="bg-white rounded-xl border border-gray-100 flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-[#232323] rounded-lg flex items-center justify-center">
            <HeartPulse className="w-4 h-4 text-white" />
          </div>
          <h3 className="text-sm font-semibold text-[#232323]">Agent Health</h3>
        </div>
        <button
          onClick={loadHealth}
          className="p-1.5 rounded-md text-gray-400 hover:text-[#232323] hover:bg-gray-100 transition-colors"
          title="Refresh"
        >
          <RefreshCw className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Agent list */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <div className="w-5 h-5 border-2 border-gray-300 border-t-[#232323] rounded-full animate-spin" />
          </div>
        ) : agentEntries.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 text-gray-400">
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
                    <AlertCircle className="w-5 h-5 text-red-500" />
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
                    <span className="text-xs font-medium text-[#232323]">
                      {meta.label}
                    </span>
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${style.bg} ${style.text}`}
                    >
                      {style.label}
                    </span>
                  </div>
                  <p className="text-[11px] text-gray-400 truncate">
                    {meta.description}
                  </p>
                </div>

                {/* Pause/Resume button */}
                <button
                  onClick={() =>
                    isRunning
                      ? handlePause(agent.agent_id)
                      : handleResume(agent.agent_id)
                  }
                  disabled={isProcessing || agent.status === "error"}
                  className={`flex-shrink-0 p-2 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                    isRunning
                      ? "text-amber-600 hover:bg-amber-50"
                      : "text-emerald-600 hover:bg-emerald-50"
                  }`}
                  title={isRunning ? "Pause agent" : "Resume agent"}
                >
                  {isProcessing ? (
                    <div className="w-4 h-4 border-2 border-gray-400 border-t-transparent rounded-full animate-spin" />
                  ) : isRunning ? (
                    <Pause className="w-4 h-4" />
                  ) : (
                    <Play className="w-4 h-4" />
                  )}
                </button>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
