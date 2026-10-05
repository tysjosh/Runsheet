"use client";

/**
 * Approval Queue panel.
 *
 * Displays pending actions requiring human approval with approve/reject
 * buttons and impact summaries. Subscribes to WebSocket for real-time
 * approval queue updates and wires to the approve/reject REST endpoints.
 *
 * Also surfaces loading-plan execution results (R12, design K13): failed,
 * incomplete (Retry), and in-flight or stalled approved loading plans.
 *
 * Validates:
 * - Requirement 9.3: Approval queue panel with approve/reject and impact summaries
 * - Requirement 12.1-12.6: loading-plan execution results in the queue
 */

import { Check, Clock, RotateCcw, ShieldAlert, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useAgentWebSocket } from "../../hooks/useAgentWebSocket";
import type { ApprovalEntry } from "../../services/agentApi";
import {
  approveAction,
  getApprovals,
  rejectAction,
} from "../../services/agentApi";
import { ApiError } from "../../services/api";

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

function _formatTimestamp(iso: string): string {
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

const LOADING_TOOL = "apply_loading_plan";

/** Matches the backend approval lease (APPROVAL_LEASE_SECONDS, K7). */
const APPROVAL_LEASE_MS = 120_000;

/** Re-render cadence while an "Applying…" row is on screen (K13). */
const APPLYING_REFRESH_MS = 15_000;

const GENERIC_APPROVE_ERROR = "Could not approve this action. Try again.";

/** Rows the dispatcher still has to act on (K13, pass-2 NIT 12). */
function isUnresolved(a: ApprovalEntry): boolean {
  return (
    a.status === "pending" ||
    a.status === "incomplete" ||
    a.status === "failed" ||
    (a.status === "approved" && a.tool_name === LOADING_TOOL)
  );
}

/** An approved loading plan whose attempt is still inside its lease. */
function isApplying(a: ApprovalEntry, now: number): boolean {
  if (a.status !== "approved" || a.tool_name !== LOADING_TOOL) return false;
  const claimedAt = a.execution_result?.claimed_at;
  if (typeof claimedAt !== "string") return false;
  const claimedMs = Date.parse(claimedAt);
  return Number.isFinite(claimedMs) && now - claimedMs < APPROVAL_LEASE_MS;
}

const APPROVE_BUTTON_CLASS =
  "flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-success hover:bg-success-dark rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-success";
const REJECT_BUTTON_CLASS =
  "flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-gray-400";

function Spinner({ light }: { light: boolean }) {
  return (
    <div
      className={`w-3 h-3 border-2 ${light ? "border-white" : "border-gray-400"} border-t-transparent rounded-full animate-spin`}
    />
  );
}

export default function ApprovalQueue() {
  const [approvals, setApprovals] = useState<ApprovalEntry[]>([]);
  const [loading, setLoading] = useState(true);
  // Per-row in-flight flags so one row's request never unlocks another's.
  const [inFlight, setInFlight] = useState<Record<string, boolean>>({});
  // Per-row errors from the last approve attempt (R12.2).
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  // Bumped by the interval below to re-evaluate "Applying…" rows.
  const [, setTick] = useState(0);

  const fetchApprovals = useCallback(async () => {
    const result = await getApprovals("default", 1, 50, true);
    return (result.entries ?? []).filter(isUnresolved);
  }, []);

  // Load initial data
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const entries = await fetchApprovals();
        if (!cancelled) {
          setApprovals(entries);
        }
      } catch (error) {
        console.error("Failed to load approvals:", error);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [fetchApprovals]);

  const refetch = useCallback(async () => {
    try {
      setApprovals(await fetchApprovals());
    } catch (error) {
      console.error("Failed to reload approvals:", error);
    }
  }, [fetchApprovals]);

  // Subscribe to real-time approval events. No tenant filter here: the
  // server only sends this connection's tenant (K9, pass-3 NIT 7).
  const handleApprovalEvent = useCallback(
    (event: { type: string; approval: ApprovalEntry }) => {
      const incoming = event.approval;
      setApprovals((prev) => {
        const exists = prev.some((a) => a.action_id === incoming.action_id);
        if (!exists) {
          return isUnresolved(incoming) ? [incoming, ...prev] : prev;
        }
        return prev
          .map((a) => (a.action_id === incoming.action_id ? incoming : a))
          .filter(isUnresolved);
      });
    },
    [],
  );

  useAgentWebSocket({ onApprovalEvent: handleApprovalEvent });

  const setRowBusy = useCallback((actionId: string, busy: boolean) => {
    setInFlight((prev) => {
      const next = { ...prev };
      if (busy) next[actionId] = true;
      else delete next[actionId];
      return next;
    });
  }, []);

  const clearRowError = useCallback((actionId: string) => {
    setRowErrors((prev) => {
      if (!(actionId in prev)) return prev;
      const next = { ...prev };
      delete next[actionId];
      return next;
    });
  }, []);

  // Approve / Retry handler (R12.1, R12.2)
  const handleApprove = useCallback(
    async (actionId: string) => {
      setRowBusy(actionId, true);
      clearRowError(actionId);
      try {
        await approveAction(actionId);
        setApprovals((prev) => prev.filter((a) => a.action_id !== actionId));
      } catch (error) {
        if (
          error instanceof ApiError &&
          (error.code === "LOADING_PLAN_EXECUTION_FAILED" ||
            error.status === 409)
        ) {
          setRowErrors((prev) => ({ ...prev, [actionId]: error.message }));
          await refetch();
        } else {
          console.error("Failed to approve action:", error);
          setRowErrors((prev) => ({
            ...prev,
            [actionId]: GENERIC_APPROVE_ERROR,
          }));
        }
      } finally {
        setRowBusy(actionId, false);
      }
    },
    [clearRowError, refetch, setRowBusy],
  );

  // Reject / Dismiss handler
  const handleReject = useCallback(
    async (actionId: string) => {
      setRowBusy(actionId, true);
      try {
        await rejectAction(actionId);
        setApprovals((prev) => prev.filter((a) => a.action_id !== actionId));
        clearRowError(actionId);
      } catch (error) {
        console.error("Failed to reject action:", error);
      } finally {
        setRowBusy(actionId, false);
      }
    },
    [clearRowError, setRowBusy],
  );

  const unresolved = approvals.filter(isUnresolved);
  const pendingCount = unresolved.filter((a) => a.status === "pending").length;
  const now = Date.now();
  const hasApplyingRow = unresolved.some((a) => isApplying(a, now));

  // While a row is "Applying…", re-render every 15 s so it moves on to
  // Retry once its lease lapses without a reload (K13, pass-2 finding 6).
  useEffect(() => {
    if (!hasApplyingRow) return;
    const id = setInterval(() => setTick((t) => t + 1), APPLYING_REFRESH_MS);
    return () => clearInterval(id);
  }, [hasApplyingRow]);

  return (
    <div className="bg-white rounded-xl border border-gray-100 flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-warning rounded-lg flex items-center justify-center">
            <ShieldAlert className="w-4 h-4 text-white" />
          </div>
          <h3 className="text-sm font-semibold text-primary">Approval Queue</h3>
        </div>
        {pendingCount > 0 && (
          <span className="text-xs font-medium bg-warning-light text-warning-dark px-2 py-0.5 rounded-full">
            {pendingCount} pending
          </span>
        )}
      </div>

      {/* Queue */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <div className="w-5 h-5 border-2 border-gray-300 border-t-amber-500 rounded-full animate-spin" />
          </div>
        ) : unresolved.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 text-gray-500">
            <ShieldAlert className="w-8 h-8 mb-2" />
            <p className="text-sm">Nothing needs your review</p>
          </div>
        ) : (
          unresolved.map((approval) => {
            const id = approval.action_id;
            const isProcessing = Boolean(inFlight[id]);
            const result = approval.execution_result;
            const resultMessage =
              typeof result?.message === "string" && result.message
                ? result.message
                : undefined;
            const alertMessage = rowErrors[id] ?? resultMessage;
            const applying = isApplying(approval, now);
            const isLoadingApproved =
              approval.status === "approved" &&
              approval.tool_name === LOADING_TOOL;
            const hasClaim = typeof result?.claimed_at === "string";

            let statusNote: string | null = null;
            if (isLoadingApproved && !applying) {
              statusNote = hasClaim
                ? "This plan stopped while it was being applied. Retry to finish it."
                : "Approved before plans could be applied. Dismiss it; the next loading run will re-propose these orders.";
            }

            const approveButton = (label: "Approve" | "Retry") => (
              <button
                type="button"
                onClick={() => handleApprove(id)}
                disabled={isProcessing}
                aria-label={`${label} ${approval.tool_name} action`}
                className={APPROVE_BUTTON_CLASS}
              >
                {isProcessing ? (
                  <Spinner light />
                ) : label === "Retry" ? (
                  <RotateCcw className="w-3 h-3" />
                ) : (
                  <Check className="w-3 h-3" />
                )}
                {label}
              </button>
            );
            const rejectButton = (
              label: "Reject" | "Dismiss",
              aria: string,
            ) => (
              <button
                type="button"
                onClick={() => handleReject(id)}
                disabled={isProcessing}
                aria-label={aria}
                className={REJECT_BUTTON_CLASS}
              >
                {isProcessing ? (
                  <Spinner light={false} />
                ) : (
                  <X className="w-3 h-3" />
                )}
                {label}
              </button>
            );

            let actions: React.ReactNode;
            if (applying) {
              actions = (
                <button type="button" disabled className={APPROVE_BUTTON_CLASS}>
                  <Spinner light />
                  Applying…
                </button>
              );
            } else if (isLoadingApproved) {
              // Stalled attempt: Retry reclaims; Reject is refused by the
              // backend for a non-legacy approved entry (K7). Legacy: Dismiss.
              actions = hasClaim
                ? approveButton("Retry")
                : rejectButton("Dismiss", "Dismiss approved loading plan");
            } else if (approval.status === "incomplete") {
              // Reject only when the sticky writes_made is known false (R12.3).
              actions = (
                <>
                  {approveButton("Retry")}
                  {result?.writes_made === false &&
                    rejectButton(
                      "Reject",
                      `Reject ${approval.tool_name} action`,
                    )}
                </>
              );
            } else if (approval.status === "failed") {
              actions = rejectButton("Dismiss", "Dismiss failed loading plan");
            } else {
              actions = (
                <>
                  {approveButton("Approve")}
                  {rejectButton(
                    "Reject",
                    `Reject ${approval.tool_name} action`,
                  )}
                </>
              );
            }

            return (
              <div
                key={id}
                data-testid={`approval-row-${id}`}
                aria-busy={applying ? "true" : undefined}
                className="border border-gray-100 rounded-lg p-3 hover:border-gray-200 transition-colors"
              >
                {/* Top row: agent + risk + time */}
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium text-primary">
                      {getAgentLabel(approval.proposed_by)}
                    </span>
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                        approval.risk_level === "high"
                          ? "bg-error-light text-error"
                          : approval.risk_level === "medium"
                            ? "bg-warning-light text-warning"
                            : "bg-success-light text-success"
                      }`}
                    >
                      {approval.risk_level}
                    </span>
                  </div>
                  {approval.status === "pending" && (
                    <div className="flex items-center gap-1 text-[10px] text-gray-500">
                      <Clock className="w-3 h-3" />
                      {timeUntilExpiry(approval.expiry_time)}
                    </div>
                  )}
                </div>

                {/* Tool name */}
                <p className="text-xs font-medium text-gray-700 mb-1">
                  {approval.tool_name}
                </p>

                {/* Impact summary */}
                <p className="text-xs text-gray-500 mb-3 line-clamp-2">
                  {approval.impact_summary || "No impact summary available"}
                </p>

                {statusNote && (
                  <p className="text-xs text-gray-700 mb-2">{statusNote}</p>
                )}

                {alertMessage && (
                  <p role="alert" className="text-xs text-error mb-2">
                    {alertMessage}
                  </p>
                )}

                {/* Action buttons */}
                <div className="flex items-center gap-2">{actions}</div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
