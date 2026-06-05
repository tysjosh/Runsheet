"use client";

import { useCallback, useEffect, useState } from "react";
import { PageHeader } from "@/components/ui";
import {
  approveKFactorAdjustment,
  getKFactorDashboard,
  type KFactorEntry,
} from "../../services/complianceApi";

// ─── Derived status ──────────────────────────────────────────────────────────

type KFactorStatus = "ok" | "review_needed" | "insufficient_data";

/**
 * Derive a display status from the backend entry. The backend exposes
 * ``read_only`` (insufficient deliveries) and a ``suggested_kfactor``
 * (present only when variance exceeds the recalibration threshold).
 */
function deriveStatus(entry: KFactorEntry): KFactorStatus {
  if (entry.read_only) return "insufficient_data";
  if (entry.suggested_kfactor !== null) return "review_needed";
  return "ok";
}

// ─── Status badge helper ─────────────────────────────────────────────────────

function statusBadge(status: KFactorStatus): {
  label: string;
  className: string;
} {
  switch (status) {
    case "ok":
      return { label: "OK", className: "bg-success-light text-success-dark" };
    case "review_needed":
      return {
        label: "Review Needed",
        className: "bg-warning-light text-warning-dark",
      };
    case "insufficient_data":
      return {
        label: "Insufficient Data",
        className: "bg-gray-100 text-gray-800",
      };
    default:
      return { label: status, className: "bg-gray-100 text-gray-800" };
  }
}

function formatPercent(value: number | null): string {
  if (value === null) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`;
}

function formatKFactor(value: number | null): string {
  if (value === null) return "—";
  return value.toFixed(4);
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function KFactorCalibrationPage() {
  const [entries, setEntries] = useState<KFactorEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Approval dialog state
  const [approvalTarget, setApprovalTarget] = useState<KFactorEntry | null>(
    null,
  );
  const [approving, setApproving] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);

  // ─── Fetch dashboard ───────────────────────────────────────────────────────

  const fetchDashboard = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getKFactorDashboard();
      setEntries(response.data ?? []);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load K-factor dashboard",
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
    if (!approvalTarget || approvalTarget.suggested_kfactor === null) return;

    setApproving(true);
    setApprovalError(null);
    try {
      await approveKFactorAdjustment(approvalTarget.tank_id, {
        new_kfactor: approvalTarget.suggested_kfactor,
        operator_id: "current_user", // In production, this would come from auth context
      });
      setApprovalTarget(null);
      // Refresh dashboard after approval
      await fetchDashboard();
    } catch (err) {
      setApprovalError(
        err instanceof Error
          ? err.message
          : "Failed to approve K-factor adjustment",
      );
    } finally {
      setApproving(false);
    }
  }

  // ─── Derive view model from the flat entry list ────────────────────────────

  // Annotate each entry with its derived status, then sort by absolute
  // variance (highest first; null variance sorts last).
  const decoratedEntries = entries.map((entry) => ({
    entry,
    status: deriveStatus(entry),
  }));

  const sortedEntries = [...decoratedEntries].sort(
    (a, b) =>
      Math.abs(b.entry.variance_percent ?? 0) -
      Math.abs(a.entry.variance_percent ?? 0),
  );

  const totalReviewNeeded = decoratedEntries.filter(
    (d) => d.status === "review_needed",
  ).length;
  const totalInsufficientData = decoratedEntries.filter(
    (d) => d.status === "insufficient_data",
  ).length;

  // ─── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="p-6">
      <PageHeader
        title="K-Factor Calibration"
        subtitle="Monitor tank K-factor variance and approve recalibration adjustments for auto-fill forecasting accuracy.
        "
      />

      {/* Loading state */}
      {loading && (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading K-factor dashboard...</span>
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      )}

      {/* Error state */}
      {!loading && error && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* Dashboard content */}
      {!loading && !error && (
        <>
          {/* Summary cards */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            <div className="bg-warning-light border border-warning-light rounded-lg p-4">
              <div className="text-sm text-warning-dark font-medium">
                Review Needed
              </div>
              <div className="text-2xl font-bold text-warning-dark mt-1">
                {totalReviewNeeded}
              </div>
              <div className="text-xs text-warning mt-1">
                Tanks with variance exceeding threshold
              </div>
            </div>
            <div className="bg-gray-50 border border-gray-200 rounded-lg p-4">
              <div className="text-sm text-gray-700 font-medium">
                Insufficient Data
              </div>
              <div className="text-2xl font-bold text-gray-800 mt-1">
                {totalInsufficientData}
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
                  <th className="text-left p-3 font-medium">
                    Current K-Factor
                  </th>
                  <th className="text-left p-3 font-medium">
                    Suggested K-Factor
                  </th>
                  <th className="text-left p-3 font-medium">
                    Cumulative Variance
                  </th>
                  <th className="text-left p-3 font-medium">Status</th>
                  <th className="text-left p-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {sortedEntries.map(({ entry, status }) => {
                  const badge = statusBadge(status);
                  const variance = entry.variance_percent;
                  return (
                    <tr
                      key={entry.tank_id}
                      className="border-b hover:bg-gray-50"
                    >
                      <td className="p-3 font-medium">{entry.tank_id}</td>
                      <td className="p-3">{entry.customer_id}</td>
                      <td className="p-3 font-mono text-sm">
                        {formatKFactor(entry.current_kfactor)}
                      </td>
                      <td className="p-3 font-mono text-sm">
                        {formatKFactor(entry.suggested_kfactor)}
                      </td>
                      <td className="p-3">
                        <span
                          className={
                            variance !== null && Math.abs(variance) > 15
                              ? "text-error font-medium"
                              : "text-gray-700"
                          }
                        >
                          {formatPercent(variance)}
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
                        {status === "review_needed" &&
                          entry.suggested_kfactor !== null && (
                            <button
                              type="button"
                              onClick={() => handleApproveClick(entry)}
                              className="bg-primary text-white px-3 py-1 rounded text-sm hover:bg-primary-hover"
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
                    <td colSpan={7} className="p-6 text-center text-gray-500">
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
            <h2 id="approval-dialog-title" className="text-lg font-bold mb-4">
              Confirm K-Factor Adjustment
            </h2>

            <div className="space-y-3 mb-6">
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">Tank ID:</span>
                <span className="font-medium">{approvalTarget.tank_id}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">Customer:</span>
                <span className="font-medium">
                  {approvalTarget.customer_id}
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">Current K-Factor:</span>
                <span className="font-mono">
                  {formatKFactor(approvalTarget.current_kfactor)}
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">New K-Factor:</span>
                <span className="font-mono font-bold text-info-dark">
                  {formatKFactor(approvalTarget.suggested_kfactor)}
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-600">Cumulative Variance:</span>
                <span className="font-medium">
                  {formatPercent(approvalTarget.variance_percent)}
                </span>
              </div>
            </div>

            <p className="text-sm text-gray-600 mb-4">
              This will update the tank&apos;s K-factor and notify the tank
              forecasting agent. This action is logged for audit purposes.
            </p>

            {/* Approval error */}
            {approvalError && (
              <div
                role="alert"
                className="bg-error-light border border-error-light text-error-dark p-3 rounded mb-4 text-sm"
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
                className="bg-primary text-white px-4 py-2 rounded hover:bg-primary-hover disabled:opacity-50"
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
