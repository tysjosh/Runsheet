"use client";

import { Fuel, Plus, Search } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import LoadingSpinner from "../../../components/LoadingSpinner";
import FuelConsumptionChart from "../../../components/ops/FuelConsumptionChart";
import FuelStationDetail from "../../../components/ops/FuelStationDetail";
import FuelStationForm from "../../../components/ops/FuelStationForm";
import FuelStationList from "../../../components/ops/FuelStationList";
import FuelSummaryBar from "../../../components/ops/FuelSummaryBar";
import { useOpsWebSocket } from "../../../hooks/useOpsWebSocket";
import type {
  ConsumptionMetric,
  FuelNetworkSummary,
  FuelStation,
  FuelStationDetail as FuelStationDetailType,
  FuelType,
  StationStatus,
  StationFilters,
} from "../../../services/fuelApi";
import {
  getConsumptionMetrics,
  getNetworkSummary,
  getStation,
  getStations,
} from "../../../services/fuelApi";

const FUEL_TYPE_OPTIONS: { value: "" | FuelType; label: string }[] = [
  { value: "", label: "All Fuel Types" },
  { value: "AGO", label: "AGO (Diesel)" },
  { value: "PMS", label: "PMS (Petrol)" },
  { value: "ATK", label: "ATK (Aviation)" },
  { value: "LPG", label: "LPG (Gas)" },
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

/**
 * Fuel Monitoring Dashboard page.
 *
 * Displays a network summary bar, station list with filters, consumption
 * trend chart, and station detail panel. Subscribes to fuel_alert WebSocket
 * events for real-time stock status updates.
 *
 * Validates: Requirements 6.1-6.7
 */
export default function FuelDashboardPage() {
  const [stations, setStations] = useState<FuelStation[]>([]);
  const [summary, setSummary] = useState<FuelNetworkSummary>(EMPTY_SUMMARY);
  const [consumptionData, setConsumptionData] = useState<ConsumptionMetric[]>([]);
  const [selectedStationId, setSelectedStationId] = useState<string | null>(null);
  const [stationDetail, setStationDetail] = useState<FuelStationDetailType | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);

  // Filter state
  const [fuelTypeFilter, setFuelTypeFilter] = useState<"" | FuelType>("");
  const [statusFilter, setStatusFilter] = useState<"" | StationStatus>("");
  const [locationFilter, setLocationFilter] = useState("");

  // Station form modal state
  const [showStationForm, setShowStationForm] = useState(false);
  const [stationFormMode, setStationFormMode] = useState<"create" | "edit">("create");
  const [editingStation, setEditingStation] = useState<FuelStation | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);

      const filters: StationFilters = {};
      if (fuelTypeFilter) filters.fuel_type = fuelTypeFilter;
      if (statusFilter) filters.status = statusFilter;
      if (locationFilter) filters.location = locationFilter;

      const [stationsRes, summaryRes, metricsRes] = await Promise.all([
        getStations(filters),
        getNetworkSummary(),
        getConsumptionMetrics({ bucket: "daily" }),
      ]);

      setStations(stationsRes.data);
      setSummary(summaryRes.data);
      setConsumptionData(metricsRes.data);
    } catch (error) {
      console.error("Failed to load fuel data:", error);
    } finally {
      setLoading(false);
    }
  }, [fuelTypeFilter, statusFilter, locationFilter]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  /** Load station detail when a station is selected. Validates: Requirement 6.6 */
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

  /** Open FuelStationForm in create mode. Validates: Requirement 8.1 */
  const handleAddStation = useCallback(() => {
    setStationFormMode("create");
    setEditingStation(null);
    setShowStationForm(true);
  }, []);

  /** Open FuelStationForm in edit mode with station data. Validates: Requirement 8.3 */
  const handleEditStation = useCallback((station: FuelStation) => {
    setStationFormMode("edit");
    setEditingStation(station);
    setShowStationForm(true);
  }, []);

  /** Close the station form modal */
  const handleCloseStationForm = useCallback(() => {
    setShowStationForm(false);
    setEditingStation(null);
  }, []);

  /** Handle successful create or edit from FuelStationForm */
  const handleStationFormSuccess = useCallback(
    (savedStation: FuelStation) => {
      if (stationFormMode === "create") {
        // Add the new station to the displayed list
        setStations((prev) => [savedStation, ...prev]);
      } else {
        // Update the station in the displayed list
        setStations((prev) =>
          prev.map((s) =>
            s.station_id === savedStation.station_id ? savedStation : s,
          ),
        );
        // Also update the detail panel if this station is currently selected
        if (selectedStationId === savedStation.station_id && stationDetail) {
          setStationDetail((prev) =>
            prev ? { ...prev, station: savedStation } : prev,
          );
        }
      }
    },
    [stationFormMode, selectedStationId, stationDetail],
  );

  /**
   * Handle real-time fuel alert updates via WebSocket.
   * Updates the affected station row within 5 seconds.
   *
   * Validates: Requirements 6.5
   */
  const handleFuelAlert = useCallback(
    (alert: { station_id: string; status: string; current_stock_liters: number }) => {
      setStations((prev) =>
        prev.map((s) =>
          s.station_id === alert.station_id
            ? { ...s, status: alert.status as StationStatus, current_stock_liters: alert.current_stock_liters }
            : s,
        ),
      );
      // Refresh summary to reflect updated alert counts
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

  if (loading) {
    return <LoadingSpinner message="Loading fuel dashboard..." />;
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <Fuel className="w-5 h-5 text-white" />
          </div>
          <div className="flex-1">
            <h1 className="text-2xl font-semibold text-[#232323]">
              Fuel Monitoring
            </h1>
            <p className="text-gray-500">
              Track fuel stock levels, alerts, and consumption trends
            </p>
          </div>
          <button
            type="button"
            onClick={handleAddStation}
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white rounded-lg hover:opacity-90 transition-opacity"
            style={{ backgroundColor: "#232323" }}
          >
            <Plus className="w-4 h-4" aria-hidden="true" />
            Add Station
          </button>
        </div>

        {/* Filters — Validates: Requirement 6.4 */}
        <div className="flex flex-wrap items-center gap-3">
          <select
            value={fuelTypeFilter}
            onChange={(e) => setFuelTypeFilter(e.target.value as "" | FuelType)}
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
            onChange={(e) => setStatusFilter(e.target.value as "" | StationStatus)}
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
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" aria-hidden="true" />
            <input
              type="text"
              value={locationFilter}
              onChange={(e) => setLocationFilter(e.target.value)}
              placeholder="Filter by location..."
              className="pl-8 pr-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
              aria-label="Filter by location"
            />
          </div>
        </div>
      </div>

      {/* Summary Bar — Validates: Requirement 6.2 */}
      <div className="border-b border-gray-100 px-8 py-4">
        <FuelSummaryBar summary={summary} />
      </div>

      {/* Main content area */}
      <div className="flex-1 overflow-hidden flex">
        {/* Left: Station list + chart */}
        <div className={`flex-1 flex flex-col overflow-hidden ${stationDetail ? "lg:w-3/5" : "w-full"}`}>
          {/* Consumption Chart — Validates: Requirement 6.3 */}
          <div className="border-b border-gray-100 px-8 py-6">
            <h2 className="text-sm font-medium text-gray-700 mb-3">
              Daily Consumption Trend
            </h2>
            <FuelConsumptionChart data={consumptionData} />
          </div>

          {/* Station List — Validates: Requirements 6.1, 6.4 */}
          <div className="flex-1 overflow-y-auto">
            <FuelStationList
              stations={stations}
              onSelectStation={handleSelectStation}
              selectedStationId={selectedStationId}
              onEditStation={handleEditStation}
            />
          </div>
        </div>

        {/* Right: Station Detail Panel — Validates: Requirement 6.6 */}
        {selectedStationId && (
          <div className="hidden lg:block w-2/5 border-l border-gray-100 overflow-y-auto p-4">
            {detailLoading ? (
              <LoadingSpinner message="Loading station detail..." />
            ) : stationDetail ? (
              <FuelStationDetail detail={stationDetail} onClose={handleCloseDetail} />
            ) : (
              <p className="text-sm text-gray-400 text-center py-8">
                Failed to load station detail
              </p>
            )}
          </div>
        )}
      </div>

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
