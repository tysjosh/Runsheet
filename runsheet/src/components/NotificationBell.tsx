"use client";

/**
 * Global operator alert bell for the dashboard header.
 *
 * Surfaces autonomous-agent activity and approvals that need attention — the
 * same stream that powers the Operations Command Center — with a badge showing
 * the number of approvals awaiting the operator and a dropdown of the most
 * recent alerts. Subscribes to /ws/agent-activity so new agent actions and
 * approval requests appear in real time across every dashboard page; the badge
 * counts outstanding approvals (seeded from the server and adjusted as they are
 * created/resolved), so it persists across reloads rather than resetting.
 *
 * Note: this is distinct from the customer-notification history at
 * /dashboard/notifications (outbound SMS/email/WhatsApp to customers). The bell
 * is for the operator's own attention queue. "View all" routes to the command
 * center where the full feed + approval queue live.
 */

import { Bell, CheckCircle2, Clock, ShieldAlert, XCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAgentWebSocket } from "../hooks/useAgentWebSocket";
import {
  type ActivityLogEntry,
  type ApprovalEntry,
  getActivityLog,
  getApprovals,
} from "../services/agentApi";

const RECENT_LIMIT = 8;

/** Normalized alert shown in the bell — unifies activity log + approvals. */
interface BellAlert {
  id: string;
  kind: "activity" | "approval";
  agentLabel: string;
  summary: string;
  outcome: string | null;
  riskLevel: string | null;
  timestamp: string;
}

const AGENT_LABELS: Record<string, string> = {
  delay_response_agent: "Delay Response",
  fuel_management_agent: "Fuel Management",
  sla_guardian_agent: "SLA Guardian",
  ai_agent: "AI Assistant",
  system: "System",
};

function agentLabel(agentId: string): string {
  return AGENT_LABELS[agentId] ?? agentId;
}

function activitySummary(entry: ActivityLogEntry): string {
  if (entry.action_type === "monitoring_cycle") {
    const detections =
      (entry.details as Record<string, unknown>)?.detection_count ?? 0;
    const actions =
      (entry.details as Record<string, unknown>)?.action_count ?? 0;
    return `Monitoring cycle: ${detections} detections, ${actions} actions`;
  }
  if (entry.tool_name) return `${entry.action_type}: ${entry.tool_name}`;
  return entry.action_type;
}

function activityToAlert(entry: ActivityLogEntry): BellAlert {
  return {
    id: entry.log_id,
    kind: "activity",
    agentLabel: agentLabel(entry.agent_id),
    summary: activitySummary(entry),
    outcome: entry.outcome,
    riskLevel: entry.risk_level,
    timestamp: entry.timestamp,
  };
}

function approvalToAlert(approval: ApprovalEntry): BellAlert {
  return {
    id: approval.action_id,
    kind: "approval",
    agentLabel: agentLabel(approval.proposed_by),
    summary:
      approval.impact_summary ||
      `${approval.action_type}: ${approval.tool_name}`,
    outcome: "pending_approval",
    riskLevel: approval.risk_level,
    timestamp: approval.proposed_at,
  };
}

function mergeAlert(prev: BellAlert[], next: BellAlert): BellAlert[] {
  const deduped = prev.filter((a) => a.id !== next.id);
  return [next, ...deduped].slice(0, RECENT_LIMIT);
}

function alertIcon(alert: BellAlert) {
  if (alert.kind === "approval")
    return <ShieldAlert className="w-3.5 h-3.5 text-warning" />;
  switch (alert.outcome) {
    case "success":
      return <CheckCircle2 className="w-3.5 h-3.5 text-success" />;
    case "failure":
    case "rejected":
      return <XCircle className="w-3.5 h-3.5 text-error" />;
    case "pending_approval":
      return <Clock className="w-3.5 h-3.5 text-warning" />;
    default:
      return <CheckCircle2 className="w-3.5 h-3.5 text-gray-400" />;
  }
}

function relativeTime(dateStr: string | null | undefined) {
  if (!dateStr) return "";
  const then = new Date(dateStr).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Date.now() - then;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
}

