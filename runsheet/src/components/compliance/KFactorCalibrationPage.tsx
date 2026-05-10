"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  getKFactorDashboard,
  approveKFactorAdjustment,
  type KFactorEntry,
  type KFactorDashboard,
} from "../../services/complianceApi";

// ─── Status badge helper ─────────────────────────────────────────────────────

function statusBadge(status: KFactorEntry["status"]): {
  label: string;
  className: string;
} {
  switch (status) {
    case "ok":
      return { label: "OK", className: "bg-green-100 text-green-800" };
    case "review_needed":
      return { label: "Review Needed", className: "bg-yellow-100 text-yellow-800" };
    case "insufficient_data":
      return { label: "Insufficient Data", className: "bg-gray-100 text-gray-800" };
    default:
      return { label: status, className: "bg-gray-100 text-gray-800" };
  }
}

function formatPercent(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`;
}

function formatKFactor(value: number | null): string {
  if (value === null) return "—";
  return value.toFixed(4);
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function KFactorCalibrationPage() {
  const [dashboard, setDashboard] = useState<KFactorDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Approval dialog state
  const [approvalTarget, setApprovalTarget] = useState<KFactorEntry | null>(null);
  const [approving, setApproving] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);

  // ─── Fetch dashboard ───────────────────────────────────────────────────────

  const fetchDashboard = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getKFactorDashboard();
      setDashboard(response.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load K-factor dashboard",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDashboard();
  }, [fetchDashboard]);

  // ─── Approval workflow ─────────────────────────────────────────────────────

  function handleApproveClick(entry: KFactorEntry) {
    setApprovalTarget(entry);
    setApprovalError(null);
  }

  function handleCancelApproval() {
    setApprovalTarget(null);
    setApprovalError(null);
  }

  async function handleConfirmApproval() {
    if (!approvalTarget || approvalTarget.suggested_k_factor === null) return;

    setApproving(true);
    setApprovalError(null);
    try {
      await approveKFactorAdjustment(approvalTarget.tank_id, {
        new_kfactor: approvalTarget.suggested_k_factor,
        operator_id: "current_user", // In production, this would come from auth context
      });
      setApprovalTarget(null);
      // Refresh dashboard after approval
      await fetchDashboard();
    } catch (err) {
      setApprovalError(
        err instanceof Error ? err.message : "Failed to approve K-factor adjustment",
      );
    } finally {
      setApproving(false);
    }
  }

  // ─── Sort entries by variance (highest absolute variance first) ────────────

  const sortedEntries = dashboard?.entries
    ? [...dashboard.entries].sort(
        (a, b) => Math.abs(b.cumulative_variance_percent) - Math.abs(a.cumulative_variance_percent),
      )
    : [];

  // ─── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-bold">K-Factor Calibration</h1>
        <p className="text-gray-600 mt-1">
          Monitor tank K-factor variance and approve recalibration adjustments for auto-fill forecasting accuracy.
        </p>
      </header>

      {/* Loading state */}
      {loading && (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading K-factor dashboard...</span>
          <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
        </div>
      )}

      {/* Error state */}
      {!loading && error && (
        <div
          role="alert"
          className="bg-red-50 border border-red-200 text-red-700 p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* Dashboard content */}
      {!loading && !error && dashboard && (
        <>
          {/* Summary cards */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
              <div className="text-sm text-yellow-700 font-medium">Review Needed</div>
              <div className="text-2xl font-bold text-yellow-800 mt-1">
                {dashboard.total_review_needed}
              </div>
              <div className="text-xs text-yellow-600 mt-1">
                Tanks with variance exceeding threshold
              </div>
            </div>
            <div className="bg-gray-50 border border-gray-200 rounded-lg p-4">
              <div className="text-sm text-gray-700 font-medium">Insufficient Data</div>
              <div className="text-2xl font-bold text-gray-800 mt-1">
                {dashboard.total_insufficient_data}
              </div>
              <div className="text-xs text-gray-600 mt-1">
                Tanks with fewer than 3 deliveries
              </div>
            </div>
          </div>

          {/* Dashboard table */}
          <div className="overflow-x-auto">
            <table className="w-full border-collapse" role="table">
              <thead>
                <tr className="border-b bg-gray-50">
                  <th className="text-left p-3 font-medium">Tank ID</th>
                  <th className="text-left p-3 font-medium">Customer</th>
                  <th className="text-left p-3 font-medium">Current K-Factor</th>
                  <th className="text-left p-3 font-medium">Suggested K-Factor</th>
                  <th className="text-left p-3 font-medium">Cumulative Variance</th>
                  <th className="text-left p-3 font-medium">Status</th>
                  <th className="text-left p-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {sortedEntries.map((entry) => {
                  const badge = statusBadge(entry.status);
                  return (
                    <tr
                      key={entry.tank_id}
                      className="border-b hover:bg-gray-50"
                    >
                      <td className="p-3 font-medium">{entry.tank_id}</td>
                      <td className="p-3">{entry.customer_name}</td>
                      <td className="p-3 font-mono text-sm">
                        {formatKFactor(entry.current_k_factor)}
                      </td>
                      <td className="p-3 font-mono text-sm">
                        {formatKFactor(entry.suggested_k_factor)}
                      </td>
                      <td className="p-3">
                        <span
                          className={
                            Math.abs(entry.cumulative_variance_percent) > 15
                              ? "text-red-600 font-medium"
                              : "text-gray-700"
                          }
                        >
                          {formatPercent(entry.cumulative_variance_percent)}
                        </span>
                      </td>
                      <td className="p-3">
                        <span
                          className={`inline-block px-2 py-1 rounded text-xs font-medium ${badge.className}`}
                        >
                          {badge.label}
                        </span>
                      </td>
                      <td className="p-3">
                        {entry.status === "review_needed" && entry.suggested_k_factor !== null && (
                          <button
                            type="button"
                            onClick={() => handleApproveClick(entry)}
                            className="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-700"
                          >
                            Approve
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {sortedEntries.length === 0 && (
                  <tr>
                    <td
                      colSpan={7}
                      className="p-6 text-center text-gray-500"
                    >
                      No K-factor calibration data available.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* Approval confirmation dialog */}
      {approvalTarget && (
        <div
          className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50"
          role="dialog"
          aria-modal="true"
          aria-labelledby="approval-dialog-title"
        >
          <div className="bg-white rounded-lg shadow-xl p-6 max-w-md w-full mx-4">
            <h2
              id="approval-dialog-title"
              className="text-lg font-bold mb-4"
            >
              Confirm K-Factor Adjustment
            </h2>

            <div className="space-y-3 mb-6">
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">Tank ID:</span>
                <span className="font-medium">{approvalTarget.tank_id}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">Customer:</span>
                <span className="font-medium">{approvalTarget.customer_name}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">Current K-Factor:</span>
                <span className="font-mono">{formatKFactor(approvalTarget.current_k_factor)}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">New K-Factor:</span>
                <span className="font-mono font-bold text-blue-700">
                  {formatKFactor(approvalTarget.suggested_k_factor)}
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">Cumulative Variance:</span>
                <span className="font-medium">
                  {formatPercent(approvalTarget.cumulative_variance_percent)}
                </span>
              </div>
            </div>

            <p className="text-sm text-gray-600 mb-4">
              This will update the tank&apos;s K-factor and notify the tank forecasting agent. This action is logged for audit purposes.
            </p>

            {/* Approval error */}
            {approvalError && (
              <div
                role="alert"
                className="bg-red-50 border border-red-200 text-red-700 p-3 rounded mb-4 text-sm"
              >
                {approvalError}
              </div>
            )}

            <div className="flex gap-3 justify-end">
              <button
                type="button"
                onClick={handleCancelApproval}
                disabled={approving}
                className="px-4 py-2 border rounded hover:bg-gray-50 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleConfirmApproval}
                disabled={approving}
                className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 disabled:opacity-50"
              >
                {approving ? "Approving..." : "Confirm Approval"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
