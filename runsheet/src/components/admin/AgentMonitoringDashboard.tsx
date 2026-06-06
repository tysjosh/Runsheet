"use client";

import {
  Activity,
  AlertCircle,
  Bot,
  Check,
  CheckCircle,
  Clock,
  Pause,
  Play,
  TrendingUp,
  X,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  type ActivityLogEntry,
  type ActivityStats,
  type AgentHealth,
  type ApprovalEntry,
  approveAction,
  getActivityLog,
  getActivityStats,
  getAgentHealth,
  getApprovals,
  pauseAgent,
  rejectAction,
  resumeAgent,
} from "../../services/adminApi";
import { Badge, Button, type Column, Modal, PageHeader, Table } from "../ui";

// ─── Helper Functions ────────────────────────────────────────────────────────

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

function formatTimestamp(timestamp: string): string {
  return new Date(timestamp).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function getStatusBadge(status: string) {
  switch (status.toLowerCase()) {
    case "running":
      return <Badge variant="success">Running</Badge>;
    case "stopped":
    case "paused":
      return <Badge variant="default">Stopped</Badge>;
    case "error":
      return <Badge variant="error">Error</Badge>;
    default:
      return <Badge variant="default">{status}</Badge>;
  }
}

function getOutcomeBadge(outcome: string) {
  switch (outcome.toLowerCase()) {
    case "success":
      return <Badge variant="success">Success</Badge>;
    case "failure":
    case "error":
      return <Badge variant="error">Failed</Badge>;
    case "pending":
      return <Badge variant="warning">Pending</Badge>;
    default:
      return <Badge variant="default">{outcome}</Badge>;
  }
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function AgentMonitoringDashboard() {
  const [agents, setAgents] = useState<Record<string, AgentHealth>>({});
  const [activityLog, setActivityLog] = useState<ActivityLogEntry[]>([]);
  const [stats, setStats] = useState<ActivityStats | null>(null);
  const [approvals, setApprovals] = useState<ApprovalEntry[]>([]);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<
    "agents" | "activity" | "approvals"
  >("agents");
  const [selectedApproval, setSelectedApproval] =
    useState<ApprovalEntry | null>(null);
  const [showApprovalModal, setShowApprovalModal] = useState(false);
  const [rejectReason, setRejectReason] = useState("");

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [healthData, activityData, statsData, approvalsData] =
        await Promise.all([
          getAgentHealth(),
          getActivityLog({ page: 1, size: 50 }),
          getActivityStats(),
          getApprovals({ page: 1, size: 20 }),
        ]);
      setAgents(healthData.agents);
      setActivityLog(activityData.items);
      setStats(statsData);
      setApprovals(approvalsData.items);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load agent monitoring data",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handlePauseAgent = async (agentId: string) => {
    setError(null);
    setSuccess(null);
    try {
      await pauseAgent(agentId);
      setSuccess(`Agent ${agentId} paused successfully`);
      await fetchData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to pause agent");
    }
  };

  const handleResumeAgent = async (agentId: string) => {
    setError(null);
    setSuccess(null);
    try {
      await resumeAgent(agentId);
      setSuccess(`Agent ${agentId} resumed successfully`);
      await fetchData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to resume agent");
    }
  };

  const handleApprove = async (actionId: string) => {
    setError(null);
    setSuccess(null);
    try {
      await approveAction(actionId);
      setSuccess("Action approved successfully");
      setShowApprovalModal(false);
      setSelectedApproval(null);
      await fetchData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to approve action");
    }
  };

  const handleReject = async (actionId: string) => {
    setError(null);
    setSuccess(null);
    try {
      await rejectAction(actionId, rejectReason);
      setSuccess("Action rejected successfully");
      setShowApprovalModal(false);
      setSelectedApproval(null);
      setRejectReason("");
      await fetchData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reject action");
    }
  };

  const agentList = Object.values(agents);

  const agentColumns: Column<AgentHealth>[] = [
    {
      key: "agent_id",
      label: "Agent ID",
      render: (agent) => (
        <div className="flex items-center gap-2">
          <Bot className="w-4 h-4 text-primary" />
          <span className="font-mono text-sm">{agent.agent_id}</span>
        </div>
      ),
    },
    {
      key: "type",
      label: "Type",
      render: (agent) => (
        <Badge variant={agent.type === "autonomous" ? "info" : "default"}>
          {agent.type}
        </Badge>
      ),
    },
    {
      key: "status",
      label: "Status",
      render: (agent) => getStatusBadge(agent.status),
    },
    {
      key: "actions",
      label: "Actions",
      render: (agent) => (
        <div className="flex items-center gap-2">
          {agent.status === "running" ? (
            <button
              type="button"
              onClick={() => handlePauseAgent(agent.agent_id)}
              className="text-sm text-orange-600 hover:text-orange-800 flex items-center gap-1"
            >
              <Pause className="w-4 h-4" />
              Pause
            </button>
          ) : (
            <button
              type="button"
              onClick={() => handleResumeAgent(agent.agent_id)}
              className="text-sm text-green-600 hover:text-green-800 flex items-center gap-1"
            >
              <Play className="w-4 h-4" />
              Resume
            </button>
          )}
        </div>
      ),
    },
  ];

  const activityColumns: Column<ActivityLogEntry>[] = [
    {
      key: "timestamp",
      label: "Time",
      render: (entry) => (
        <span className="text-sm text-gray-600">
          {entry.timestamp ? formatTimestamp(entry.timestamp) : "—"}
        </span>
      ),
    },
    {
      key: "agent_id",
      label: "Agent",
      render: (entry) => (
        <span className="font-mono text-sm">{entry.agent_id}</span>
      ),
    },
    {
      key: "action_type",
      label: "Action",
      render: (entry) => <Badge variant="default">{entry.action_type}</Badge>,
    },
    {
      key: "tool_name",
      label: "Tool",
      render: (entry) => (
        <span className="text-sm text-gray-700">{entry.tool_name || "—"}</span>
      ),
    },
    {
      key: "outcome",
      label: "Outcome",
      render: (entry) => getOutcomeBadge(entry.outcome),
    },
    {
      key: "duration_ms",
      label: "Duration",
      render: (entry) => (
        <span className="text-sm text-gray-700">
          {formatDuration(entry.duration_ms)}
        </span>
      ),
    },
  ];

  const approvalColumns: Column<ApprovalEntry>[] = [
    {
      key: "proposed_at",
      label: "Proposed",
      render: (entry) => (
        <span className="text-sm text-gray-600">
          {formatTimestamp(entry.proposed_at)}
        </span>
      ),
    },
    {
      key: "agent_id",
      label: "Agent",
      render: (entry) => (
        <span className="font-mono text-sm">{entry.agent_id}</span>
      ),
    },
    {
      key: "action_type",
      label: "Action",
      render: (entry) => <Badge variant="default">{entry.action_type}</Badge>,
    },
    {
      key: "risk_level",
      label: "Risk",
      render: (entry) => {
        const risk = entry.risk_level?.toLowerCase();
        return (
          <Badge
            variant={
              risk === "high"
                ? "error"
                : risk === "medium"
                  ? "warning"
                  : "default"
            }
          >
            {entry.risk_level || "Low"}
          </Badge>
        );
      },
    },
    {
      key: "actions",
      label: "Actions",
      render: (entry) => (
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => {
              setSelectedApproval(entry);
              setShowApprovalModal(true);
            }}
            className="text-sm text-blue-600 hover:text-blue-800"
          >
            Review
          </button>
        </div>
      ),
    },
  ];

  return (
    <div className="p-6">
      <PageHeader
        title="Agent Monitoring"
        subtitle="Monitor AI agent activity and manage approvals"
        icon={<Activity className="w-5 h-5" />}
      />

      {/* Error/Success Messages */}
      {error && (
        <div className="bg-error-light border border-error text-error-dark p-3 rounded-lg mb-4 flex items-center gap-2">
          <AlertCircle className="w-5 h-5 flex-shrink-0" />
          <span className="text-sm">{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="ml-auto"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {success && (
        <div className="bg-success-light border border-success text-success-dark p-3 rounded-lg mb-4 flex items-center gap-2">
          <Check className="w-5 h-5 flex-shrink-0" />
          <span className="text-sm">{success}</span>
          <button
            type="button"
            onClick={() => setSuccess(null)}
            className="ml-auto"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
          <div className="bg-white rounded-xl border border-gray-200 p-4">
            <div className="flex items-center gap-2 mb-2">
              <Activity className="w-4 h-4 text-blue-500" />
              <span className="text-sm font-medium text-gray-700">
                Total Actions
              </span>
            </div>
            <div className="text-2xl font-bold text-gray-900">
              {stats.total_actions}
            </div>
          </div>

          <div className="bg-white rounded-xl border border-gray-200 p-4">
            <div className="flex items-center gap-2 mb-2">
              <CheckCircle className="w-4 h-4 text-green-500" />
              <span className="text-sm font-medium text-gray-700">
                Success Rate
              </span>
            </div>
            <div className="text-2xl font-bold text-gray-900">
              {(stats.success_rate * 100).toFixed(1)}%
            </div>
          </div>

          <div className="bg-white rounded-xl border border-gray-200 p-4">
            <div className="flex items-center gap-2 mb-2">
              <Clock className="w-4 h-4 text-purple-500" />
              <span className="text-sm font-medium text-gray-700">
                Avg Duration
              </span>
            </div>
            <div className="text-2xl font-bold text-gray-900">
              {formatDuration(stats.average_duration_ms)}
            </div>
          </div>

          <div className="bg-white rounded-xl border border-gray-200 p-4">
            <div className="flex items-center gap-2 mb-2">
              <TrendingUp className="w-4 h-4 text-orange-500" />
              <span className="text-sm font-medium text-gray-700">
                Pending Approvals
              </span>
            </div>
            <div className="text-2xl font-bold text-gray-900">
              {approvals.length}
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="bg-white rounded-xl border border-gray-200 mb-6">
        <div className="border-b border-gray-200 px-6">
          <nav className="flex gap-6">
            {[
              { id: "agents" as const, label: "Agents" },
              { id: "activity" as const, label: "Activity Log" },
              { id: "approvals" as const, label: "Pending Approvals" },
            ].map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`px-4 py-3 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === tab.id
                    ? "border-primary text-primary"
                    : "border-transparent text-gray-500 hover:text-gray-700"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </nav>
        </div>

        <div className="p-6">
          {loading ? (
            <div className="flex justify-center py-12">
              <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
            </div>
          ) : (
            <>
              {activeTab === "agents" && (
                <Table columns={agentColumns} data={agentList} />
              )}
              {activeTab === "activity" && (
                <Table columns={activityColumns} data={activityLog} />
              )}
              {activeTab === "approvals" &&
                (approvals.length === 0 ? (
                  <div className="text-center py-12 text-gray-500">
                    <CheckCircle className="w-12 h-12 text-gray-400 mx-auto mb-4" />
                    <p>No pending approvals</p>
                  </div>
                ) : (
                  <Table columns={approvalColumns} data={approvals} />
                ))}
            </>
          )}
        </div>
      </div>

      {/* Approval Modal */}
      {selectedApproval && (
        <Modal
          isOpen={showApprovalModal}
          onClose={() => {
            setShowApprovalModal(false);
            setSelectedApproval(null);
            setRejectReason("");
          }}
          title="Review Action"
        >
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Agent ID
              </label>
              <div className="font-mono text-sm bg-gray-50 p-3 rounded border border-gray-200">
                {selectedApproval.agent_id}
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Action Type
              </label>
              <div className="text-sm bg-gray-50 p-3 rounded border border-gray-200">
                {selectedApproval.action_type}
              </div>
            </div>

            {selectedApproval.tool_name && (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  Tool
                </label>
                <div className="text-sm bg-gray-50 p-3 rounded border border-gray-200">
                  {selectedApproval.tool_name}
                </div>
              </div>
            )}

            {selectedApproval.parameters && (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  Parameters
                </label>
                <pre className="text-xs bg-gray-50 p-3 rounded border border-gray-200 overflow-auto max-h-40">
                  {JSON.stringify(selectedApproval.parameters, null, 2)}
                </pre>
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Rejection Reason (optional)
              </label>
              <textarea
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                placeholder="Enter reason for rejection..."
                rows={3}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary focus:border-primary"
              />
            </div>

            <div className="flex justify-end gap-3 pt-4">
              <Button
                variant="danger"
                onClick={() => handleReject(selectedApproval.action_id)}
                disabled={loading}
              >
                <XCircle className="w-4 h-4 mr-2" />
                Reject
              </Button>
              <Button
                variant="success"
                onClick={() => handleApprove(selectedApproval.action_id)}
                disabled={loading}
              >
                <CheckCircle className="w-4 h-4 mr-2" />
                Approve
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