export default function NotificationBell() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<BellAlert[]>([]);
  const [pendingCount, setPendingCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);

  // Initial load: recent agent activity + any pending approvals, merged newest-first.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [activity, approvals] = await Promise.all([
          getActivityLog({ size: RECENT_LIMIT }),
          getApprovals(undefined, 1, RECENT_LIMIT).catch(() => null),
        ]);
        if (cancelled) return;
        // The approvals endpoint returns only pending items, with `total`
        // reflecting the full pending count — that's the actionable badge
        // number (it persists across reloads, unlike a "since last seen"
        // counter).
        setPendingCount(approvals?.total ?? 0);
        const merged: BellAlert[] = [
          ...(approvals?.entries ?? [])
            .filter((a) => a.status === "pending")
            .map(approvalToAlert),
          ...(activity.entries ?? []).map(activityToAlert),
        ]
          .sort((a, b) => b.timestamp.localeCompare(a.timestamp))
          .slice(0, RECENT_LIMIT);
        setItems(merged);
      } catch {
        // Non-fatal — the bell just starts empty until a live event arrives.
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // New agent activity is informational — it shows in the list but does not
  // change the badge, which tracks outstanding approvals only.
  const handleActivity = useCallback((entry: ActivityLogEntry) => {
    setItems((prev) => mergeAlert(prev, activityToAlert(entry)));
  }, []);

  const handleApprovalEvent = useCallback(
    (event: { type: string; approval: ApprovalEntry }) => {
      if (event.type === "approval_created") {
        setItems((prev) => mergeAlert(prev, approvalToAlert(event.approval)));
        setPendingCount((n) => n + 1);
      } else {
        // approved / rejected / expired — clear it from the attention list
        // and the badge.
        setItems((prev) =>
          prev.filter((a) => a.id !== event.approval.action_id),
        );
        setPendingCount((n) => Math.max(0, n - 1));
      }
    },
    [],
  );

  useAgentWebSocket({
    autoConnect: true,
    onActivity: handleActivity,
    onAgentAction: handleActivity,
    onApprovalEvent: handleApprovalEvent,
  });

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = () => setOpen((prev) => !prev);

  const goToCommand = () => {
    setOpen(false);
    router.push("/ops/command");
  };

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={toggle}
        aria-label={
          pendingCount > 0
            ? `Alerts, ${pendingCount} pending approval${pendingCount === 1 ? "" : "s"}`
            : "Alerts"
        }
        aria-haspopup="true"
        aria-expanded={open}
        className="relative flex items-center justify-center w-9 h-9 rounded-lg transition-colors hover:bg-[color-mix(in_srgb,var(--color-primary)_8%,transparent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-[color:var(--color-primary)]"
        style={{ color: "var(--color-primary)" }}
      >
        <Bell className="w-5 h-5" />
        {pendingCount > 0 && (
          <span
            className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 flex items-center justify-center rounded-full text-[10px] font-semibold text-white"
            style={{ backgroundColor: "var(--color-error)" }}
          >
            {pendingCount > 9 ? "9+" : pendingCount}
          </span>
        )}
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 mt-2 w-80 sm:w-96 max-h-[28rem] flex flex-col rounded-xl border bg-[color:var(--color-surface)] shadow-lg z-50"
          style={{
            borderColor:
              "color-mix(in srgb, var(--color-primary) 12%, transparent)",
          }}
        >
          <div
            className="flex items-center justify-between px-4 py-3 border-b"
            style={{
              borderColor:
                "color-mix(in srgb, var(--color-primary) 10%, transparent)",
            }}
          >
            <span
              className="text-sm font-semibold"
              style={{ color: "var(--color-primary)" }}
            >
              Agent Alerts
            </span>
            <button
              type="button"
              onClick={goToCommand}
              className="text-xs font-medium underline hover:no-underline focus:outline-none"
              style={{ color: "var(--color-primary)" }}
            >
              View all
            </button>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="px-4 py-8 text-center text-sm text-gray-500">
                Loading…
              </div>
            ) : items.length === 0 ? (
              <div className="px-4 py-10 text-center">
                <Bell className="w-8 h-8 mx-auto mb-2 text-gray-300" />
                <p className="text-sm text-gray-500">No agent activity yet</p>
              </div>
            ) : (
              <ul className="divide-y divide-gray-100">
                {items.map((alert) => (
                  <li key={`${alert.kind}-${alert.id}`}>
                    <button
                      type="button"
                      onClick={goToCommand}
                      className="w-full text-left px-4 py-3 transition-colors hover:bg-[color-mix(in_srgb,var(--color-primary)_5%,transparent)] focus:outline-none"
                    >
                      <div className="flex items-start gap-2">
                        <span className="mt-0.5">{alertIcon(alert)}</span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-sm font-medium text-primary truncate">
                              {alert.agentLabel}
                            </span>
                            <span className="text-[11px] text-gray-400 flex-shrink-0">
                              {relativeTime(alert.timestamp)}
                            </span>
                          </div>
                          <div className="text-xs text-gray-600 truncate">
                            {alert.summary}
                          </div>
                          <div className="mt-0.5 flex items-center gap-1.5">
                            {alert.kind === "approval" && (
                              <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-warning-light text-warning">
                                Needs approval
                              </span>
                            )}
                            {alert.riskLevel && (
                              <span
                                className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                                  alert.riskLevel === "high"
                                    ? "bg-error-light text-error"
                                    : alert.riskLevel === "medium"
                                      ? "bg-warning-light text-warning"
                                      : "bg-success-light text-success"
                                }`}
                              >
                                {alert.riskLevel}
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
