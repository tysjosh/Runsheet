"use client";

import { useCallback, useEffect, useState } from "react";
import { Bot } from "lucide-react";
import {
  getApprovals,
  approveAction,
  rejectAction,
} from "../../services/agentApi";
import type { ApprovalEntry, RiskLevel } from "../../services/agentApi";

const RISK_CONFIG: Record<RiskLevel, { bg: string; text: string }> = {
  low: { bg: "bg-green-100", text: "text-green-700" },
  medium: { bg: "bg-yellow-100", text: "text-yellow-700" },
  high: { bg: "bg-red-100", text: "text-red-700" },
};

const REFRESH_INTERVAL_MS = 30_000;

/**
 * Approval queue panel showing pending agent proposals that require
 * human review. Operators can approve or reject each action inline.
 *
 * Auto-refreshes every 30 seconds.
 *
 * Validates: Requirements 2.3, 2.4, 9.5
 */
export default function ApprovalQueuePanel() {
  const [approvals, setApprovals] = useState<ApprovalEntry[]>([]);
  const [processingIds, setProcessingIds] = useState<Set<string>>(new Set());

  const fetchApprovals = useCallback(async () => {
    try {
      const data = await getApprovals("dev-tenant");
      // API may return `entries` or `items` — handle both shapes
      const list =
        (data as any).entries ?? (data as any).items ?? [];
      setApprovals(list.filter((e: ApprovalEntry) => e.status === "pending"));
    } catch (error) {
      console.error("Failed to fetch approvals:", error);
    }
  }, []);

  useEffect(() => {
    fetchApprovals();
    const interval = setInterval(fetchApprovals, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [fetchApprovals]);

  const handleApprove = async (actionId: string) => {
    setProcessingIds((prev) => new Set(prev).add(actionId));
    try {
      await approveAction(actionId, "admin");
      setApprovals((prev) => prev.filter((a) => a.action_id !== actionId));
    } catch (error) {
      console.error("Failed to approve action:", error);
    } finally {
      setProcessingIds((prev) => {
        const next = new Set(prev);
        next.delete(actionId);
        return next;
      });
    }
  };

  const handleReject = async (actionId: string) => {
    setProcessingIds((prev) => new Set(prev).add(actionId));
    try {
      await rejectAction(actionId, "Rejected by operator");
      setApprovals((prev) => prev.filter((a) => a.action_id !== actionId));
    } catch (error) {
      console.error("Failed to reject action:", error);
    } finally {
      setProcessingIds((prev) => {
        const next = new Set(prev);
        next.delete(actionId);
        return next;
      });
    }
  };

  const formatToolName = (name: string): string =>
    name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  return (
    <div className="bg-white rounded-xl border border-gray-100">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100">
        <Bot className="w-4 h-4 text-violet-600" />
        <h3 className="text-sm font-semibold text-[#232323]">
          Agent Proposals{" "}
          {approvals.length > 0 && (
            <span className="ml-1 inline-flex items-center justify-center px-1.5 py-0.5 rounded-full bg-violet-100 text-violet-700 text-xs font-medium">
              {approvals.length}
            </span>
          )}
        </h3>
      </div>

      <div className="max-h-64 overflow-y-auto divide-y divide-gray-50">
        {approvals.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-gray-400">
            No pending proposals
          </div>
        ) : (
          approvals.map((entry) => {
            const risk = RISK_CONFIG[entry.risk_level] ?? RISK_CONFIG.medium;
            const isProcessing = processingIds.has(entry.action_id);

            return (
              <div
                key={entry.action_id}
                className="px-4 py-3 hover:bg-gray-50/50"
              >
                {/* Tool name + risk badge */}
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-[#232323]">
                    {formatToolName(entry.tool_name)}
                  </span>
                  <span
                    className={`px-2 py-0.5 rounded text-xs font-medium ${risk.bg} ${risk.text}`}
                  >
                    {entry.risk_level}
                  </span>
                </div>

                {/* Proposed by */}
                <p className="text-xs text-gray-500 mt-0.5">
                  Proposed by{" "}
                  <span className="font-medium">{entry.proposed_by}</span>
                </p>

                {/* Impact summary */}
                {entry.impact_summary && (
                  <p className="text-xs text-gray-400 mt-0.5 line-clamp-2">
                    {entry.impact_summary}
                  </p>
                )}

                {/* Action buttons */}
                <div className="flex gap-2 mt-2">
                  <button
                    type="button"
                    disabled={isProcessing}
                    onClick={() => handleApprove(entry.action_id)}
                    className="flex-1 px-2 py-1 rounded text-xs font-medium bg-green-100 text-green-700 hover:bg-green-200 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {isProcessing ? "…" : "Approve"}
                  </button>
                  <button
                    type="button"
                    disabled={isProcessing}
                    onClick={() => handleReject(entry.action_id)}
                    className="flex-1 px-2 py-1 rounded text-xs font-medium bg-red-100 text-red-700 hover:bg-red-200 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {isProcessing ? "…" : "Reject"}
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
