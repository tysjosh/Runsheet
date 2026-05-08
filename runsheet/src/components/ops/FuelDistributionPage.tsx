"use client";

/**
 * Fuel Distribution MVP Pipeline page.
 *
 * Tabbed layout with three tabs:
 * - Plans: Generate plans, view plan details, trigger replanning
 * - Forecasts: Paginated tank forecasts with station/fuel_grade filters
 * - Priorities: Paginated delivery priority rankings
 *
 * Plan Execution Lifecycle features:
 * - Plan list fetched from backend with pagination and status filter
 * - Status badges with color coding (draft=gray, dispatched=blue, completed=green, rejected=red)
 * - Approve/Reject buttons for draft plans with confirmation dialog
 * - Execution progress with real-time WebSocket updates for dispatched plans
 * - Outcome comparison table for completed plans
 * - Cost analysis breakdown for all plans
 * - Toast notifications for success/error feedback
 *
 * Validates:
 * - Requirement 1.1: Generate Plan button triggers POST /api/fuel/mvp/plan/generate
 * - Requirement 1.2: Display plan status and run_id
 * - Requirement 1.3: Display loading plan and route plan details
 * - Requirement 1.4: Replan form with disruption_type, description, entity_id
 * - Requirement 1.5: Paginated forecasts with station_id and fuel_grade filters
 * - Requirement 1.6: Paginated delivery priority rankings
 * - Requirement 1.7: Error handling with message display and form state retention
 */

import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  DollarSign,
  Droplets,
  Eye,
  GitBranch,
  Layers,
  Loader2,
  MapPin,
  Play,
  RefreshCw,
  Route,
  Settings,
  Siren,
  Truck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ExecutionUpdateData } from "../../hooks/usePlanExecutionSocket";
import { usePlanExecutionSocket } from "../../hooks/usePlanExecutionSocket";
import type {
  CompartmentAssignment,
  CostBreakdown,
  CostConfig,
  EmergencyStopRequest,
  EmergencyStopResponse,
  Forecast,
  PaginationMeta,
  PlanDetail,
  PlanListItem,
  PlanOutcome,
  PriorityEntry,
  PriorityListEntry,
  ReplanDiff,
  ReplanDiffResponse,
  ReplanRequest,
  ReplanResponse,
  RouteAssignment,
  SafeToDelayBucket,
  StopVariance,
} from "../../services/fuelApi";
import {
  approvePlan,
  generatePlan,
  getForecasts,
  getPlan,
  getPlanCosts,
  getPlanOutcomes,
  getPriorityLists,
  getReplanDiff,
  insertEmergencyStop,
  listPlans,
  rejectPlan,
  replan,
  updateCostConfig,
} from "../../services/fuelApi";
import StormModeBanner from "./StormModeBanner";

// ─── Constants ───────────────────────────────────────────────────────────────

const TENANT_ID = "dev-tenant";
const DISPATCHER_ID = "dispatcher-001";
const PAGE_SIZE = 10;

const TABS = [
  { id: "plans", label: "Plans", icon: Truck },
  { id: "forecasts", label: "Forecasts", icon: Droplets },
  { id: "priorities", label: "Priorities", icon: MapPin },
] as const;

type TabId = (typeof TABS)[number]["id"];

const URGENCY_CONFIG: Record<string, { color: string; bg: string }> = {
  low: { color: "text-green-700", bg: "bg-green-100" },
  medium: { color: "text-yellow-700", bg: "bg-yellow-100" },
  high: { color: "text-orange-700", bg: "bg-orange-100" },
  critical: { color: "text-red-700", bg: "bg-red-100" },
};

const STATUS_BADGE_CONFIG: Record<string, { color: string; bg: string }> = {
  draft: { color: "text-gray-700", bg: "bg-gray-100" },
  proposed: { color: "text-gray-700", bg: "bg-gray-100" },
  dispatched: { color: "text-blue-700", bg: "bg-blue-100" },
  completed: { color: "text-green-700", bg: "bg-green-100" },
  rejected: { color: "text-red-700", bg: "bg-red-100" },
};

const VARIANCE_THRESHOLD = 5; // 5% threshold for color coding

// Statuses that allow approve/reject actions
const APPROVABLE_STATUSES = ["draft", "proposed"];

// ─── Toast Notification System ───────────────────────────────────────────────

interface Toast {
  id: number;
  message: string;
  type: "success" | "error";
}

let toastIdCounter = 0;

