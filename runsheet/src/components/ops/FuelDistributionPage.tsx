"use client";

/**
 * Fuel Distribution MVP Pipeline page.
 *
 * Tabbed layout with three tabs:
 * - Plans: Generate plans, view plan details, trigger replanning
 * - Forecasts: Paginated tank forecasts with station/fuel_grade filters
 * - Priorities: Paginated delivery priority rankings
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
  ChevronLeft,
  ChevronRight,
  Droplets,
  Eye,
  Loader2,
  MapPin,
  Play,
  RefreshCw,
  Route,
  Truck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type {
  CompartmentAssignment,
  DeliveryPriority,
  Forecast,
  GeneratePlanResponse,
  LoadingPlan,
  PaginatedResponse,
  PaginationMeta,
  PlanDetail,
  ReplanRequest,
  ReplanResponse,
  RouteAssignment,
  RoutePlan,
} from "../../services/fuelApi";
import {
  generatePlan,
  getForecasts,
  getPlan,
  getPriorities,
  replan,
} from "../../services/fuelApi";

// ─── Constants ───────────────────────────────────────────────────────────────

const TENANT_ID = "default";
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
              onChange={(e) =>
                setForm({ ...form, entity_id: e.target.value })
              }
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

// ─── Plan Detail View ────────────────────────────────────────────────────────

interface PlanDetailViewProps {
  planId: string;
  onBack: () => void;
  onReplan: () => void;
}

function PlanDetailView({ planId, onBack, onReplan }: PlanDetailViewProps) {
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
        <button
          onClick={onReplan}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white rounded-lg"
          style={{ backgroundColor: "#232323" }}
        >
          <RefreshCw className="w-4 h-4" />
          Replan
        </button>
      </div>

      <h3 className="text-sm font-semibold text-[#232323]">
        Plan: {plan.plan_id}
      </h3>

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
                      <span className="text-xs text-gray-500">
                        {route.distance_km.toFixed(1)} km
                      </span>
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
    </div>
  );
}

// ─── Pagination Controls ─────────────────────────────────────────────────────

interface PaginationControlsProps {
  pagination: PaginationMeta;
  onPageChange: (page: number) => void;
}

function PaginationControls({ pagination, onPageChange }: PaginationControlsProps) {
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
  const [plans, setPlans] = useState<GeneratePlanResponse[]>([]);
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [showReplan, setShowReplan] = useState(false);

  const handleGenerate = useCallback(async () => {
    setGenerating(true);
    setError("");
    try {
      const result = await generatePlan(TENANT_ID);
      setPlans((prev) => [result, ...prev]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to generate plan");
    } finally {
      setGenerating(false);
    }
  }, []);

  const handleReplanSuccess = useCallback((response: ReplanResponse) => {
    // Add the replanned entry to the list
    setPlans((prev) => [
      { run_id: response.plan_id, status: response.status },
      ...prev,
    ]);
  }, []);

  // If a plan is selected, show detail view
  if (selectedPlanId) {
    return (
      <>
        <PlanDetailView
          planId={selectedPlanId}
          onBack={() => setSelectedPlanId(null)}
          onReplan={() => setShowReplan(true)}
        />
        {showReplan && (
          <ReplanForm
            planId={selectedPlanId}
            onClose={() => setShowReplan(false)}
            onSuccess={handleReplanSuccess}
          />
        )}
      </>
    );
  }

  return (
    <div className="space-y-4">
      {/* Generate button + error */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-[#232323]">
          Distribution Plans
        </h3>
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

      {error && (
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {error}
        </p>
      )}

      {/* Plan list */}
      {plans.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-gray-400">
          <Truck className="w-8 h-8 mb-2" />
          <p className="text-sm">No plans generated yet</p>
          <p className="text-xs mt-1">
            Click &quot;Generate Plan&quot; to create a distribution plan
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {plans.map((p, i) => (
            <div
              key={`${p.run_id}-${i}`}
              className="flex items-center justify-between border border-gray-100 rounded-lg p-4 hover:border-gray-200 transition-colors"
            >
              <div>
                <p className="text-sm font-medium text-[#232323]">
                  Run: {p.run_id}
                </p>
                <span
                  className={`inline-flex items-center text-[10px] px-1.5 py-0.5 rounded font-medium mt-1 ${
                    p.status === "completed"
                      ? "bg-green-50 text-green-600"
                      : p.status === "failed"
                        ? "bg-red-50 text-red-600"
                        : "bg-yellow-50 text-yellow-600"
                  }`}
                >
                  {p.status}
                </span>
              </div>
              <button
                onClick={() => setSelectedPlanId(p.run_id)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
                aria-label={`View plan ${p.run_id}`}
              >
                <Eye className="w-3 h-3" />
                View
              </button>
            </div>
          ))}
        </div>
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
      setError(
        err instanceof Error ? err.message : "Failed to load forecasts",
      );
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
                <tr key={`${f.station_id}-${f.fuel_grade}-${i}`} className="hover:bg-gray-50">
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
                    {new Date(f.timestamp).toLocaleString()}
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

function PrioritiesTab() {
  const [priorities, setPriorities] = useState<DeliveryPriority[]>([]);
  const [pagination, setPagination] = useState<PaginationMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);

  const loadPriorities = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await getPriorities({
        tenant_id: TENANT_ID,
        page,
        size: PAGE_SIZE,
      });
      // API may return nested priorities array or flat items
      const raw = (result as any).data ?? (result as any).items ?? [];
      // If the API returns a single doc with nested priorities array, flatten it
      const flat = raw.length === 1 && Array.isArray(raw[0]?.priorities)
        ? raw[0].priorities
        : raw;
      setPriorities(flat);
      setPagination((result as any).pagination ?? null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load priorities",
      );
    } finally {
      setLoading(false);
    }
  }, [page]);

  useEffect(() => {
    loadPriorities();
  }, [loadPriorities]);

  const handlePageChange = useCallback((newPage: number) => {
    setPage(newPage);
  }, []);

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-[#232323]">
        Delivery Priorities
      </h3>

      {error && (
        <p className="text-sm text-red-600 bg-red-50 px-4 py-3 rounded-lg">
          {error}
        </p>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 text-gray-400 animate-spin" />
        </div>
      ) : priorities.length === 0 ? (
        <div className="text-center py-16 text-gray-400">
          <p className="text-sm">No delivery priorities found</p>
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
                  Timestamp
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {priorities.map((p, i) => {
                const urgencyStyle = URGENCY_CONFIG[(p as any).urgency ?? (p as any).priority_bucket ?? "low"] ?? URGENCY_CONFIG.low;
                return (
                  <tr key={`${p.station_id}-${p.fuel_grade}-${i}`} className="hover:bg-gray-50">
                    <td className="px-4 py-3 text-sm font-medium text-[#232323]">
                      {p.station_id}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      {(p as any).station_name ?? p.station_id}
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
                        {(p as any).urgency ?? (p as any).priority_bucket ?? "—"}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500">
                      {new Date(p.timestamp).toLocaleString()}
                    </td>
                  </tr>
                );
              })}
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

// ─── Main Page Component ─────────────────────────────────────────────────────

export default function FuelDistributionPage() {
  const [activeTab, setActiveTab] = useState<TabId>("plans");

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50">
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
