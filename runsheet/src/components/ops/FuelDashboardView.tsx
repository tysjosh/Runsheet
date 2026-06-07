"use client";

import {
  AlertTriangle,
  BarChart3,
  Fuel,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useOpsWebSocket } from "../../hooks/useOpsWebSocket";
import type {
  ConsumptionMetric,
  FuelNetworkSummary,
  FuelStation,
  FuelStationDetail as FuelStationDetailType,
  FuelType,
  StationFilters,
  StationStatus,
} from "../../services/fuelApi";
import {
  getConsumptionMetrics,
  getNetworkSummary,
  getStation,
  getStations,
} from "../../services/fuelApi";
import LoadingSpinner from "../LoadingSpinner";
import FuelConsumptionChart from "../ops/FuelConsumptionChart";
import FuelStationDetail from "../ops/FuelStationDetail";
import FuelStationForm from "../ops/FuelStationForm";
import FuelStationList from "../ops/FuelStationList";
import FuelSummaryBar from "../ops/FuelSummaryBar";
import { Button, PageHeader, type Tab, TabNavigation } from "../ui";

const FUEL_TYPE_OPTIONS: { value: "" | FuelType; label: string }[] = [
  { value: "", label: "All Fuel Types" },
  { value: "DIESEL_2", label: "Diesel #2" },
  { value: "GASOLINE_REG", label: "Regular Unleaded" },
  { value: "GASOLINE_PREM", label: "Premium Unleaded" },
  { value: "PROPANE", label: "Propane" },
  { value: "KEROSENE", label: "Kerosene" },
  { value: "DEF", label: "DEF" },
];

const STATUS_OPTIONS: { value: "" | StationStatus; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "normal", label: "Normal" },
  { value: "low", label: "Low" },
  { value: "critical", label: "Critical" },
  { value: "empty", label: "Empty" },
];

const EMPTY_SUMMARY: FuelNetworkSummary = {
  total_stations: 0,
  total_capacity_liters: 0,
  total_current_stock_liters: 0,
  total_daily_consumption: 0,
  average_days_until_empty: 0,
  stations_normal: 0,
  stations_low: 0,
  stations_critical: 0,
  stations_empty: 0,
  active_alerts: 0,
};

const TABS: Tab[] = [
  {
    id: "stations",
    label: "Fuel Stations",
    icon: <Fuel className="w-4 h-4" />,
  },
  {
    id: "efficiency",
    label: "Consumption",
    icon: <BarChart3 className="w-4 h-4" />,
  },
];

type TabId = string;

// Fallback poll so stock levels recover if the ops WebSocket drops or misses
// a push — a monitoring screen can't silently go stale.
const REFRESH_INTERVAL_MS = 60_000;