function ToastContainer({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed top-4 right-4 z-[100] space-y-2">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm font-medium ${
            toast.type === "success"
              ? "bg-green-600 text-white"
              : "bg-red-600 text-white"
          }`}
        >
          {toast.type === "success" ? (
            <Check className="w-4 h-4" />
          ) : (
            <AlertTriangle className="w-4 h-4" />
          )}
          <span>{toast.message}</span>
          <button
            onClick={() => onDismiss(toast.id)}
            className="ml-2 p-0.5 hover:bg-white/20 rounded"
            aria-label="Dismiss notification"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      ))}
    </div>
  );
}

function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: "success" | "error") => {
    const id = ++toastIdCounter;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return { toasts, addToast, dismissToast };
}

// ─── Status Badge Component ──────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const config = STATUS_BADGE_CONFIG[status] ?? STATUS_BADGE_CONFIG.draft;
  return (
    <span
      className={`inline-flex items-center text-[10px] px-2 py-0.5 rounded font-medium ${config.bg} ${config.color}`}
    >
      {status}
    </span>
  );
}

// ─── Rejection Confirmation Dialog ───────────────────────────────────────────

interface RejectDialogProps {
  planId: string;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
  loading: boolean;
}

function RejectDialog({
  planId,
  onConfirm,
  onCancel,
  loading,
}: RejectDialogProps) {
  const [reason, setReason] = useState("");

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-[#232323]">Reject Plan</h2>
          <button
            onClick={onCancel}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close rejection dialog"
            disabled={loading}
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="px-6 py-4 space-y-4">
          <p className="text-sm text-gray-600">
            Are you sure you want to reject plan{" "}
            <span className="font-medium">{planId}</span>?
          </p>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Reason (optional)
            </label>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Enter rejection reason..."
              rows={3}
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white resize-none"
              disabled={loading}
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onCancel}
              disabled={loading}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => onConfirm(reason)}
              disabled={loading}
              className="px-4 py-2 text-sm text-white bg-red-600 hover:bg-red-700 rounded-lg disabled:opacity-50"
            >
              {loading ? "Rejecting..." : "Reject Plan"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Cost Configuration Panel ────────────────────────────────────────────────

interface CostConfigPanelProps {
  onClose: () => void;
  onSave: () => void;
  addToast: (message: string, type: "success" | "error") => void;
}

function CostConfigPanel({ onClose, onSave, addToast }: CostConfigPanelProps) {
  const [config, setConfig] = useState<CostConfig>({
    fuel_consumption_rate: 0.35,
    fuel_price_per_liter: 1.5,
    driver_hourly_rate: 25,
    currency: "USD",
  });
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      await updateCostConfig(TENANT_ID, config);
      addToast("Cost configuration saved", "success");
      onSave();
      onClose();
    } catch (err) {
      addToast(
        err instanceof Error ? err.message : "Failed to save cost config",
        "error",
      );
    } finally {
      setSaving(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-[#232323]">
            Cost Configuration
          </h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close cost configuration"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="px-6 py-4 space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Fuel Consumption Rate (L/km)
            </label>
            <input
              type="number"
              step="0.01"
              value={config.fuel_consumption_rate}
              onChange={(e) =>
                setConfig({
                  ...config,
                  fuel_consumption_rate: parseFloat(e.target.value) || 0,
                })
              }
              className={inputClass}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Fuel Price per Liter ({config.currency})
            </label>
            <input
              type="number"
              step="0.01"
              value={config.fuel_price_per_liter}
              onChange={(e) =>
                setConfig({
                  ...config,
                  fuel_price_per_liter: parseFloat(e.target.value) || 0,
                })
              }
              className={inputClass}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Driver Hourly Rate ({config.currency})
            </label>
            <input
              type="number"
              step="0.5"
              value={config.driver_hourly_rate}
              onChange={(e) =>
                setConfig({
                  ...config,
                  driver_hourly_rate: parseFloat(e.target.value) || 0,
                })
              }
              className={inputClass}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Currency
            </label>
            <select
              value={config.currency}
              onChange={(e) =>
                setConfig({ ...config, currency: e.target.value })
              }
              className={inputClass}
            >
              <option value="USD">USD</option>
              <option value="EUR">EUR</option>
              <option value="GBP">GBP</option>
              <option value="NGN">NGN</option>
              <option value="KES">KES</option>
            </select>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving}
              className="px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              {saving ? "Saving..." : "Save Configuration"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Execution Progress Section ──────────────────────────────────────────────

interface ExecutionProgressProps {
  planId: string;
  executionData?: ExecutionUpdateData | null;
}

function ExecutionProgress({ planId, executionData }: ExecutionProgressProps) {
  const completedStops = executionData?.completed_stops ?? 0;
  const totalStops = executionData?.total_stops ?? 0;
  const percentage =
    totalStops > 0 ? Math.round((completedStops / totalStops) * 100) : 0;

  return (
    <div className="border border-blue-100 rounded-lg p-4 bg-blue-50/30">
      <div className="flex items-center gap-2 mb-3">
        <Route className="w-4 h-4 text-blue-600" />
        <h4 className="text-sm font-medium text-[#232323]">
          Execution Progress
        </h4>
        <StatusBadge status="dispatched" />
      </div>

      {/* Progress bar */}
      <div className="mb-3">
        <div className="flex items-center justify-between text-xs text-gray-600 mb-1">
          <span>
            {completedStops} / {totalStops} stops completed
          </span>
          <span className="font-medium">{percentage}%</span>
        </div>
        <div className="w-full h-2.5 bg-gray-200 rounded-full overflow-hidden">
          <div
            className="h-full bg-blue-600 rounded-full transition-all duration-500"
            style={{ width: `${percentage}%` }}
          />
        </div>
      </div>

      {/* Last update info */}
      {executionData?.updated_at && (
        <p className="text-xs text-gray-500">
          Last update: {new Date(executionData.updated_at).toLocaleString()}
        </p>
      )}

      {/* Stop status (from WebSocket data) */}
      {executionData?.stop && (
        <div className="mt-3 text-xs">
          <p className="text-gray-600">
            Latest: Stop #{executionData.stop.sequence} (
            {executionData.stop.station_id}) —{" "}
            <span
              className={
                executionData.stop.status === "completed"
                  ? "text-green-600 font-medium"
                  : "text-yellow-600 font-medium"
              }
            >
              {executionData.stop.status}
            </span>
          </p>
        </div>
      )}
    </div>
  );
}

// ─── Outcome Comparison Section ──────────────────────────────────────────────

interface OutcomeComparisonProps {
  planId: string;
}

function OutcomeComparison({ planId }: OutcomeComparisonProps) {
  const [outcome, setOutcome] = useState<PlanOutcome | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError("");
      try {
        const result = await getPlanOutcomes(planId, TENANT_ID);
        if (!cancelled) setOutcome(result.data);
      } catch (err) {
        if (!cancelled)
          setError(
            err instanceof Error ? err.message : "Failed to load outcomes",
          );
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [planId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="w-5 h-5 text-gray-400 animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="border border-gray-100 rounded-lg p-4">
        <p className="text-sm text-red-600">{error}</p>
      </div>
    );
  }

  if (!outcome) return null;

  const varianceColor = (value: number) =>
    Math.abs(value) <= VARIANCE_THRESHOLD ? "text-green-600" : "text-red-600";

  return (
    <div className="border border-green-100 rounded-lg p-4 bg-green-50/30">
      <div className="flex items-center gap-2 mb-3">
        <Check className="w-4 h-4 text-green-600" />
        <h4 className="text-sm font-medium text-[#232323]">
          Plan vs Actual Outcomes
        </h4>
      </div>

      {/* Aggregate metrics */}
      <div className="grid grid-cols-3 gap-4 mb-4">
        <div className="bg-white rounded-lg p-3 border border-gray-100">
          <p className="text-xs text-gray-500">Qty Variance</p>
          <p
            className={`text-lg font-semibold ${varianceColor(outcome.aggregate_quantity_variance_pct)}`}
          >
            {outcome.aggregate_quantity_variance_pct.toFixed(1)}%
          </p>
        </div>
        <div className="bg-white rounded-lg p-3 border border-gray-100">
          <p className="text-xs text-gray-500">Time Variance</p>
          <p
            className={`text-lg font-semibold ${Math.abs(outcome.aggregate_time_variance_minutes) <= 15 ? "text-green-600" : "text-red-600"}`}
          >
            {outcome.aggregate_time_variance_minutes.toFixed(0)} min
          </p>
        </div>
        <div className="bg-white rounded-lg p-3 border border-gray-100">
          <p className="text-xs text-gray-500">Missed Stops</p>
          <p
            className={`text-lg font-semibold ${outcome.missed_stops_count === 0 ? "text-green-600" : "text-red-600"}`}
          >
            {outcome.missed_stops_count}
          </p>
        </div>
      </div>

      {/* Per-stop comparison table */}
      {outcome.stop_variances && outcome.stop_variances.length > 0 && (
        <div className="overflow-x-auto">
          <table
            className="w-full text-xs"
            aria-label="Stop variance comparison"
          >
            <thead className="bg-white">
              <tr>
                <th className="px-3 py-2 text-left text-gray-600 font-medium">
                  Station
                </th>
                <th className="px-3 py-2 text-right text-gray-600 font-medium">
                  Qty Variance %
                </th>
                <th className="px-3 py-2 text-right text-gray-600 font-medium">
                  Time Variance (min)
                </th>
                <th className="px-3 py-2 text-left text-gray-600 font-medium">
                  Status
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {outcome.stop_variances.map((sv: StopVariance) => (
                <tr
                  key={`${sv.station_id}-${sv.sequence}`}
                  className="hover:bg-white"
                >
                  <td className="px-3 py-2 text-gray-700">
                    {sv.station_id} (#{sv.sequence})
                  </td>
                  <td
                    className={`px-3 py-2 text-right font-medium ${varianceColor(sv.quantity_variance_pct)}`}
                  >
                    {sv.quantity_variance_pct.toFixed(1)}%
                  </td>
                  <td
                    className={`px-3 py-2 text-right font-medium ${Math.abs(sv.time_variance_minutes) <= 15 ? "text-green-600" : "text-red-600"}`}
                  >
                    {sv.time_variance_minutes.toFixed(0)}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={`inline-flex items-center text-[10px] px-1.5 py-0.5 rounded font-medium ${
                        sv.status === "completed"
                          ? "bg-green-50 text-green-600"
                          : "bg-yellow-50 text-yellow-600"
                      }`}
                    >
                      {sv.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Cost Breakdown Section ──────────────────────────────────────────────────

interface CostBreakdownSectionProps {
  planId: string;
  planStatus: string;
}

function CostBreakdownSection({
  planId,
  planStatus,
}: CostBreakdownSectionProps) {
  const [costs, setCosts] = useState<{
    estimated: CostBreakdown;
    actual?: CostBreakdown;
    cost_variance_pct?: number;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError("");
      try {
        const result = await getPlanCosts(planId, TENANT_ID);
        if (!cancelled) setCosts(result.data);
      } catch (err) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "Failed to load costs");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [planId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="w-5 h-5 text-gray-400 animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="border border-gray-100 rounded-lg p-4">
        <p className="text-sm text-gray-500">{error}</p>
      </div>
    );
  }

  if (!costs) return null;

  const { estimated, actual, cost_variance_pct } = costs;

  return (
    <div className="border border-gray-100 rounded-lg p-4">
      <div className="flex items-center gap-2 mb-3">
        <DollarSign className="w-4 h-4 text-gray-500" />
        <h4 className="text-sm font-medium text-[#232323]">Cost Analysis</h4>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs" aria-label="Cost breakdown">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-3 py-2 text-left text-gray-600 font-medium">
                Category
              </th>
              <th className="px-3 py-2 text-right text-gray-600 font-medium">
                Estimated
              </th>
              {planStatus === "completed" && actual && (
                <>
                  <th className="px-3 py-2 text-right text-gray-600 font-medium">
                    Actual
                  </th>
                  <th className="px-3 py-2 text-right text-gray-600 font-medium">
                    Variance
                  </th>
                </>
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            <tr>
              <td className="px-3 py-2 text-gray-700">Fuel Cost</td>
              <td className="px-3 py-2 text-right text-gray-700">
                {estimated.currency ?? "$"}
                {estimated.fuel_cost.toFixed(2)}
              </td>
              {planStatus === "completed" && actual && (
                <>
                  <td className="px-3 py-2 text-right text-gray-700">
                    {actual.currency ?? "$"}
                    {actual.fuel_cost.toFixed(2)}
                  </td>
                  <td
                    className={`px-3 py-2 text-right font-medium ${
                      Math.abs(
                        ((actual.fuel_cost - estimated.fuel_cost) /
                          estimated.fuel_cost) *
                          100,
                      ) <= VARIANCE_THRESHOLD
                        ? "text-green-600"
                        : "text-red-600"
                    }`}
                  >
                    {estimated.fuel_cost > 0
                      ? `${(((actual.fuel_cost - estimated.fuel_cost) / estimated.fuel_cost) * 100).toFixed(1)}%`
                      : "—"}
                  </td>
                </>
              )}
            </tr>
            <tr>
              <td className="px-3 py-2 text-gray-700">Driver Cost</td>
              <td className="px-3 py-2 text-right text-gray-700">
                {estimated.currency ?? "$"}
                {estimated.driver_cost.toFixed(2)}
              </td>
              {planStatus === "completed" && actual && (
                <>
                  <td className="px-3 py-2 text-right text-gray-700">
                    {actual.currency ?? "$"}
                    {actual.driver_cost.toFixed(2)}
                  </td>
                  <td
                    className={`px-3 py-2 text-right font-medium ${
                      Math.abs(
                        ((actual.driver_cost - estimated.driver_cost) /
                          estimated.driver_cost) *
                          100,
                      ) <= VARIANCE_THRESHOLD
                        ? "text-green-600"
                        : "text-red-600"
                    }`}
                  >
                    {estimated.driver_cost > 0
                      ? `${(((actual.driver_cost - estimated.driver_cost) / estimated.driver_cost) * 100).toFixed(1)}%`
                      : "—"}
                  </td>
                </>
              )}
            </tr>
            <tr className="font-medium">
              <td className="px-3 py-2 text-[#232323]">Total</td>
              <td className="px-3 py-2 text-right text-[#232323]">
                {estimated.currency ?? "$"}
                {(
                  estimated.total_estimated_cost ??
                  estimated.fuel_cost + estimated.driver_cost
                ).toFixed(2)}
              </td>
              {planStatus === "completed" && actual && (
                <>
                  <td className="px-3 py-2 text-right text-[#232323]">
                    {actual.currency ?? "$"}
                    {(
                      actual.total_actual_cost ??
                      actual.fuel_cost + actual.driver_cost
                    ).toFixed(2)}
                  </td>
                  <td
                    className={`px-3 py-2 text-right ${
                      cost_variance_pct != null &&
                      Math.abs(cost_variance_pct) <= VARIANCE_THRESHOLD
                        ? "text-green-600"
                        : "text-red-600"
                    }`}
                  >
                    {cost_variance_pct != null
                      ? `${cost_variance_pct.toFixed(1)}%`
                      : "—"}
                  </td>
                </>
              )}
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── Replan Modal ────────────────────────────────────────────────────────────

interface ReplanFormProps {
  planId: string;
  onClose: () => void;
  onSuccess: (response: ReplanResponse) => void;
}

function ReplanForm({ planId, onClose, onSuccess }: ReplanFormProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState<ReplanRequest>({
    disruption_type: "",
    description: "",
    entity_id: "",
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.disruption_type || !form.description || !form.entity_id) {
      setError("All fields are required.");
      return;
    }
    setError("");
    setSubmitting(true);
    try {
      const res = await replan(planId, form, TENANT_ID);
      onSuccess(res);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to submit replan");
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-[#232323]">Replan</h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close replan form"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Disruption Type
            </label>
            <select
              value={form.disruption_type}
              onChange={(e) =>
                setForm({ ...form, disruption_type: e.target.value })
              }
              className={inputClass}
              required
            >
              <option value="">Select type...</option>
              <option value="truck_breakdown">Truck Breakdown</option>
              <option value="station_closure">Station Closure</option>
              <option value="demand_spike">Demand Spike</option>
              <option value="road_closure">Road Closure</option>
              <option value="other">Other</option>
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Description
            </label>
            <textarea
              value={form.description}
              onChange={(e) =>
                setForm({ ...form, description: e.target.value })
              }
              placeholder="Describe the disruption..."
              rows={3}
              className={inputClass + " resize-none"}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Entity ID
            </label>
            <input
              type="text"
              value={form.entity_id}
              onChange={(e) => setForm({ ...form, entity_id: e.target.value })}
              placeholder="e.g. TRK-001 or STN-005"
              className={inputClass}
              required
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              {submitting ? "Submitting..." : "Submit Replan"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Emergency Stop Modal (Task 4.9 / Req 2.4.1) ─────────────────────────────

const EMERGENCY_STOP_REASON_LABELS: Record<string, string> = {
  capacity_insufficient:
    "No compartment capacity — requested gallons exceed remaining.",
  sla_breach: "Insertion would breach an existing stop's SLA window.",
  truck_off_duty: "Truck is already off-duty or out of HOS window.",
};

interface EmergencyStopModalProps {
  routeId: string;
  onClose: () => void;
  onSuccess: (response: EmergencyStopResponse) => void;
}

function EmergencyStopModal({
  routeId,
  onClose,
  onSuccess,
}: EmergencyStopModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [destinationType, setDestinationType] = useState<
    "station" | "customer_tank"
  >("station");
  const [form, setForm] = useState<EmergencyStopRequest>({
    fuel_grade: "",
    requested_gallons: 0,
    priority_reason: "",
  });

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    const destinationValue =
      destinationType === "station" ? form.station_id : form.customer_tank_id;
    if (!destinationValue) {
      setError("Destination id is required.");
      return;
    }
    if (!form.fuel_grade.trim()) {
      setError("Fuel grade is required.");
      return;
    }
    if (!form.requested_gallons || form.requested_gallons <= 0) {
      setError("Requested gallons must be greater than zero.");
      return;
    }
    if (!form.priority_reason.trim()) {
      setError("Priority reason is required.");
      return;
    }

    const payload: EmergencyStopRequest = {
      fuel_grade: form.fuel_grade.trim(),
      requested_gallons: Number(form.requested_gallons),
      priority_reason: form.priority_reason.trim(),
    };
    if (destinationType === "station") {
      payload.station_id = form.station_id;
    } else {
      payload.customer_tank_id = form.customer_tank_id;
    }
    if (form.SLA_by?.trim()) {
      payload.SLA_by = form.SLA_by.trim();
    }

    setSubmitting(true);
    try {
      const res = await insertEmergencyStop(routeId, payload);
      onSuccess(res);
      onClose();
    } catch (err) {
      // The backend surfaces structured reason codes on HTTP 409
      // (``capacity_insufficient`` / ``sla_breach`` / ``truck_off_duty``).
      // ``fuelRequest`` packs those into ``ApiError.message`` — we surface
      // the human-readable label when we recognize it.
      const raw =
        err instanceof Error ? err.message : "Failed to insert emergency stop";
      const friendly = EMERGENCY_STOP_REASON_LABELS[raw] ?? raw;
      setError(friendly);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <div className="flex items-center gap-2">
            <Siren className="w-4 h-4 text-red-600" />
            <h2 className="text-lg font-semibold text-[#232323]">
              Emergency Stop
            </h2>
          </div>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close emergency stop form"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Destination Type
            </label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setDestinationType("station")}
                className={`flex-1 px-3 py-2 text-xs font-medium rounded-lg border transition-colors ${
                  destinationType === "station"
                    ? "bg-gray-900 text-white border-gray-900"
                    : "bg-white text-gray-600 border-gray-200 hover:bg-gray-50"
                }`}
              >
                Station
              </button>
              <button
                type="button"
                onClick={() => setDestinationType("customer_tank")}
                className={`flex-1 px-3 py-2 text-xs font-medium rounded-lg border transition-colors ${
                  destinationType === "customer_tank"
                    ? "bg-gray-900 text-white border-gray-900"
                    : "bg-white text-gray-600 border-gray-200 hover:bg-gray-50"
                }`}
              >
                Customer Tank
              </button>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              {destinationType === "station"
                ? "Station ID"
                : "Customer Tank ID"}
            </label>
            <input
              type="text"
              value={
                destinationType === "station"
                  ? (form.station_id ?? "")
                  : (form.customer_tank_id ?? "")
              }
              onChange={(e) =>
                setForm((prev) => ({
                  ...prev,
                  station_id:
                    destinationType === "station"
                      ? e.target.value
                      : prev.station_id,
                  customer_tank_id:
                    destinationType === "customer_tank"
                      ? e.target.value
                      : prev.customer_tank_id,
                }))
              }
              placeholder={
                destinationType === "station" ? "e.g. STN-042" : "e.g. CT-0193"
              }
              className={inputClass}
              required
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Fuel Grade
              </label>
              <input
                type="text"
                value={form.fuel_grade}
                onChange={(e) =>
                  setForm({ ...form, fuel_grade: e.target.value })
                }
                placeholder="DIESEL_2 / PROPANE / ..."
                className={inputClass}
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Requested Gallons
              </label>
              <input
                type="number"
                min="0.01"
                step="0.1"
                value={form.requested_gallons || ""}
                onChange={(e) =>
                  setForm({
                    ...form,
                    requested_gallons: parseFloat(e.target.value) || 0,
                  })
                }
                placeholder="200"
                className={inputClass}
                required
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Priority Reason
            </label>
            <textarea
              value={form.priority_reason}
              onChange={(e) =>
                setForm({ ...form, priority_reason: e.target.value })
              }
              placeholder="Why is this insertion urgent?"
              rows={2}
              className={`${inputClass} resize-none`}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              SLA By (optional, ISO-8601)
            </label>
            <input
              type="text"
              value={form.SLA_by ?? ""}
              onChange={(e) => setForm({ ...form, SLA_by: e.target.value })}
              placeholder="2024-05-01T18:00:00Z"
              className={inputClass}
            />
            <p className="mt-1 text-[11px] text-gray-500">
              Backend may respond with 409 + reason codes
              (capacity_insufficient, sla_breach, truck_off_duty).
            </p>
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 text-sm text-white bg-red-600 hover:bg-red-700 rounded-lg disabled:opacity-50"
            >
              {submitting ? "Inserting..." : "Insert Emergency Stop"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Replan Diff Panel (Task 4.10 / Req 2.5.3) ───────────────────────────────

interface ReplanDiffSectionProps<T> {
  title: string;
  items: T[];
  emptyLabel?: string;
  render: (item: T, index: number) => React.ReactNode;
  initiallyOpen?: boolean;
}

function ReplanDiffSection<T>({
  title,
  items,
  emptyLabel,
  render,
  initiallyOpen = false,
}: ReplanDiffSectionProps<T>) {
  const [open, setOpen] = useState(initiallyOpen && items.length > 0);
  const isEmpty = items.length === 0;

  return (
    <div className="border border-gray-100 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => !isEmpty && setOpen((o) => !o)}
        disabled={isEmpty}
        className={`w-full flex items-center justify-between px-4 py-2.5 text-sm font-medium transition-colors ${
          isEmpty
            ? "text-gray-400 cursor-default"
            : "text-[#232323] hover:bg-gray-50"
        }`}
        aria-expanded={open}
      >
        <span className="flex items-center gap-2">
          {title}
          <span
            className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
              isEmpty ? "bg-gray-50 text-gray-400" : "bg-blue-50 text-blue-700"
            }`}
          >
            {items.length}
          </span>
        </span>
        {!isEmpty &&
          (open ? (
            <ChevronUp className="w-4 h-4 text-gray-400" />
          ) : (
            <ChevronDown className="w-4 h-4 text-gray-400" />
          ))}
      </button>
      {open && !isEmpty && (
        <div className="border-t border-gray-100 divide-y divide-gray-100">
          {items.map((item, i) => (
            <div key={i} className="px-4 py-2 text-xs text-gray-700">
              {render(item, i)}
            </div>
          ))}
        </div>
      )}
      {isEmpty && emptyLabel && (
        <div className="px-4 py-2 text-[11px] text-gray-400">{emptyLabel}</div>
      )}
    </div>
  );
}

