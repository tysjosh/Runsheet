"use client";

/**
 * Approval Queue panel.
 *
 * Displays pending actions requiring human approval with approve/reject
 * buttons and impact summaries. Subscribes to WebSocket for real-time
 * approval queue updates and wires to the approve/reject REST endpoints.
 *
 * Validates:
 * - Requirement 9.3: Approval queue panel with approve/reject and impact summaries
 */

import {
  AlertTriangle,
  Check,
  Clock,
  ShieldAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useAgentWebSocket } from "../../hooks/useAgentWebSocket";
import type { ApprovalEntry } from "../../services/agentApi";
import {
  approveAction,
  getApprovals,
  rejectAction,
} from "../../services/agentApi";

/** Human-readable agent name mapping */
const AGENT_LABELS: Record<string, string> = {
  delay_response_agent: "Delay Response",
  fuel_management_agent: "Fuel Management",
  sla_guardian_agent: "SLA Guardian",
  ai_agent: "AI Assistant",
};

function getAgentLabel(agentId: string): string {
  return AGENT_LABELS[agentId] ?? agentId;
}

function formatTimestamp(iso: string): string {
  try {
    const date = new Date(iso);
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function timeUntilExpiry(expiryIso: string): string {
  try {
    const expiry = new Date(expiryIso);
    const now = new Date();
    const diffMs = expiry.getTime() - now.getTime();
    if (diffMs <= 0) return "Expired";
    const minutes = Math.floor(diffMs / 60000);
    if (minutes < 60) return `${minutes}m left`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m left`;
  } catch {
    return "";
  }
}

export default function ApprovalQueue() {
  const [approvals, setApprovals] = useState<ApprovalEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionInProgress, setActionInProgress] = useState<string | null>(null);

  // Load initial data
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const result = await getApprovals("default", 1, 50);
        if (!cancelled) {
          setApprovals(result.entries ?? []);
        }
      } catch (error) {
        console.error("Failed to load approvals:", error);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  // Subscribe to real-time approval events
  const handleApprovalEvent = useCallback(
    (event: { type: string; approval: ApprovalEntry }) => {
      setApprovals((prev) => {
        if (event.type === "approval_created") {
          // Add new approval to the top
          const exists = prev.some(
            (a) => a.action_id === event.approval.action_id,
          );
          if (exists) return prev;
          return [event.approval, ...prev];
        }
        // Update existing approval (approved, rejected, expired)
        return prev
          .map((a) =>
            a.action_id === event.approval.action_id ? event.approval : a,
          )
          .filter((a) => a.status === "pending");
      });
    },
    [],
  );

  useAgentWebSocket({ onApprovalEvent: handleApprovalEvent });

  // Approve handler
  const handleApprove = useCallback(async (actionId: string) => {
    setActionInProgress(actionId);
    try {
      await approveAction(actionId);
      setApprovals((prev) => prev.filter((a) => a.action_id !== actionId));
    } catch (error) {
      console.error("Failed to approve action:", error);
    } finally {
      setActionInProgress(null);
    }
  }, []);

  // Reject handler
  const handleReject = useCallback(async (actionId: string) => {
    setActionInProgress(actionId);
    try {
      await rejectAction(actionId);
      setApprovals((prev) => prev.filter((a) => a.action_id !== actionId));
    } catch (error) {
      console.error("Failed to reject action:", error);
    } finally {
      setActionInProgress(null);
    }
  }, []);

  const pendingApprovals = approvals.filter((a) => a.status === "pending");

  return (
    <div className="bg-white rounded-xl border border-gray-100 flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-amber-500 rounded-lg flex items-center justify-center">
            <ShieldAlert className="w-4 h-4 text-white" />
          </div>
          <h3 className="text-sm font-semibold text-[#232323]">Approval Queue</h3>
        </div>
        {pendingApprovals.length > 0 && (
          <span className="text-xs font-medium bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full">
            {pendingApprovals.length} pending
          </span>
        )}
      </div>

      {/* Queue */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <div className="w-5 h-5 border-2 border-gray-300 border-t-amber-500 rounded-full animate-spin" />
          </div>
        ) : pendingApprovals.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 text-gray-400">
            <ShieldAlert className="w-8 h-8 mb-2" />
            <p className="text-sm">No pending approvals</p>
          </div>
        ) : (
          pendingApprovals.map((approval) => {
            const isProcessing = actionInProgress === approval.action_id;
            return (
              <div
                key={approval.action_id}
                className="border border-gray-100 rounded-lg p-3 hover:border-gray-200 transition-colors"
              >
                {/* Top row: agent + risk + time */}
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium text-[#232323]">
                      {getAgentLabel(approval.proposed_by)}
                    </span>
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                        approval.risk_level === "high"
                          ? "bg-red-50 text-red-600"
                          : approval.risk_level === "medium"
                            ? "bg-amber-50 text-amber-600"
                            : "bg-emerald-50 text-emerald-600"
                      }`}
                    >
                      {approval.risk_level}
                    </span>
                  </div>
                  <div className="flex items-center gap-1 text-[10px] text-gray-400">
                    <Clock className="w-3 h-3" />
                    {timeUntilExpiry(approval.expiry_time)}
                  </div>
                </div>

                {/* Tool name */}
                <p className="text-xs font-medium text-gray-700 mb-1">
                  {approval.tool_name}
                </p>

                {/* Impact summary */}
                <p className="text-xs text-gray-500 mb-3 line-clamp-2">
                  {approval.impact_summary || "No impact summary available"}
                </p>

                {/* Action buttons */}
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => handleApprove(approval.action_id)}
                    disabled={isProcessing}
                    aria-label={`Approve ${approval.tool_name} action`}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-emerald-600 hover:bg-emerald-700 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-emerald-500"
                  >
                    {isProcessing ? (
                      <div className="w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin" />
                    ) : (
                      <Check className="w-3 h-3" />
                    )}
                    Approve
                  </button>
                  <button
                    onClick={() => handleReject(approval.action_id)}
                    disabled={isProcessing}
                    aria-label={`Reject ${approval.tool_name} action`}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-gray-400"
                  >
                    {isProcessing ? (
                      <div className="w-3 h-3 border-2 border-gray-400 border-t-transparent rounded-full animate-spin" />
                    ) : (
                      <X className="w-3 h-3" />
                    )}
                    Reject
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