function formatRelative(date: Date | null): string {
  if (!date) return "";
  const secs = Math.round((Date.now() - date.getTime()) / 1000);
  if (secs < 5) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

interface FuelDashboardPageProps {
  /**
   * When embedded in the Fuel Ops hub, the hub owns the page header + the
   * top-level tab set, so this view suppresses its own header/tabs and renders
   * the panel for the controlled `view`. Standalone (`/ops/fuel`) it keeps its
   * own header and tabs.
   */
  embedded?: boolean;
  /** Controlled view when embedded. */
  view?: "stations" | "efficiency";
}

/**
 * Fuel Monitoring dashboard.
 *
 * Network summary bar, station list with filters, consumption trend, and a
 * station detail panel (side panel on `lg`, slide-over drawer below). Each
 * data source fails open independently, with a fallback poll + manual refresh
 * so the view always re-syncs; routine filter changes update the list in place
 * rather than blanking the whole screen.
 *
 * Validates: Requirements 6.1-6.7, 8.1, 8.3
 */
export default function FuelDashboardView({
  embedded = false,
  view,
}: FuelDashboardPageProps = {}) {
  const [stations, setStations] = useState<FuelStation[]>([]);
  const [summary, setSummary] = useState<FuelNetworkSummary>(EMPTY_SUMMARY);
  const [consumptionData, setConsumptionData] = useState<ConsumptionMetric[]>(
    [],
  );
  const [selectedStationId, setSelectedStationId] = useState<string | null>(
    null,
  );
  const [stationDetail, setStationDetail] =
    useState<FuelStationDetailType | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Filter state
  const [fuelTypeFilter, setFuelTypeFilter] = useState<"" | FuelType>("");
  const [statusFilter, setStatusFilter] = useState<"" | StationStatus>("");
  const [locationFilter, setLocationFilter] = useState("");

  // Station form modal state
  const [showStationForm, setShowStationForm] = useState(false);
  const [stationFormMode, setStationFormMode] = useState<"create" | "edit">(
    "create",
  );
  const [editingStation, setEditingStation] = useState<FuelStation | null>(
    null,
  );

  const [internalTab, setInternalTab] = useState<TabId>("stations");
  const activeTab: TabId = embedded ? (view ?? "stations") : internalTab;

  const loadData = useCallback(async () => {
    setRefreshing(true);
    const filters: StationFilters = {};
    if (fuelTypeFilter) filters.fuel_type = fuelTypeFilter;
    if (statusFilter) filters.status = statusFilter;
    if (locationFilter) filters.location = locationFilter;

    // Each source fails open independently — one bad endpoint must not blank
    // the others. On failure we keep the last-known values and flag the error.
    const results = await Promise.allSettled([
      getStations(filters),
      getNetworkSummary(),
      getConsumptionMetrics({ bucket: "daily" }),
    ]);
    const [stationsRes, summaryRes, metricsRes] = results;

    if (stationsRes.status === "fulfilled") setStations(stationsRes.value.data);
    if (summaryRes.status === "fulfilled") setSummary(summaryRes.value.data);
    if (metricsRes.status === "fulfilled") {
      setConsumptionData(metricsRes.value.data);
    }

    setLoadError(results.some((r) => r.status === "rejected"));
    setLastUpdated(new Date());
    setLoading(false);
    setRefreshing(false);
  }, [fuelTypeFilter, statusFilter, locationFilter]);

  // Initial load + reload on filter change.
  useEffect(() => {
    loadData();
  }, [loadData]);

  // Slow fallback poll.
  useEffect(() => {
    const id = setInterval(() => loadData(), REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [loadData]);

  const loadStationDetail = useCallback(async (stationId: string) => {
    try {
      setDetailLoading(true);
      const res = await getStation(stationId);
      setStationDetail(res.data);
    } catch (error) {
      console.error("Failed to load station detail:", error);
      setStationDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const handleSelectStation = useCallback(
    (stationId: string) => {
      if (selectedStationId === stationId) {
        setSelectedStationId(null);
        setStationDetail(null);
      } else {
        setSelectedStationId(stationId);
        loadStationDetail(stationId);
      }
    },
    [selectedStationId, loadStationDetail],
  );

  const handleCloseDetail = useCallback(() => {
    setSelectedStationId(null);
    setStationDetail(null);
  }, []);

  const handleAddStation = useCallback(() => {
    setStationFormMode("create");
    setEditingStation(null);
    setShowStationForm(true);
  }, []);

  const handleEditStation = useCallback((station: FuelStation) => {
    setStationFormMode("edit");
    setEditingStation(station);
    setShowStationForm(true);
  }, []);

  const handleCloseStationForm = useCallback(() => {
    setShowStationForm(false);
    setEditingStation(null);
  }, []);

  const handleStationFormSuccess = useCallback(
    (savedStation: FuelStation) => {
      if (stationFormMode === "create") {
        setStations((prev) => [savedStation, ...prev]);
      } else {
        setStations((prev) =>
          prev.map((s) =>
            s.station_id === savedStation.station_id ? savedStation : s,
          ),
        );
        if (selectedStationId === savedStation.station_id && stationDetail) {
          setStationDetail((prev) =>
            prev ? { ...prev, station: savedStation } : prev,
          );
        }
      }
    },
    [stationFormMode, selectedStationId, stationDetail],
  );

  const handleFuelAlert = useCallback(
    (alert: {
      station_id: string;
      status: string;
      current_stock_liters: number;
    }) => {
      setStations((prev) =>
        prev.map((s) =>
          s.station_id === alert.station_id
            ? {
                ...s,
                status: alert.status as StationStatus,
                current_stock_liters: alert.current_stock_liters,
              }
            : s,
        ),
      );
      getNetworkSummary()
        .then((res) => setSummary(res.data))
        .catch(() => {});
    },
    [],
  );

  useOpsWebSocket({
    subscriptions: ["fuel_alert"],
    onFuelAlert: handleFuelAlert,
  });

  // First load only — routine reloads (filters/poll/refresh) keep the chrome.
  if (loading) {
    return <LoadingSpinner message="Loading fuel dashboard..." />;
  }

  const detailPanel = detailLoading ? (
    <LoadingSpinner message="Loading station detail..." />
  ) : stationDetail ? (
    <FuelStationDetail
      detail={stationDetail}
      onClose={handleCloseDetail}
      onEventRecorded={() => {
        if (selectedStationId) loadStationDetail(selectedStationId);
        loadData();
      }}
    />
  ) : (
    <p className="text-sm text-gray-500 text-center py-8">
      Failed to load station detail
    </p>
  );

  const toolbar = (
    <div className="flex flex-wrap items-center justify-between gap-3 px-8 py-3">
      <div className="min-h-[1.25rem]">
        {loadError && (
          <span
            role="alert"
            className="inline-flex items-center gap-1.5 text-xs text-warning-dark"
          >
            <AlertTriangle className="h-3.5 w-3.5" />
            Some fuel data failed to load — showing last known values.
          </span>
        )}
      </div>
      <div className="flex items-center gap-3">
        {lastUpdated && (
          <span className="text-xs text-gray-500">
            Updated {formatRelative(lastUpdated)}
          </span>
        )}
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={() => loadData()}
          icon={
            <RefreshCw
              className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`}
            />
          }
        >
          Refresh
        </Button>
        <Button
          type="button"
          size="sm"
          onClick={handleAddStation}
          icon={<Plus className="w-4 h-4" aria-hidden="true" />}
        >
          Add Station
        </Button>
      </div>
    </div>
  );

  return (
    <div className="h-full flex flex-col bg-white">
      {!embedded && (
        <PageHeader
          title="Fuel Monitoring"
          subtitle="Track fuel stock levels, alerts, and consumption trends"
          icon={<Fuel className="w-5 h-5" />}
        />
      )}

      {toolbar}

      {/* Summary Bar — Validates: Requirement 6.2 */}
      <div className="border-b border-gray-100 px-8 py-4">
        <FuelSummaryBar summary={summary} />
      </div>

      {/* Main content area */}
      <div className="flex-1 overflow-hidden flex flex-col">
        {!embedded && (
          <TabNavigation
            tabs={TABS}
            activeTab={internalTab}
            onChange={setInternalTab}
            className="!px-8 pt-4"
          />
        )}

        <div className="flex-1 overflow-hidden flex border-t border-gray-200">
          {activeTab === "efficiency" && (
            <div className="flex-1 overflow-y-auto">
              <div className="px-8 py-6">
                <h2 className="text-sm font-medium text-gray-700 mb-3">
                  Daily Consumption Trend
                </h2>
                <FuelConsumptionChart data={consumptionData} />
              </div>
            </div>
          )}

          {activeTab === "stations" && (
            <div className="flex-1 overflow-hidden flex">
              {/* Left: Station list */}
              <div
                className={`flex-1 overflow-y-auto ${selectedStationId ? "lg:w-3/5" : "w-full"}`}
              >
                <div className="px-8 py-6">
                  {/* Filters — Validates: Requirement 6.4 */}
                  <div className="flex flex-wrap items-center gap-3 mb-4">
                    <select
                      value={fuelTypeFilter}
                      onChange={(e) =>
                        setFuelTypeFilter(e.target.value as "" | FuelType)
                      }
                      className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg bg-white focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
                      aria-label="Filter by fuel type"
                    >
                      {FUEL_TYPE_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>

                    <select
                      value={statusFilter}
                      onChange={(e) =>
                        setStatusFilter(e.target.value as "" | StationStatus)
                      }
                      className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg bg-white focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
                      aria-label="Filter by status"
                    >
                      {STATUS_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>

                    <div className="relative">
                      <Search
                        className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500"
                        aria-hidden="true"
                      />
                      <input
                        type="text"
                        value={locationFilter}
                        onChange={(e) => setLocationFilter(e.target.value)}
                        placeholder="Filter by location..."
                        className="pl-8 pr-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
                        aria-label="Filter by location"
                      />
                    </div>

                    {refreshing && (
                      <RefreshCw className="w-4 h-4 text-gray-400 animate-spin" />
                    )}
                  </div>

                  <FuelStationList
                    stations={stations}
                    onSelectStation={handleSelectStation}
                    selectedStationId={selectedStationId}
                    onEditStation={handleEditStation}
                  />
                </div>
              </div>

              {/* Right: Station Detail — side panel on lg+ */}
              {selectedStationId && (
                <div className="hidden lg:block w-2/5 border-l border-gray-100 overflow-y-auto p-4">
                  {detailPanel}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Station Detail — slide-over drawer below lg so the click works on
          tablet/phone (the side panel is desktop-only). */}
      {selectedStationId && (
        <div
          className="fixed inset-0 z-50 flex lg:hidden"
          role="dialog"
          aria-modal="true"
        >
          <button
            type="button"
            aria-label="Close station detail"
            className="absolute inset-0 bg-black/40"
            onClick={handleCloseDetail}
          />
          <div className="relative ml-auto h-full w-full max-w-md overflow-y-auto bg-white p-4 shadow-xl">
            <button
              type="button"
              onClick={handleCloseDetail}
              aria-label="Close"
              className="absolute right-3 top-3 rounded-md p-1.5 text-gray-500 hover:bg-gray-100"
            >
              <X className="h-5 w-5" />
            </button>
            {detailPanel}
          </div>
        </div>
      )}

      {/* Station Form Modal — Validates: Requirements 8.1, 8.3 */}
      {showStationForm && (
        <FuelStationForm
          mode={stationFormMode}
          station={editingStation}
          onClose={handleCloseStationForm}
          onSuccess={handleStationFormSuccess}
        />
      )}
    </div>
  );
}