function ReplanDiffBody({ diff }: { diff: ReplanDiff }) {
  return (
    <div className="space-y-2">
      <ReplanDiffSection
        title="Added stops"
        items={diff.added_stops}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-[#232323]">{s.stop_id}</span>
            <span className="text-gray-500">
              index {s.index}
              {s.gallons != null ? ` · ${s.gallons.toFixed(0)} gal` : ""}
              {s.product_code ? ` · ${s.product_code}` : ""}
              {s.eta ? ` · ETA ${new Date(s.eta).toLocaleString()}` : ""}
            </span>
          </div>
        )}
        initiallyOpen
      />
      <ReplanDiffSection
        title="Removed stops"
        items={diff.removed_stops}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-[#232323]">{s.stop_id}</span>
            <span className="text-gray-500">
              was index {s.index}
              {s.gallons != null ? ` · ${s.gallons.toFixed(0)} gal` : ""}
            </span>
          </div>
        )}
        initiallyOpen
      />
      <ReplanDiffSection
        title="Reordered stops"
        items={diff.reordered_stops}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-[#232323]">{s.stop_id}</span>
            <span className="text-gray-500">
              {s.before_index} → {s.after_index}
            </span>
          </div>
        )}
        initiallyOpen
      />
      <ReplanDiffSection
        title="Reassigned stops"
        items={diff.reassigned_stops}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-[#232323]">{s.stop_id}</span>
            <span className="text-gray-500">
              {s.from_truck_id} → {s.to_truck_id}
            </span>
          </div>
        )}
      />
      <ReplanDiffSection
        title="Quantity changes"
        items={diff.quantity_changes}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-[#232323]">{s.stop_id}</span>
            <span className="text-gray-500">
              {s.before_gallons.toFixed(0)} → {s.after_gallons.toFixed(0)} gal
              {s.product_code ? ` · ${s.product_code}` : ""}
            </span>
          </div>
        )}
      />
      <ReplanDiffSection
        title="ETA shifts"
        items={diff.eta_shifts}
        render={(s) => (
          <div className="flex items-center justify-between">
            <span className="font-medium text-[#232323]">{s.stop_id}</span>
            <span className="text-gray-500">
              {new Date(s.before_eta).toLocaleString()} →{" "}
              {new Date(s.after_eta).toLocaleString()} (
              {s.shift_minutes >= 0 ? "+" : ""}
              {s.shift_minutes.toFixed(0)} min)
            </span>
          </div>
        )}
      />
    </div>
  );
}

