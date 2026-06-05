import { FileText, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { type LocationUpdateData, useFleetWebSocket } from "../hooks";
import { apiService } from "../services/api";
import type {
  AssetSubtype,
  AssetSummary,
  AssetType,
  Truck,
} from "../types/api";
import LoadingSpinner from "./LoadingSpinner";
import {
  Badge,
  type BadgeVariant,
  Button,
  type Column,
  EmptyState,
  FilterBar,
  PageHeader,
  Pagination,
  StatsBar,
  Table,
} from "./ui";
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
  const [assetTypeFilter, setAssetTypeFilter] = useState<AssetType | "all">(
    "all",
  );
  const [page, setPage] = useState(1);

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
            ...(update.asset_type
              ? { assetType: update.asset_type as AssetType }
              : {}),
            ...(update.asset_subtype
              ? { assetSubtype: update.asset_subtype as AssetSubtype }
              : {}),
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
              ...(update.asset_type
                ? { assetType: update.asset_type as AssetType }
                : {}),
              ...(update.asset_subtype
                ? { assetSubtype: update.asset_subtype as AssetSubtype }
                : {}),
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
      setError(
        "Unable to connect to the fleet API. Make sure the backend is running.",
      );
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

  const getStatusVariant = (status: string): BadgeVariant => {
    switch (status) {
      case "on_time":
        return "success";
      case "delayed":
        return "error";
      case "stopped":
        return "warning";
      default:
        return "neutral";
    }
  };

  const getStatusDot = (status: string) => {
    switch (status) {
      case "on_time":
        return "bg-success";
      case "delayed":
        return "bg-error";
      case "stopped":
        return "bg-warning";
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

  const PAGE_SIZE = 20;
  const totalPages = Math.max(1, Math.ceil(filteredTrucks.length / PAGE_SIZE));
  const paginatedTrucks = filteredTrucks.slice(
    (page - 1) * PAGE_SIZE,
    page * PAGE_SIZE,
  );
  const fleetColumns: Column<Truck>[] = [
    {
      key: "asset",
      label: "Asset",
      render: (truck) => (
        <div className="flex items-center gap-2">
          <div
            className={`w-2 h-2 rounded-full ${getStatusDot(truck.status)}`}
          />
          <span className="font-medium text-gray-900 text-sm">
            {truck.plateNumber || truck.name}
          </span>
        </div>
      ),
    },
    {
      key: "type",
      label: "Type",
      render: (truck) => (
        <div className="text-xs">
          <span className="text-gray-900">
            {formatAssetLabel(truck.assetType ?? "vehicle")}
          </span>
          <span className="text-gray-400 ml-1">
            / {formatAssetLabel(truck.assetSubtype ?? "truck")}
          </span>
        </div>
      ),
    },
    {
      key: "route",
      label: "Route",
      render: (truck) => (
        <div className="text-xs text-gray-600">
          {truck.route?.origin?.name} → {truck.route?.destination?.name}
        </div>
      ),
    },
    {
      key: "status",
      label: "Status",
      render: (truck) => (
        <Badge variant={getStatusVariant(truck.status)} size="sm">
          {formatStatus(truck.status)}
        </Badge>
      ),
    },
    {
      key: "eta",
      label: "ETA",
      render: (truck) => (
        <span className="text-xs text-gray-900">
          {truck.estimatedArrival
            ? calculateTimeToArrival(truck.estimatedArrival)
            : "—"}
        </span>
      ),
    },
    {
      key: "destination",
      label: "Destination",
      render: (truck) => (
        <div className="text-xs">
          <div className="font-medium text-gray-900">
            {truck.destination?.name ?? "—"}
          </div>
          <div className="text-gray-500 text-xs">
            {truck.destination?.type ?? ""}
          </div>
        </div>
      ),
    },
  ];

  if (loading) {
    return <LoadingSpinner message="Loading fleet data..." />;
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center max-w-md">
          <p className="text-error font-medium mb-2">Connection Error</p>
          <p className="text-gray-500 text-sm mb-4">{error}</p>
          <Button onClick={loadFleetData}>Retry</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header with WebSocket Status */}
      <PageHeader
        title="Fleet Tracking"
        badge={
          <WebSocketStatusBadge
            state={wsState}
            reconnectAttempt={reconnectAttempt}
          />
        }
        actions={
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={loadFleetData}
            icon={<RefreshCw className="w-4 h-4" />}
            title="Refresh"
            aria-label="Refresh fleet data"
          />
        }
      />

      {/* Filters */}
      <FilterBar
        filters={
          <>
            <select
              value={assetTypeFilter}
              onChange={(e) => {
                setAssetTypeFilter(e.target.value as AssetType | "all");
                setPage(1);
              }}
              className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
              aria-label="Asset type"
            >
              {ASSET_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-2 px-4 py-3 text-sm border border-gray-200 rounded-xl bg-white cursor-pointer hover:bg-gray-50">
              <input
                type="checkbox"
                checked={showInTransit}
                onChange={(e) => {
                  setShowInTransit(e.target.checked);
                  setPage(1);
                }}
                className="rounded border-gray-300"
              />
              <span>In transit only</span>
            </label>
          </>
        }
      />

      {/* Fleet Summary */}
      {fleetSummary && (
        <StatsBar
          variant="inline"
          stats={[
            { label: "Total", value: fleetSummary.totalTrucks },
            {
              label: "On Time",
              value: fleetSummary.onTimeTrucks,
              color: "success",
            },
            {
              label: "Delayed",
              value: fleetSummary.delayedTrucks,
              color: "error",
            },
            { label: "Active", value: fleetSummary.activeTrucks },
            ...(fleetSummary.byType &&
            Object.keys(fleetSummary.byType).length > 0
              ? Object.entries(fleetSummary.byType).map(([type, count]) => {
                  const meta = ASSET_TYPE_LABELS[type as AssetType];
                  return {
                    label: meta?.label ?? type,
                    value: count,
                    icon: meta?.icon,
                  };
                })
              : []),
          ]}
        />
      )}

      <div className="flex-1 overflow-y-auto min-h-0">
        <Table
          variant="compact"
          columns={fleetColumns}
          data={paginatedTrucks}
          getRowId={(truck) => truck.id}
          selectedId={selectedTruck ?? undefined}
          onRowClick={handleTruckClick}
          emptyState={
            <EmptyState
              icon={<FileText />}
              title={`No ${assetTypeFilter === "all" ? "assets" : `${assetTypeFilter}s`} found`}
            />
          }
        />
        <Pagination
          currentPage={page}
          totalPages={totalPages}
          totalItems={filteredTrucks.length}
          onPageChange={setPage}
          className="px-4"
        />
      </div>
    </div>
  );
}
