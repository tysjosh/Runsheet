"use client";

/**
 * Toast notification component for autonomous agent actions.
 *
 * Displays a toast notification when an autonomous agent takes an action,
 * showing the agent name, action summary, and outcome. Toasts auto-dismiss
 * after 6 seconds and can be manually dismissed.
 *
 * Validates:
 * - Requirement 9.7: Toast notification with agent name, action summary, and outcome
 */

import { CheckCircle2, X, XCircle, Clock, AlertTriangle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useAgentWebSocket } from "../../hooks/useAgentWebSocket";
import type { ActivityLogEntry } from "../../services/agentApi";

const TOAST_DURATION_MS = 6000;
const MAX_TOASTS = 5;

/** Human-readable agent name mapping */
const AGENT_LABELS: Record<string, string> = {
  delay_response_agent: "Delay Response",
  fuel_management_agent: "Fuel Management",
  sla_guardian_agent: "SLA Guardian",
  ai_agent: "AI Assistant",
  system: "System",
};

interface ToastItem {
  id: string;
  agentName: string;
  actionSummary: string;
  outcome: string;
  timestamp: number;
}

function getAgentLabel(agentId: string): string {
  return AGENT_LABELS[agentId] ?? agentId;
}

function buildActionSummary(entry: ActivityLogEntry): string {
  if (entry.tool_name) {
    return entry.tool_name.replace(/_/g, " ");
  }
  return entry.action_type.replace(/_/g, " ");
}

const OUTCOME_CONFIG: Record<
  string,
  { icon: typeof CheckCircle2; color: string; bg: string; border: string }
> = {
  success: {
    icon: CheckCircle2,
    color: "text-emerald-600",
    bg: "bg-emerald-50",
    border: "border-emerald-200",
  },
  failure: {
    icon: XCircle,
    color: "text-red-600",
    bg: "bg-red-50",
    border: "border-red-200",
  },
  pending_approval: {
    icon: Clock,
    color: "text-amber-600",
    bg: "bg-amber-50",
    border: "border-amber-200",
  },
  rejected: {
    icon: AlertTriangle,
    color: "text-gray-600",
    bg: "bg-gray-50",
    border: "border-gray-200",
  },
};

function ToastNotification({
  toast,
  onDismiss,
}: {
  toast: ToastItem;
  onDismiss: (id: string) => void;
}) {
  const config = OUTCOME_CONFIG[toast.outcome] ?? OUTCOME_CONFIG.success;
  const Icon = config.icon;

  useEffect(() => {
    const timer = setTimeout(() => onDismiss(toast.id), TOAST_DURATION_MS);
    return () => clearTimeout(timer);
  }, [toast.id, onDismiss]);

  return (
    <div
      className={`flex items-start gap-3 px-4 py-3 rounded-lg border shadow-lg ${config.bg} ${config.border} animate-slide-in-right max-w-sm`}
      role="alert"
    >
      <Icon className={`w-5 h-5 flex-shrink-0 mt-0.5 ${config.color}`} />
      <div className="flex-1 min-w-0">
        <p className="text-xs font-semibold text-[#232323]">
          {toast.agentName}
        </p>
        <p className="text-xs text-gray-600 mt-0.5 truncate">
          {toast.actionSummary}
        </p>
        <p className={`text-[10px] mt-1 font-medium ${config.color}`}>
          {toast.outcome.replace(/_/g, " ")}
        </p>
      </div>
      <button
        onClick={() => onDismiss(toast.id)}
        className="flex-shrink-0 p-0.5 rounded text-gray-400 hover:text-gray-600 transition-colors"
        aria-label="Dismiss notification"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}

/**
 * AgentToast renders a stack of toast notifications in the bottom-right
 * corner of the viewport. It subscribes to the agent activity WebSocket
 * and creates a toast for each autonomous agent action.
 */
export default function AgentToast() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const handleAgentAction = useCallback((entry: ActivityLogEntry) => {
    // Only show toasts for autonomous agent actions, not system or user actions
    const autonomousAgents = [
      "delay_response_agent",
      "fuel_management_agent",
      "sla_guardian_agent",
    ];
    if (!autonomousAgents.includes(entry.agent_id)) return;
    // Skip monitoring_cycle entries to avoid noise
    if (entry.action_type === "monitoring_cycle") return;

    const toast: ToastItem = {
      id: entry.log_id || `toast-${Date.now()}-${Math.random()}`,
      agentName: getAgentLabel(entry.agent_id),
      actionSummary: buildActionSummary(entry),
      outcome: entry.outcome,
      timestamp: Date.now(),
    };

    setToasts((prev) => {
      const next = [toast, ...prev];
      return next.slice(0, MAX_TOASTS);
    });
  }, []);

  const handleActivity = useCallback((entry: ActivityLogEntry) => {
    // Also trigger toasts for mutation actions from autonomous agents
    const autonomousAgents = [
      "delay_response_agent",
      "fuel_management_agent",
      "sla_guardian_agent",
    ];
    if (
      autonomousAgents.includes(entry.agent_id) &&
      entry.action_type === "mutation"
    ) {
      handleAgentAction(entry);
    }
  }, [handleAgentAction]);

  useAgentWebSocket({
    onAgentAction: handleAgentAction,
    onActivity: handleActivity,
  });

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col gap-2">
      {toasts.map((toast) => (
        <ToastNotification
          key={toast.id}
          toast={toast}
          onDismiss={dismissToast}
        />
      ))}
    </div>
  );
}