interface ReplanDiffPanelProps {
  /**
   * Optional seed event id (from a URL/query param or a recent replan
   * response). When supplied the panel auto-loads the diff on mount.
   */
  seedEventId?: string | null;
}

/**
 * Replan Diff Panel (Req 2.5.3, Task 4.10).
 *
 * Renders the structured ``added / removed / reordered / reassigned``
 * diff plus ``quantity_changes`` and ``eta_shifts`` for a given replan
 * event. Accepts a manual event_id input since the MVP pipeline does
 * not yet expose a list endpoint for recent replan events; task 11.10
 * will reconcile this with the event-list API once it exists.
 */
function ReplanDiffPanel({ seedEventId }: ReplanDiffPanelProps) {
  const [inputValue, setInputValue] = useState(seedEventId ?? "");
  const [diff, setDiff] = useState<ReplanDiffResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (id: string) => {
    if (!id.trim()) {
      setDiff(null);
      setError("");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const res = await getReplanDiff(id.trim());
      setDiff(res);
    } catch (err) {
      setDiff(null);
      setError(err instanceof Error ? err.message : "Failed to load diff");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (seedEventId) {
      setInputValue(seedEventId);
      load(seedEventId);
    }
  }, [seedEventId, load]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    load(inputValue);
  };

  return (
    <div className="border border-gray-100 rounded-lg p-4 space-y-3">
      <div className="flex items-center gap-2">
        <GitBranch className="w-4 h-4 text-gray-500" />
        <h4 className="text-sm font-medium text-[#232323]">Replan Diff</h4>
        {diff?.replan_type && (
          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-gray-100 text-gray-600">
            {diff.replan_type}
          </span>
        )}
        {diff?.status && (
          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-blue-50 text-blue-700">
            {diff.status}
          </span>
        )}
      </div>

      <form onSubmit={handleSubmit} className="flex items-center gap-2">
        <input
          type="text"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          placeholder="Enter replan event_id..."
          className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
          aria-label="Replan event ID"
        />
        <button
          type="submit"
          disabled={loading || !inputValue.trim()}
          className="px-3 py-1.5 text-sm font-medium text-white rounded-lg disabled:opacity-50"
          style={{ backgroundColor: "#232323" }}
        >
          {loading ? "Loading..." : "Load Diff"}
        </button>
      </form>

      {error && (
        <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
          {error}
        </p>
      )}

      {!diff && !loading && !error && (
        <p className="text-xs text-gray-400">
          Enter a replan event_id to view the structured diff (added / removed /
          reordered / reassigned stops plus quantity and ETA changes).
        </p>
      )}

      {diff && (
        <>
          <div className="text-[11px] text-gray-500 flex flex-wrap gap-x-4 gap-y-1">
            <span>event_id: {diff.event_id}</span>
            <span>original: {diff.diff.original_route_id}</span>
            <span>patched: {diff.diff.patched_route_id}</span>
            <span>
              generated: {new Date(diff.diff.generated_at).toLocaleString()}
            </span>
          </div>
          <ReplanDiffBody diff={diff.diff} />
        </>
      )}
    </div>
  );
}

// ─── Plan Detail View ────────────────────────────────────────────────────────

interface PlanDetailViewProps {
  planId: string;
  planStatus?: string;
  onBack: () => void;
  onReplan: () => void;
  onApprove: (planId: string) => void;
  onReject: (planId: string) => void;
  executionData?: ExecutionUpdateData | null;
  /** Req 2.4.1 — open the emergency-stop modal for a given route id. */
  onEmergencyStop?: (routeId: string) => void;
  /**
   * Req 2.5.3 — event_id to auto-load into the Replan Diff panel.
   * Typically the ``event_id`` returned by the most recent emergency-stop
   * response so the dispatcher can immediately inspect the diff.
   */
  replanDiffEventId?: string | null;
}

