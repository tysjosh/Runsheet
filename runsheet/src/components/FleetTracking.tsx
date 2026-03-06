import { useCallback, useEffect, useState } from "react";
import { type LocationUpdateData, useFleetWebSocket } from "../hooks";
import { apiService } from "../services/api";
import type { AssetSubtype, AssetSummary, AssetType, Truck } from "../types/api";
import LoadingSpinner from "./LoadingSpinner";
import { WebSocketStatusBadge } from "./WebSocketStatus";

/** Filter options for the asset type dropdown */
const ASSET_TYPE_OPTIONS: { label: string; value: AssetType | "all" }[] = [
  { label: "All", value: "all" },
  { label: "Vehicles", value: "vehicle" },
  { label: "Vessels", value: "vessel" },
  { label: "Equipment", value: "equipment" },
  { label: "Containers", value: "container" },
];

/** Display labels for asset types in the summary bar */
const ASSET_TYPE_LABELS: Record<AssetType, { label: string; icon: string }> = {
  vehicle: { label: "Vehicles", icon: "🚛" },
  vessel: { label: "Vessels", icon: "🚢" },
  equipment: { label: "Equipment", icon: "🏗️" },
  container: { label: "Containers", icon: "📦" },
};

interface FleetTrackingProps {
  onTruckSelect?: (truck: Truck) => void;
}