function PlanDetailView({
  planId,
  planStatus,
  onBack,
  onReplan,
  onApprove,
  onReject,
  executionData,
  onEmergencyStop,
  replanDiffEventId,
}: PlanDetailViewProps) {
  const [plan, setPlan] = useState<PlanDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError("");
      try {
        const result = await getPlan(planId, TENANT_ID);
        if (!cancelled) setPlan(result);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load plan");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [planId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-6 h-6 text-gray-400 animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="py-8">
        <button
          onClick={onBack}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700 mb-4"
        >
          <ChevronLeft className="w-4 h-4" /> Back to plans
        </button>
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {error}
        </p>
      </div>
    );
  }

  if (!plan) return null;

  const currentStatus = planStatus || (plan as any).status || "draft";

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <button
          onClick={onBack}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700"
        >
          <ChevronLeft className="w-4 h-4" /> Back to plans
        </button>
        <div className="flex items-center gap-2">
          {APPROVABLE_STATUSES.includes(currentStatus) && (
            <>
              <button
                onClick={() => onApprove(planId)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-green-600 hover:bg-green-700 rounded-lg transition-colors"
              >
                <Check className="w-3 h-3" />
                Approve
              </button>
              <button
                onClick={() => onReject(planId)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-red-600 hover:bg-red-700 rounded-lg transition-colors"
              >
                <X className="w-3 h-3" />
                Reject
              </button>
            </>
          )}
          <button
            onClick={onReplan}
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white rounded-lg"
            style={{ backgroundColor: "#232323" }}
          >
            <RefreshCw className="w-4 h-4" />
            Replan
          </button>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <h3 className="text-sm font-semibold text-[#232323]">
          Plan: {plan.plan_id}
        </h3>
        <StatusBadge status={currentStatus} />
      </div>

      {/* Execution Progress (dispatched plans) */}
      {currentStatus === "dispatched" && (
        <ExecutionProgress planId={planId} executionData={executionData} />
      )}

      {/* Outcome Comparison (completed plans) */}
      {currentStatus === "completed" && <OutcomeComparison planId={planId} />}

      {/* Cost Breakdown (all plans) */}
      <CostBreakdownSection planId={planId} planStatus={currentStatus} />

      {/* Loading Plan */}
      {plan.loading_plan ? (
        <div className="border border-gray-100 rounded-lg p-4">
          <div className="flex items-center gap-2 mb-3">
            <Truck className="w-4 h-4 text-gray-500" />
            <h4 className="text-sm font-medium text-[#232323]">Loading Plan</h4>
            <span
              className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                plan.loading_plan.status === "completed"
                  ? "bg-green-50 text-green-600"
                  : "bg-yellow-50 text-yellow-600"
              }`}
            >
              {plan.loading_plan.status}
            </span>
          </div>
          <div className="grid grid-cols-3 gap-4 text-xs text-gray-600 mb-3">
            <div>
              <span className="text-gray-400">Truck:</span>{" "}
              {plan.loading_plan.truck_id}
            </div>
            <div>
              <span className="text-gray-400">Utilization:</span>{" "}
              {plan.loading_plan.total_utilization_pct.toFixed(1)}%
            </div>
            <div>
              <span className="text-gray-400">Weight:</span>{" "}
              {plan.loading_plan.total_weight_kg.toFixed(0)} kg
            </div>
          </div>
          {plan.loading_plan.assignments.length > 0 && (
            <div className="overflow-x-auto">
              <table
                className="w-full text-xs"
                aria-label="Compartment assignments"
              >
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-3 py-2 text-left text-gray-600 font-medium">
                      Compartment
                    </th>
                    <th className="px-3 py-2 text-left text-gray-600 font-medium">
                      Station
                    </th>
                    <th className="px-3 py-2 text-left text-gray-600 font-medium">
                      Fuel Grade
                    </th>
                    <th className="px-3 py-2 text-right text-gray-600 font-medium">
                      Quantity (L)
                    </th>
                    <th className="px-3 py-2 text-right text-gray-600 font-medium">
                      Capacity (L)
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {plan.loading_plan.assignments.map(
                    (a: CompartmentAssignment, i: number) => (
                      <tr key={i} className="hover:bg-gray-50">
                        <td className="px-3 py-2 text-gray-700">
                          {a.compartment_id}
                        </td>
                        <td className="px-3 py-2 text-gray-700">
                          {a.station_id}
                        </td>
                        <td className="px-3 py-2 text-gray-700">
                          {a.fuel_grade}
                        </td>
                        <td className="px-3 py-2 text-right text-gray-700">
                          {a.quantity_liters.toLocaleString()}
                        </td>
                        <td className="px-3 py-2 text-right text-gray-700">
                          {a.compartment_capacity_liters.toLocaleString()}
                        </td>
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ) : (
        <div className="border border-gray-100 rounded-lg p-4 text-sm text-gray-400 text-center">
          No loading plan available
        </div>
      )}

      {/* Route Plan */}
      {plan.route_plan ? (
        <div className="border border-gray-100 rounded-lg p-4">
          <div className="flex items-center gap-2 mb-3">
            <Route className="w-4 h-4 text-gray-500" />
            <h4 className="text-sm font-medium text-[#232323]">Route Plan</h4>
          </div>
          {plan.route_plan.routes.length > 0 ? (
            <div className="space-y-3">
              {plan.route_plan.routes.map(
                (route: RouteAssignment, i: number) => (
                  <div
                    key={route.route_id || i}
                    className="border border-gray-50 rounded-lg p-3"
                  >
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-medium text-[#232323]">
                        {route.truck_id}
                      </span>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-gray-500">
                          {route.distance_km.toFixed(1)} km
                        </span>
                        {route.route_id && onEmergencyStop && (
                          <button
                            type="button"
                            onClick={() => onEmergencyStop(route.route_id)}
                            className="flex items-center gap-1 px-2 py-1 text-[11px] font-medium text-white bg-red-600 hover:bg-red-700 rounded"
                            aria-label={`Insert emergency stop into route ${route.route_id}`}
                          >
                            <Siren className="w-3 h-3" />
                            Emergency Stop
                          </button>
                        )}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {route.stops.map((stop, j) => (
                        <span
                          key={j}
                          className="inline-flex items-center gap-1 text-[10px] px-2 py-1 bg-gray-50 rounded text-gray-600"
                        >
                          <span className="font-medium">{stop.sequence}.</span>
                          {stop.station_id}
                        </span>
                      ))}
                    </div>
                  </div>
                ),
              )}
            </div>
          ) : (
            <p className="text-xs text-gray-400">No routes assigned</p>
          )}
        </div>
      ) : (
        <div className="border border-gray-100 rounded-lg p-4 text-sm text-gray-400 text-center">
          No route plan available
        </div>
      )}

      {/* Replan Diff (Task 4.10, Req 2.5.3) */}
      <ReplanDiffPanel seedEventId={replanDiffEventId} />

      {/* Excluded Trucks — Equipment Unavailability */}
      {(plan as any).excluded_trucks &&
        (plan as any).excluded_trucks.length > 0 && (
          <div className="border border-orange-100 rounded-lg p-4 bg-orange-50/50">
            <div className="flex items-center gap-2 mb-3">
              <AlertTriangle className="w-4 h-4 text-orange-600" />
              <h4 className="text-sm font-medium text-orange-800">
                Excluded Trucks — Equipment Unavailable
              </h4>
            </div>
            <div className="space-y-2">
              {(
                (plan as any).excluded_trucks as Array<{
                  truck_id: string;
                  missing_equipment?: string[];
                  reason?: string;
                }>
              ).map((excluded, i) => (
                <div
                  key={excluded.truck_id || i}
                  className="flex items-start gap-2 text-xs bg-white rounded-lg px-3 py-2 border border-orange-100"
                >
                  <Truck className="w-3.5 h-3.5 text-orange-500 mt-0.5 flex-shrink-0" />
                  <div>
                    <span className="font-medium text-[#232323]">
                      {excluded.truck_id}
                    </span>
                    {excluded.missing_equipment &&
                      excluded.missing_equipment.length > 0 && (
                        <p className="text-gray-600 mt-0.5">
                          Missing: {excluded.missing_equipment.join(", ")}
                        </p>
                      )}
                    {excluded.reason && (
                      <p className="text-gray-600 mt-0.5">{excluded.reason}</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
    </div>
  );
}

// ─── Pagination Controls ─────────────────────────────────────────────────────

interface PaginationControlsProps {
  pagination: PaginationMeta;
  onPageChange: (page: number) => void;
}

function PaginationControls({
  pagination,
  onPageChange,
}: PaginationControlsProps) {
  if (pagination.total_pages <= 1) return null;

  return (
    <div className="flex items-center justify-between px-4 py-3 border-t border-gray-100">
      <span className="text-xs text-gray-500">
        Page {pagination.page} of {pagination.total_pages} ({pagination.total}{" "}
        total)
      </span>
      <div className="flex items-center gap-2">
        <button
          onClick={() => onPageChange(pagination.page - 1)}
          disabled={pagination.page <= 1}
          className="p-1.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed rounded"
          aria-label="Previous page"
        >
          <ChevronLeft className="w-4 h-4" />
        </button>
        <button
          onClick={() => onPageChange(pagination.page + 1)}
          disabled={pagination.page >= pagination.total_pages}
          className="p-1.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed rounded"
          aria-label="Next page"
        >
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}

// ─── Plans Tab ───────────────────────────────────────────────────────────────

function PlansTab() {
  // Plan list state (fetched from backend)
  const [planList, setPlanList] = useState<PlanListItem[]>([]);
  const [planPagination, setPlanPagination] = useState<PaginationMeta | null>(
    null,
  );
  const [planPage, setPlanPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [listLoading, setListLoading] = useState(true);

  // UI state
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(null);
  const [selectedPlanStatus, setSelectedPlanStatus] = useState<
    string | undefined
  >(undefined);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [showReplan, setShowReplan] = useState(false);
  const [showRejectDialog, setShowRejectDialog] = useState<string | null>(null);
  const [rejectLoading, setRejectLoading] = useState(false);
  const [approveLoading, setApproveLoading] = useState<string | null>(null);
  const [showCostConfig, setShowCostConfig] = useState(false);

  // Emergency-stop modal state (Task 4.9, Req 2.4.1). When an emergency
  // stop is successfully inserted we stash the resulting ``event_id`` so
  // the Replan Diff panel in PlanDetailView auto-loads the diff.
  const [emergencyRouteId, setEmergencyRouteId] = useState<string | null>(null);
  const [latestReplanEventId, setLatestReplanEventId] = useState<string | null>(
    null,
  );

  // Toast notifications
  const { toasts, addToast, dismissToast } = useToasts();

  // WebSocket for real-time execution updates
  const { lastUpdate: executionUpdate } = usePlanExecutionSocket(TENANT_ID, {
    autoConnect: true,
  });

  // Fetch plan list from backend
  const loadPlanList = useCallback(async () => {
    setListLoading(true);
    try {
      const result = await listPlans(
        TENANT_ID,
        planPage,
        PAGE_SIZE,
        statusFilter || undefined,
      );
      setPlanList(result.data ?? []);
      setPlanPagination(result.pagination ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load plans");
    } finally {
      setListLoading(false);
    }
  }, [planPage, statusFilter]);

  // Load plan list on mount and when page/filter changes
  useEffect(() => {
    loadPlanList();
  }, [loadPlanList]);

  // Refresh plan list helper
  const refreshPlanList = useCallback(() => {
    loadPlanList();
  }, [loadPlanList]);

  // Generate plan
  const handleGenerate = useCallback(async () => {
    setGenerating(true);
    setError("");
    try {
      await generatePlan(TENANT_ID);
      addToast("Plan generated successfully", "success");
      refreshPlanList();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to generate plan");
      addToast("Failed to generate plan", "error");
    } finally {
      setGenerating(false);
    }
  }, [addToast, refreshPlanList]);

  // Approve plan
  const handleApprove = useCallback(
    async (planId: string) => {
      setApproveLoading(planId);
      try {
        await approvePlan(planId, TENANT_ID, DISPATCHER_ID);
        addToast(`Plan ${planId} approved and dispatched`, "success");
        refreshPlanList();
        if (selectedPlanId === planId) {
          setSelectedPlanStatus("dispatched");
        }
      } catch (err) {
        addToast(
          err instanceof Error ? err.message : "Failed to approve plan",
          "error",
        );
      } finally {
        setApproveLoading(null);
      }
    },
    [addToast, refreshPlanList, selectedPlanId],
  );

  // Reject plan
  const handleReject = useCallback(
    async (reason: string) => {
      if (!showRejectDialog) return;
      setRejectLoading(true);
      try {
        await rejectPlan(
          showRejectDialog,
          TENANT_ID,
          DISPATCHER_ID,
          reason || undefined,
        );
        addToast(`Plan ${showRejectDialog} rejected`, "success");
        setShowRejectDialog(null);
        refreshPlanList();
        if (selectedPlanId === showRejectDialog) {
          setSelectedPlanStatus("rejected");
        }
      } catch (err) {
        addToast(
          err instanceof Error ? err.message : "Failed to reject plan",
          "error",
        );
      } finally {
        setRejectLoading(false);
      }
    },
    [showRejectDialog, addToast, refreshPlanList, selectedPlanId],
  );

  // Replan success
  const handleReplanSuccess = useCallback(
    (_response: ReplanResponse) => {
      addToast("Replan submitted successfully", "success");
      refreshPlanList();
    },
    [addToast, refreshPlanList],
  );

  // Emergency stop success — capture event_id so the detail view can
  // auto-load the resulting Replan Diff.
  const handleEmergencyStopSuccess = useCallback(
    (response: EmergencyStopResponse) => {
      setLatestReplanEventId(response.event_id);
      const riskSuffix =
        response.risk_level === "high" ? " — HIGH risk, awaiting approval" : "";
      addToast(
        `Emergency stop inserted at index ${response.insert_index}${riskSuffix}`,
        "success",
      );
      refreshPlanList();
    },
    [addToast, refreshPlanList],
  );

  // If a plan is selected, show detail view
  if (selectedPlanId) {
    return (
      <>
        <ToastContainer toasts={toasts} onDismiss={dismissToast} />
        <PlanDetailView
          planId={selectedPlanId}
          planStatus={selectedPlanStatus}
          onBack={() => {
            setSelectedPlanId(null);
            setSelectedPlanStatus(undefined);
          }}
          onReplan={() => setShowReplan(true)}
          onApprove={handleApprove}
          onReject={(id) => setShowRejectDialog(id)}
          executionData={
            executionUpdate?.plan_id === selectedPlanId ? executionUpdate : null
          }
          onEmergencyStop={(routeId) => setEmergencyRouteId(routeId)}
          replanDiffEventId={latestReplanEventId}
        />
        {emergencyRouteId && (
          <EmergencyStopModal
            routeId={emergencyRouteId}
            onClose={() => setEmergencyRouteId(null)}
            onSuccess={handleEmergencyStopSuccess}
          />
        )}
        {showReplan && (
          <ReplanForm
            planId={selectedPlanId}
            onClose={() => setShowReplan(false)}
            onSuccess={handleReplanSuccess}
          />
        )}
        {showRejectDialog && (
          <RejectDialog
            planId={showRejectDialog}
            onConfirm={handleReject}
            onCancel={() => setShowRejectDialog(null)}
            loading={rejectLoading}
          />
        )}
      </>
    );
  }

  return (
    <div className="space-y-4">
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />

      {/* Header with generate button and settings */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-[#232323]">
          Distribution Plans
        </h3>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowCostConfig(true)}
            className="flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 hover:text-gray-800 hover:bg-gray-100 rounded-lg transition-colors"
            aria-label="Cost configuration settings"
          >
            <Settings className="w-4 h-4" />
          </button>
          <button
            onClick={handleGenerate}
            disabled={generating}
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white rounded-lg disabled:opacity-50"
            style={{ backgroundColor: "#232323" }}
          >
            {generating ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Play className="w-4 h-4" />
            )}
            {generating ? "Generating..." : "Generate Plan"}
          </button>
        </div>
      </div>

      {/* Status filter */}
      <div className="flex items-center gap-3">
        <select
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value);
            setPlanPage(1);
          }}
          className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
        >
          <option value="">All statuses</option>
          <option value="draft">Draft</option>
          <option value="proposed">Proposed</option>
          <option value="dispatched">Dispatched</option>
          <option value="completed">Completed</option>
          <option value="rejected">Rejected</option>
        </select>
        <button
          onClick={refreshPlanList}
          className="p-1.5 text-gray-400 hover:text-gray-600 rounded"
          aria-label="Refresh plan list"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {error && (
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {error}
        </p>
      )}

      {/* Plan list */}
      {listLoading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 text-gray-400 animate-spin" />
        </div>
      ) : planList.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-gray-400">
          <Truck className="w-8 h-8 mb-2" />
          <p className="text-sm">No plans found</p>
          <p className="text-xs mt-1">
            Click &quot;Generate Plan&quot; to create a distribution plan
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {planList.map((p) => (
            <div
              key={p.plan_id}
              className="flex items-center justify-between border border-gray-100 rounded-lg p-4 hover:border-gray-200 transition-colors"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-[#232323] truncate">
                    {p.plan_id}
                  </p>
                  <StatusBadge status={p.status} />
                </div>
                <div className="flex items-center gap-4 mt-1 text-xs text-gray-500">
                  {p.truck_id && <span>Truck: {p.truck_id}</span>}
                  {p.created_at && (
                    <span>{new Date(p.created_at).toLocaleDateString()}</span>
                  )}
                  {p.total_utilization_pct != null && (
                    <span>{p.total_utilization_pct.toFixed(0)}% util</span>
                  )}
                  {/* Cost summary in list view */}
                  {p.status === "completed" && p.actual_cost != null && (
                    <span className="text-green-600 font-medium">
                      Actual: ${p.actual_cost.toFixed(0)}
                    </span>
                  )}
                  {p.status === "dispatched" && p.estimated_cost != null && (
                    <span className="text-blue-600 font-medium">
                      Est: ${p.estimated_cost.toFixed(0)}
                    </span>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2 ml-4">
                {/* Approve/Reject buttons for draft/proposed plans */}
                {APPROVABLE_STATUSES.includes(p.status) && (
                  <>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleApprove(p.plan_id);
                      }}
                      disabled={approveLoading === p.plan_id}
                      className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-white bg-green-600 hover:bg-green-700 rounded-lg transition-colors disabled:opacity-50"
                      aria-label={`Approve plan ${p.plan_id}`}
                    >
                      {approveLoading === p.plan_id ? (
                        <Loader2 className="w-3 h-3 animate-spin" />
                      ) : (
                        <Check className="w-3 h-3" />
                      )}
                      Approve
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setShowRejectDialog(p.plan_id);
                      }}
                      className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium text-white bg-red-600 hover:bg-red-700 rounded-lg transition-colors"
                      aria-label={`Reject plan ${p.plan_id}`}
                    >
                      <X className="w-3 h-3" />
                      Reject
                    </button>
                  </>
                )}
                <button
                  onClick={() => {
                    setSelectedPlanId(p.plan_id);
                    setSelectedPlanStatus(p.status);
                  }}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
                  aria-label={`View plan ${p.plan_id}`}
                >
                  <Eye className="w-3 h-3" />
                  View
                </button>
              </div>
            </div>
          ))}

          {/* Pagination */}
          {planPagination && (
            <PaginationControls
              pagination={planPagination}
              onPageChange={setPlanPage}
            />
          )}
        </div>
      )}

      {/* Reject dialog */}
      {showRejectDialog && (
        <RejectDialog
          planId={showRejectDialog}
          onConfirm={handleReject}
          onCancel={() => setShowRejectDialog(null)}
          loading={rejectLoading}
        />
      )}

      {/* Cost config panel */}
      {showCostConfig && (
        <CostConfigPanel
          onClose={() => setShowCostConfig(false)}
          onSave={refreshPlanList}
          addToast={addToast}
        />
      )}
    </div>
  );
}

// ─── Forecasts Tab ───────────────────────────────────────────────────────────

function ForecastsTab() {
  const [forecasts, setForecasts] = useState<Forecast[]>([]);
  const [pagination, setPagination] = useState<PaginationMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [stationFilter, setStationFilter] = useState("");
  const [gradeFilter, setGradeFilter] = useState("");
  const [page, setPage] = useState(1);

  const loadForecasts = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await getForecasts({
        tenant_id: TENANT_ID,
        station_id: stationFilter || undefined,
        fuel_grade: gradeFilter || undefined,
        page,
        size: PAGE_SIZE,
      });
      setForecasts((result as any).data ?? (result as any).items ?? []);
      setPagination((result as any).pagination ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load forecasts");
    } finally {
      setLoading(false);
    }
  }, [stationFilter, gradeFilter, page]);

  useEffect(() => {
    loadForecasts();
  }, [loadForecasts]);

  const handlePageChange = useCallback((newPage: number) => {
    setPage(newPage);
  }, []);

  const inputClass =
    "px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-[#232323]">Tank Forecasts</h3>

      {/* Filters */}
      <div className="flex items-center gap-3">
        <input
          type="text"
          value={stationFilter}
          onChange={(e) => {
            setStationFilter(e.target.value);
            setPage(1);
          }}
          placeholder="Filter by station ID..."
          className={inputClass}
        />
        <select
          value={gradeFilter}
          onChange={(e) => {
            setGradeFilter(e.target.value);
            setPage(1);
          }}
          className={inputClass}
        >
          <option value="">All fuel grades</option>
          <option value="AGO">AGO (Diesel)</option>
          <option value="PMS">PMS (Petrol)</option>
          <option value="ATK">ATK (Aviation)</option>
          <option value="LPG">LPG (Gas)</option>
        </select>
      </div>

      {error && (
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {error}
        </p>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 text-gray-400 animate-spin" />
        </div>
      ) : forecasts.length === 0 ? (
        <div className="text-center py-16 text-gray-400">
          <p className="text-sm">No forecasts found</p>
          <p className="text-xs mt-1">Try adjusting your filters</p>
        </div>
      ) : (
        <div className="overflow-x-auto border border-gray-100 rounded-lg">
          <table className="w-full" aria-label="Tank forecasts">
            <thead className="bg-gray-50 border-b border-gray-100">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Station
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Fuel Grade
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Runout P50 (hrs)
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Runout P90 (hrs)
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Risk 24h
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Timestamp
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {forecasts.map((f, i) => (
                <tr
                  key={`${f.station_id}-${f.fuel_grade}-${i}`}
                  className="hover:bg-gray-50"
                >
                  <td className="px-4 py-3 text-sm font-medium text-[#232323]">
                    {f.station_id}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-700">
                    {f.fuel_grade}
                  </td>
                  <td className="px-4 py-3 text-sm text-right text-gray-700">
                    {((f as any).hours_to_runout_p50 ?? 0).toFixed(1)}
                  </td>
                  <td className="px-4 py-3 text-sm text-right text-gray-700">
                    {((f as any).hours_to_runout_p90 ?? 0).toFixed(1)}
                  </td>
                  <td className="px-4 py-3 text-sm text-right text-gray-700">
                    {(((f as any).runout_risk_24h ?? 0) * 100).toFixed(0)}%
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">
                    {f.timestamp ? new Date(f.timestamp).toLocaleString() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {pagination && (
            <PaginationControls
              pagination={pagination}
              onPageChange={handlePageChange}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ─── Priorities Tab ──────────────────────────────────────────────────────────

const SAFE_TO_DELAY_BUCKET_LABELS: Record<SafeToDelayBucket, string> = {
  none: "None (< 1 day)",
  short: "Short (1–3 days)",
  medium: "Medium (4–7 days)",
  long: "Long (> 7 days)",
};

const SAFE_TO_DELAY_BADGE: Record<SafeToDelayBucket, string> = {
  none: "bg-red-50 text-red-700",
  short: "bg-orange-50 text-orange-700",
  medium: "bg-yellow-50 text-yellow-700",
  long: "bg-green-50 text-green-700",
};

interface PriorityCluster {
  cluster_id: string;
  cluster_size: number;
  centroid: { lat: number; lon: number } | null;
  members: PriorityEntry[];
}

/** Group priorities by ``cluster_id`` (falling back to "Unclustered"). */
function groupByCluster(entries: PriorityEntry[]): PriorityCluster[] {
  const groups = new Map<string, PriorityEntry[]>();
  for (const entry of entries) {
    const key = entry.cluster_id ?? "__unclustered__";
    const bucket = groups.get(key);
    if (bucket) {
      bucket.push(entry);
    } else {
      groups.set(key, [entry]);
    }
  }

  const clusters: PriorityCluster[] = [];
  for (const [key, members] of groups.entries()) {
    // Use the first populated cluster_size value as the authoritative
    // size (per-member size is mirrored so any entry carries it); when
    // none is present we fall back to the actual member count.
    const declaredSize = members.find(
      (m) => m.cluster_size != null,
    )?.cluster_size;
    const size = declaredSize ?? members.length;

    // Centroid is the mean of member coordinates when any members carry
    // a station lat/lon on the entry. Priority entries from the current
    // backend don't include geo on the row itself, so this is best-
    // effort — in most cases centroid will be ``null`` and the cluster
    // row renders without coordinates.
    const geos = members
      .map((m: any) => ({
        lat: typeof m.lat === "number" ? m.lat : null,
        lon: typeof m.lon === "number" ? m.lon : null,
      }))
      .filter(
        (g): g is { lat: number; lon: number } =>
          g.lat != null && g.lon != null,
      );
    const centroid =
      geos.length > 0
        ? {
            lat: geos.reduce((a, g) => a + g.lat, 0) / geos.length,
            lon: geos.reduce((a, g) => a + g.lon, 0) / geos.length,
          }
        : null;

    clusters.push({
      cluster_id: key === "__unclustered__" ? "Unclustered" : key,
      cluster_size: size,
      centroid,
      members,
    });
  }

  // Stable ordering: clustered rows first (by size desc), Unclustered
  // last so the dispatcher sees the dense geographic groups up top.
  clusters.sort((a, b) => {
    const aUn = a.cluster_id === "Unclustered";
    const bUn = b.cluster_id === "Unclustered";
    if (aUn !== bUn) return aUn ? 1 : -1;
    return b.cluster_size - a.cluster_size;
  });
  return clusters;
}

function PrioritiesTab() {
  const [runs, setRuns] = useState<PriorityListEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [bucketFilter, setBucketFilter] = useState<SafeToDelayBucket | "">("");
  const [clusterView, setClusterView] = useState(false);

  const loadPriorities = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await getPriorityLists({
        // The ``bucketFilter`` query hits ES as a nested query, so the
        // returned runs are already filtered to those containing at
        // least one entry in the chosen bucket (Req 3.1.4).
        safe_to_delay_bucket: bucketFilter || undefined,
        page: 1,
        size: 1,
      });
      const raw = (result as any).data ?? (result as any).items ?? [];
      setRuns(Array.isArray(raw) ? (raw as PriorityListEntry[]) : []);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load priorities",
      );
    } finally {
      setLoading(false);
    }
  }, [bucketFilter]);

  useEffect(() => {
    loadPriorities();
  }, [loadPriorities]);

  // Flatten all entries from the returned priority runs. When a bucket
  // filter is set we additionally narrow client-side to just entries
  // matching the bucket (the server filter only guarantees at least one
  // entry matches per run).
  const entries: PriorityEntry[] = useMemo(() => {
    const flat: PriorityEntry[] = [];
    for (const run of runs) {
      const rows = Array.isArray(run?.priorities) ? run.priorities : [];
      for (const row of rows) {
        if (bucketFilter && row.safe_to_delay_bucket !== bucketFilter) {
          continue;
        }
        flat.push(row);
      }
    }
    return flat;
  }, [runs, bucketFilter]);

  const clusters = useMemo(() => groupByCluster(entries), [entries]);

  // Client-side pagination of the flattened rows (flat list view only).
  const totalItems = entries.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  const paginatedItems = entries.slice(
    (page - 1) * PAGE_SIZE,
    page * PAGE_SIZE,
  );
  const paginationMeta: PaginationMeta = {
    page,
    size: PAGE_SIZE,
    total: totalItems,
    total_pages: totalPages,
  };

  // Reset to page 1 when the filter or view mode changes so the user
  // isn't stranded on an empty page after narrowing the list.
  useEffect(() => {
    setPage(1);
  }, [bucketFilter, clusterView]);

  const handlePageChange = useCallback((newPage: number) => {
    setPage(newPage);
  }, []);

  const inputClass =
    "px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-[#232323]">
          Delivery Priorities
        </h3>
        <button
          onClick={loadPriorities}
          className="p-1.5 text-gray-400 hover:text-gray-600 rounded"
          aria-label="Refresh priorities"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {/* Filters: safe_to_delay_bucket + cluster view toggle (Req 3.1.4, 3.4.3) */}
      <div className="flex items-center gap-4">
        <div className="flex flex-col gap-1">
          <label
            htmlFor="safe-to-delay-bucket"
            className="text-[11px] font-medium text-gray-600"
          >
            Safe to delay
          </label>
          <select
            id="safe-to-delay-bucket"
            value={bucketFilter}
            onChange={(e) =>
              setBucketFilter((e.target.value as SafeToDelayBucket | "") || "")
            }
            className={inputClass}
          >
            <option value="">Any</option>
            {(
              Object.keys(SAFE_TO_DELAY_BUCKET_LABELS) as SafeToDelayBucket[]
            ).map((bucket) => (
              <option key={bucket} value={bucket}>
                {SAFE_TO_DELAY_BUCKET_LABELS[bucket]}
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <span className="text-[11px] font-medium text-gray-600">View</span>
          <button
            type="button"
            onClick={() => setClusterView((v) => !v)}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border transition-colors ${
              clusterView
                ? "bg-gray-900 text-white border-gray-900"
                : "bg-white text-gray-600 border-gray-200 hover:bg-gray-50"
            }`}
            aria-pressed={clusterView}
          >
            <Layers className="w-4 h-4" />
            Cluster view
          </button>
        </div>
      </div>

      {error && (
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {error}
        </p>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 text-gray-400 animate-spin" />
        </div>
      ) : entries.length === 0 ? (
        <div className="text-center py-16 text-gray-400">
          <p className="text-sm">No delivery priorities found</p>
          {bucketFilter && (
            <p className="text-xs mt-1">
              Try changing the safe-to-delay filter.
            </p>
          )}
        </div>
      ) : clusterView ? (
        <div className="overflow-x-auto border border-gray-100 rounded-lg">
          <table className="w-full" aria-label="Delivery priorities by cluster">
            <thead className="bg-gray-50 border-b border-gray-100">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Cluster
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Size
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Centroid
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Members
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {clusters.map((c) => (
                <tr key={c.cluster_id} className="hover:bg-gray-50 align-top">
                  <td className="px-4 py-3 text-sm font-medium text-[#232323]">
                    {c.cluster_id}
                  </td>
                  <td className="px-4 py-3 text-sm text-right text-gray-700">
                    {c.cluster_size}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">
                    {c.centroid
                      ? `${c.centroid.lat.toFixed(4)}, ${c.centroid.lon.toFixed(4)}`
                      : "—"}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-600">
                    <div className="flex flex-wrap gap-1">
                      {c.members.slice(0, 8).map((m, i) => (
                        <span
                          key={`${m.station_id ?? m.customer_tank_id ?? i}-${i}`}
                          className="inline-flex items-center gap-1 px-2 py-0.5 bg-gray-100 rounded text-[11px]"
                        >
                          {m.station_id ?? m.customer_tank_id ?? "?"}
                          {m.fuel_grade ? ` · ${m.fuel_grade}` : ""}
                        </span>
                      ))}
                      {c.members.length > 8 && (
                        <span className="text-gray-400 text-[11px]">
                          +{c.members.length - 8} more
                        </span>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="overflow-x-auto border border-gray-100 rounded-lg">
          <table className="w-full" aria-label="Delivery priorities">
            <thead className="bg-gray-50 border-b border-gray-100">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Station
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Name
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Fuel Grade
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Priority Score
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Urgency
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Safe to Delay
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Cluster
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                  Timestamp
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {paginatedItems.map((p, i) => {
                const urgencyKey =
                  (p as any).urgency ?? p.priority_bucket ?? "low";
                const urgencyStyle =
                  URGENCY_CONFIG[urgencyKey] ?? URGENCY_CONFIG.low;
                const bucket = p.safe_to_delay_bucket;
                const bucketBadge = bucket
                  ? SAFE_TO_DELAY_BADGE[bucket]
                  : "bg-gray-100 text-gray-500";
                return (
                  <tr
                    key={`${p.station_id ?? p.customer_tank_id ?? "row"}-${p.fuel_grade}-${i}`}
                    className="hover:bg-gray-50"
                  >
                    <td className="px-4 py-3 text-sm font-medium text-[#232323]">
                      {p.station_id ?? p.customer_tank_id ?? "—"}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      {p.station_name ??
                        p.station_id ??
                        p.customer_tank_id ??
                        "—"}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      {p.fuel_grade}
                    </td>
                    <td className="px-4 py-3 text-sm text-right text-gray-700">
                      {(p.priority_score ?? 0).toFixed(2)}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${urgencyStyle.bg} ${urgencyStyle.color}`}
                      >
                        {urgencyKey}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium ${bucketBadge}`}
                      >
                        {bucket ?? "—"}
                        {p.safe_to_delay_days != null
                          ? ` · ${p.safe_to_delay_days}d`
                          : ""}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-600">
                      {p.cluster_id ? (
                        <span>
                          {p.cluster_id}
                          {p.cluster_size != null
                            ? ` (n=${p.cluster_size})`
                            : ""}
                        </span>
                      ) : (
                        <span className="text-gray-400">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500">
                      {p.timestamp
                        ? new Date(p.timestamp).toLocaleString()
                        : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {paginationMeta.total_pages > 1 && (
            <PaginationControls
              pagination={paginationMeta}
              onPageChange={handlePageChange}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ─── Main Page Component ─────────────────────────────────────────────────────

export default function FuelDistributionPage() {
  const [activeTab, setActiveTab] = useState<TabId>("plans");

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50">
      {/* Storm_Mode banner (Task 11.7, Req 9.4.1) — pinned to the top of
          operations control pages, visible only when the backend reports
          Storm_Mode is active. Dispatcher/admin roles see the override
          form inline; other roles see the advisory without the control. */}
      <StormModeBanner roles={["dispatcher"]} actorId={DISPATCHER_ID} />

      {/* Header */}
      <div className="px-6 pt-6 pb-0">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-9 h-9 bg-blue-600 rounded-lg flex items-center justify-center">
            <Droplets className="w-5 h-5 text-white" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-[#232323]">
              Fuel Distribution
            </h2>
            <p className="text-xs text-gray-500">
              Plan generation, forecasts, and delivery priorities
            </p>
          </div>
        </div>

        {/* Tab bar */}
        <div className="flex items-center gap-1">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${
                activeTab === tab.id
                  ? "bg-white text-[#232323] border border-gray-200 border-b-white -mb-px z-10"
                  : "text-gray-500 hover:text-gray-700 hover:bg-gray-100"
              }`}
            >
              <tab.icon className="w-4 h-4" />
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      <div className="flex-1 min-h-0 overflow-auto bg-white border-t border-gray-200 px-6 py-6">
        {activeTab === "plans" && <PlansTab />}
        {activeTab === "forecasts" && <ForecastsTab />}
        {activeTab === "priorities" && <PrioritiesTab />}
      </div>
    </div>
  );
}