export default function FleetTracking({ onTruckSelect }: FleetTrackingProps) {
  const [trucks, setTrucks] = useState<Truck[]>([]);
  const [fleetSummary, setFleetSummary] = useState<AssetSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [showInTransit, setShowInTransit] = useState(true);
  const [selectedTruck, setSelectedTruck] = useState<string | null>(null);
  const [assetTypeFilter, setAssetTypeFilter] = useState<AssetType | "all">("all");

  /**
   * Handle real-time location updates from WebSocket
   * Updates the truck's current location in the local state
   */
  const handleLocationUpdate = useCallback((update: LocationUpdateData) => {
    setTrucks((prevTrucks) =>
      prevTrucks.map((truck) => {
        if (truck.id === update.truck_id) {
          return {
            ...truck,
            currentLocation: {
              ...truck.currentLocation,
              coordinates: {
                lat: update.coordinates.lat,
                lon: update.coordinates.lon,
              },
            },
            lastUpdate: update.timestamp,
            ...(update.asset_type ? { assetType: update.asset_type as AssetType } : {}),
            ...(update.asset_subtype ? { assetSubtype: update.asset_subtype as AssetSubtype } : {}),
          };
        }
        return truck;
      }),
    );
  }, []);

  /**
   * Handle batch location updates from WebSocket
   */
  const handleBatchLocationUpdate = useCallback(
    (updates: LocationUpdateData[]) => {
      setTrucks((prevTrucks) => {
        const updateMap = new Map(updates.map((u) => [u.truck_id, u]));

        return prevTrucks.map((truck) => {
          const update = updateMap.get(truck.id);
          if (update) {
            return {
              ...truck,
              currentLocation: {
                ...truck.currentLocation,
                coordinates: {
                  lat: update.coordinates.lat,
                  lon: update.coordinates.lon,
                },
              },
              lastUpdate: update.timestamp,
              ...(update.asset_type ? { assetType: update.asset_type as AssetType } : {}),
              ...(update.asset_subtype ? { assetSubtype: update.asset_subtype as AssetSubtype } : {}),
            };
          }
          return truck;
        });
      });
    },
    [],
  );

  /**
   * WebSocket connection for real-time fleet updates
   * Validates: Requirement 9.5 - automatic reconnection with exponential backoff
   */
  const { state: wsState, reconnectAttempt } = useFleetWebSocket({
    autoConnect: true,
    onLocationUpdate: handleLocationUpdate,
    onBatchLocationUpdate: handleBatchLocationUpdate,
    onReconnecting: (attempt, delay) => {
      console.log(
        `Fleet WebSocket reconnecting in ${delay}ms (attempt ${attempt})`,
      );
    },
  });

  const [error, setError] = useState<string | null>(null);

  const loadFleetData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      // Use getTrucks for backward compat when filter is "all" or "vehicle"
      // Use getAssets with asset_type filter for other types
      const trucksPromise =
        assetTypeFilter === "all" || assetTypeFilter === "vehicle"
          ? apiService.getTrucks()
          : apiService.getAssets({ asset_type: assetTypeFilter });

      const [trucksResponse, summaryResponse] = await Promise.all([
        trucksPromise,
        apiService.getFleetSummary(),
      ]);

      setTrucks(trucksResponse.data);
      setFleetSummary(summaryResponse.data);
    } catch (err) {
      console.error("Failed to load fleet data:", err);
      setError("Unable to connect to the fleet API. Make sure the backend is running.");
    } finally {
      setLoading(false);
    }
  }, [assetTypeFilter]);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      if (cancelled) return;
      await loadFleetData();
    };

    load();

    return () => {
      cancelled = true;
    };
  }, [loadFleetData]);

  const handleTruckClick = (truck: Truck) => {
    setSelectedTruck(truck.id);
    onTruckSelect?.(truck);
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "on_time":
        return "text-green-600";
      case "delayed":
        return "text-red-600";
      case "stopped":
        return "text-yellow-600";
      default:
        return "text-gray-600";
    }
  };

  const getStatusBg = (status: string) => {
    switch (status) {
      case "on_time":
        return "bg-green-50";
      case "delayed":
        return "bg-red-50";
      case "stopped":
        return "bg-yellow-50";
      default:
        return "bg-gray-50";
    }
  };

  const getStatusDot = (status: string) => {
    switch (status) {
      case "on_time":
        return "bg-green-500";
      case "delayed":
        return "bg-red-500";
      case "stopped":
        return "bg-yellow-500";
      default:
        return "bg-gray-500";
    }
  };

  const formatStatus = (status: string) => {
    return status.replace("_", " ").replace(/\b\w/g, (l) => l.toUpperCase());
  };

  const formatAssetLabel = (value: string) => {
    return value.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
  };

  const calculateTimeToArrival = (estimatedArrival: string) => {
    const now = new Date();
    const arrival = new Date(estimatedArrival);
    const diffMs = arrival.getTime() - now.getTime();
    const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
    const diffMinutes = Math.floor((diffMs % (1000 * 60 * 60)) / (1000 * 60));

    if (diffMs < 0) {
      return `${Math.abs(diffHours)}h ${Math.abs(diffMinutes)}m late`;
    }
    return `${diffHours}h ${diffMinutes}m`;
  };

  const filteredTrucks = showInTransit
    ? trucks.filter((truck) => ["on_time", "delayed"].includes(truck.status))
    : trucks;

  if (loading) {
    return <LoadingSpinner message="Loading fleet data..." />;
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center max-w-md">
          <p className="text-red-600 font-medium mb-2">Connection Error</p>
          <p className="text-gray-500 text-sm mb-4">{error}</p>
          <button
            onClick={loadFleetData}
            className="px-4 py-2 bg-gray-900 text-white text-sm rounded-lg hover:bg-gray-800 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Compact Header */}
      <div className="border-b border-gray-200 px-4 py-2 flex-shrink-0">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-bold text-gray-900">Fleet Tracking</h1>
            <WebSocketStatusBadge
              state={wsState}
              reconnectAttempt={reconnectAttempt}
            />
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-sm">
              <span className="text-gray-600">Asset type</span>
              <select
                value={assetTypeFilter}
                onChange={(e) => setAssetTypeFilter(e.target.value as AssetType | "all")}
                className="text-sm border border-gray-300 rounded px-2 py-1 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              >
                {ASSET_TYPE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 text-sm">
              <span className="text-gray-600">In transit only</span>
              <input
                type="checkbox"
                checked={showInTransit}
                onChange={(e) => setShowInTransit(e.target.checked)}
                className="w-4 h-4 text-blue-600 rounded focus:ring-2 focus:ring-blue-500"
              />
            </label>
            <button
              onClick={loadFleetData}
              className="text-gray-400 hover:text-gray-600 p-1 rounded hover:bg-gray-100"
              title="Refresh"
            >
              <svg
                className="w-4 h-4"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
                />
              </svg>
            </button>
          </div>
        </div>

        {/* Compact Fleet Summary */}
        {fleetSummary && (
          <div className="flex flex-col gap-1 text-xs">
            <div className="flex gap-4">
              <div className="flex items-center gap-1.5">
                <span className="text-gray-600">Total:</span>
                <span className="font-semibold text-gray-900">
                  {fleetSummary.totalTrucks}
                </span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-gray-600">On Time:</span>
                <span className="font-semibold text-green-600">
                  {fleetSummary.onTimeTrucks}
                </span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-gray-600">Delayed:</span>
                <span className="font-semibold text-red-600">
                  {fleetSummary.delayedTrucks}
                </span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-gray-600">Active:</span>
                <span className="font-semibold text-gray-900">
                  {fleetSummary.activeTrucks}
                </span>
              </div>
            </div>
            {fleetSummary.byType && Object.keys(fleetSummary.byType).length > 0 && (
              <div className="flex gap-3 text-gray-500">
                {Object.entries(fleetSummary.byType).map(([type, count]) => {
                  const meta = ASSET_TYPE_LABELS[type as AssetType];
                  return (
                    <div key={type} className="flex items-center gap-1">
                      <span>{meta?.icon}</span>
                      <span>{meta?.label ?? type}:</span>
                      <span className="font-semibold text-gray-700">{count}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Table View - Maximum Space Efficiency */}
      <div className="flex-1 overflow-y-auto min-h-0">
        <table className="w-full">
          <thead className="bg-gray-50 sticky top-0 border-b border-gray-200 z-10">
            <tr>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-600 uppercase">
                Asset
              </th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-600 uppercase">
                Type
              </th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-600 uppercase">
                Route
              </th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-600 uppercase">
                Status
              </th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-600 uppercase">
                ETA
              </th>
              <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-600 uppercase">
                Destination
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {filteredTrucks.map((truck) => (
              <tr
                key={truck.id}
                className={`cursor-pointer transition-colors ${
                  selectedTruck === truck.id ? "bg-blue-50" : "hover:bg-gray-50"
                }`}
                onClick={() => handleTruckClick(truck)}
              >
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <div
                      className={`w-2 h-2 rounded-full ${getStatusDot(truck.status)}`}
                    ></div>
                    <span className="font-medium text-gray-900 text-sm">
                      {truck.plateNumber || truck.name}
                    </span>
                  </div>
                </td>
                <td className="px-3 py-2">
                  <div className="text-xs">
                    <span className="text-gray-900">{formatAssetLabel(truck.assetType ?? "vehicle")}</span>
                    <span className="text-gray-400 ml-1">/ {formatAssetLabel(truck.assetSubtype ?? "truck")}</span>
                  </div>
                </td>
                <td className="px-3 py-2">
                  <div className="text-xs text-gray-600">
                    {truck.route?.origin?.name} → {truck.route?.destination?.name}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <span
                    className={`inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium ${getStatusBg(truck.status)} ${getStatusColor(truck.status)}`}
                  >
                    {formatStatus(truck.status)}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <span className="text-xs text-gray-900">
                    {truck.estimatedArrival
                      ? calculateTimeToArrival(truck.estimatedArrival)
                      : "—"}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <div className="text-xs">
                    <div className="font-medium text-gray-900">
                      {truck.destination?.name ?? "—"}
                    </div>
                    <div className="text-gray-500 text-xs">
                      {truck.destination?.type ?? ""}
                    </div>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {filteredTrucks.length === 0 && (
          <div className="text-center py-12 text-gray-500">
            <svg
              className="w-12 h-12 mx-auto mb-4 text-gray-300"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
              />
            </svg>
            <p>No {assetTypeFilter === "all" ? "assets" : assetTypeFilter + "s"} found</p>
          </div>
        )}
      </div>
    </div>
  );
}
