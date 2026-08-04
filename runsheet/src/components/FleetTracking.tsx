import { Boxes, FileText, Plus, RefreshCw, X } from "lucide-react";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { type LocationUpdateData, useFleetWebSocket } from "../hooks";
import { useDialogA11y } from "../hooks/useDialogA11y";
import type {
  AssetComplianceSummary,
  CreateAssetPayload,
} from "../services/api";
import { ApiError, apiService } from "../services/api";
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

// Compartments are a property of a truck, reached by clicking the asset rather
// than living as a separate top-level tab. Lazy-loaded into a slide-over.
const TruckCompartmentsPage = lazy(() => import("./ops/TruckCompartmentsPage"));

/** Rows per page in the tracking table. */
const PAGE_SIZE = 20;

/** Statuses kept by the default "In transit only" filter. */
const IN_TRANSIT_STATUSES = ["on_time", "delayed"];

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

/**
 * Chip styling for the per-asset compliance signal sourced from
 * GET /api/fleet/assets/{asset_id}/compliance (Req 11.2). `valid`/`expiring`/
 * `expired` mirror the Drivers qualification chip; `unknown` (no records) and a
 * missing entry render as an "unlinked" affordance.
 */
const COMPLIANCE_CHIP: Record<
  Exclude<AssetComplianceSummary["overall_status"], "unknown">,
  { variant: BadgeVariant; label: string }
> = {
  expired: { variant: "error", label: "Expired" },
  expiring: { variant: "warning", label: "Expiring" },
  valid: { variant: "success", label: "Valid" },
};

/** Asset subtype options grouped by the parent asset type. */
const SUBTYPE_OPTIONS: Record<AssetType, AssetSubtype[]> = {
  vehicle: ["truck", "fuel_truck", "personnel_vehicle"],
  vessel: ["boat", "barge"],
  equipment: ["crane", "forklift"],
  container: ["cargo_container", "ISO_tank"],
};

function labelize(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
}

/**
 * Create a fleet asset (truck/tanker/vessel/…) via POST /fleet/assets.
 * The operator can add a tanker here; its compartments are then defined
 * from the row's Compartments slide-over (or auto-registered when
 * compartments are configured).
 */
function AddAssetModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const [assetId, setAssetId] = useState("");
  const [name, setName] = useState("");
  const [assetType, setAssetType] = useState<AssetType>("vehicle");
  const [assetSubtype, setAssetSubtype] = useState<AssetSubtype>("fuel_truck");
  // Type-specific identifier the backend requires (plate/vessel/container).
  const [identifier, setIdentifier] = useState("");
  const [address, setAddress] = useState("");
  const [lat, setLat] = useState("0");
  const [lon, setLon] = useState("0");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  // Which type-specific identifier is required for the selected type.
  const identifierField =
    assetType === "vehicle"
      ? { key: "plate_number" as const, label: "Plate number" }
      : assetType === "vessel"
        ? { key: "vessel_name" as const, label: "Vessel name" }
        : assetType === "container"
          ? { key: "container_number" as const, label: "Container number" }
          : null; // equipment needs no extra identifier

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const id = assetId.trim();
    if (!id || !name.trim()) {
      setError("Asset ID and name are required.");
      return;
    }
    if (identifierField && !identifier.trim()) {
      setError(
        `${identifierField.label} is required for ${labelize(assetType)} assets.`,
      );
      return;
    }
    const latNum = Number(lat);
    const lonNum = Number(lon);
    if (!Number.isFinite(latNum) || !Number.isFinite(lonNum)) {
      setError("Latitude and longitude must be numbers.");
      return;
    }
    const trimmedAddress = address.trim() || "Unspecified";
    const payload: CreateAssetPayload = {
      asset_id: id,
      asset_type: assetType,
      asset_subtype: assetSubtype,
      name: name.trim(),
      status: "active",
      current_location: {
        id: `loc-${id}`,
        name: trimmedAddress,
        type: "site",
        coordinates: { lat: latNum, lon: lonNum },
        address: trimmedAddress,
      },
      ...(identifierField ? { [identifierField.key]: identifier.trim() } : {}),
    };
    setSubmitting(true);
    setError("");
    try {
      await apiService.createAsset(payload);
      onCreated();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to create asset.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Add asset"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4">
          <h2 className="text-lg font-semibold text-primary">Add asset</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-gray-500 hover:text-gray-700"
            aria-label="Close"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label
                htmlFor="asset-id"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Asset ID
              </label>
              <input
                id="asset-id"
                type="text"
                value={assetId}
                onChange={(e) => setAssetId(e.target.value)}
                placeholder="TNK-001"
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
                required
              />
            </div>
            <div>
              <label
                htmlFor="asset-name"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Name
              </label>
              <input
                id="asset-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Tanker 1"
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
                required
              />
            </div>
            <div>
              <label
                htmlFor="asset-type"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Type
              </label>
              <select
                id="asset-type"
                value={assetType}
                onChange={(e) => {
                  const t = e.target.value as AssetType;
                  setAssetType(t);
                  setAssetSubtype(SUBTYPE_OPTIONS[t][0]);
                  setIdentifier("");
                }}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
              >
                {(Object.keys(SUBTYPE_OPTIONS) as AssetType[]).map((t) => (
                  <option key={t} value={t}>
                    {labelize(t)}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label
                htmlFor="asset-subtype"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Subtype
              </label>
              <select
                id="asset-subtype"
                value={assetSubtype}
                onChange={(e) =>
                  setAssetSubtype(e.target.value as AssetSubtype)
                }
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
              >
                {SUBTYPE_OPTIONS[assetType].map((s) => (
                  <option key={s} value={s}>
                    {labelize(s)}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {identifierField && (
            <div>
              <label
                htmlFor="asset-identifier"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                {identifierField.label}
              </label>
              <input
                id="asset-identifier"
                type="text"
                value={identifier}
                onChange={(e) => setIdentifier(e.target.value)}
                placeholder={
                  assetType === "vehicle" ? "ABC-1234" : identifierField.label
                }
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
                required
              />
            </div>
          )}
          <div>
            <label
              htmlFor="asset-address"
              className="block text-xs font-medium text-gray-600 mb-1"
            >
              Location / address
            </label>
            <input
              id="asset-address"
              type="text"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder="Main depot"
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label
                htmlFor="asset-lat"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Latitude
              </label>
              <input
                id="asset-lat"
                type="number"
                step="any"
                value={lat}
                onChange={(e) => setLat(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
              />
            </div>
            <div>
              <label
                htmlFor="asset-lon"
                className="block text-xs font-medium text-gray-600 mb-1"
              >
                Longitude
              </label>
              <input
                id="asset-lon"
                type="number"
                step="any"
                value={lon}
                onChange={(e) => setLon(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
              />
            </div>
          </div>
          {error && (
            <p
              role="alert"
              className="text-sm text-error bg-error-light px-3 py-2 rounded-lg"
            >
              {error}
            </p>
          )}
          <div className="flex items-center justify-end gap-2 border-t border-gray-100 pt-4">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 rounded-lg hover:bg-gray-50 border border-gray-200"
            >
              Cancel
            </button>
            <Button type="submit" disabled={submitting}>
              {submitting ? "Creating…" : "Create asset"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

interface FleetTrackingProps {
  onTruckSelect?: (truck: Truck) => void;
  /**
   * Asset id deep-linked via `?asset=` on /dashboard/fleet (the canonical
   * destination produced by `entityHref("asset", id)`). When it matches a
   * loaded asset the row is selected, the map is focused, and the default
   * filters/pagination are adjusted so the row is actually visible. When it
   * matches nothing a dismissible notice is shown rather than silently doing
   * nothing.
   */
  focusAssetId?: string | null;
}

export default function FleetTracking({
  onTruckSelect,
  focusAssetId,
}: FleetTrackingProps) {
  const [trucks, setTrucks] = useState<Truck[]>([]);
  const [fleetSummary, setFleetSummary] = useState<AssetSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [showInTransit, setShowInTransit] = useState(true);
  const [selectedTruck, setSelectedTruck] = useState<string | null>(null);
  // Truck whose compartments are shown in the slide-over (click-through from
  // the row), replacing the former top-level "Compartments" tab.
  const [compartmentsTruck, setCompartmentsTruck] = useState<Truck | null>(
    null,
  );
  const compartmentsPanelRef = useRef<HTMLDivElement>(null);
  const closeCompartments = useCallback(() => setCompartmentsTruck(null), []);
  useDialogA11y(
    compartmentsTruck !== null,
    compartmentsPanelRef,
    closeCompartments,
  );
  const [assetTypeFilter, setAssetTypeFilter] = useState<AssetType | "all">(
    "all",
  );
  const [page, setPage] = useState(1);
  const [showAddAsset, setShowAddAsset] = useState(false);
  /**
   * Deep-link focus bookkeeping (`?asset=<id>`): the id whose focus has already
   * been applied, and the id we could not find in the loaded set.
   */
  const appliedFocusRef = useRef<string | null>(null);
  const [focusNotFound, setFocusNotFound] = useState<string | null>(null);

  /**
   * Per-asset compliance signal keyed by asset id, lazily fetched for the
   * visible page so the assignment surface can flag a non-compliant asset
   * (Req 11.2). `undefined` while loading / "unknown" when the asset has no
   * compliance records (rendered as an "unlinked" chip).
   */
  const [compliance, setCompliance] = useState<
    Record<string, AssetComplianceSummary["overall_status"]>
  >({});

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

  /**
   * Deep-link focus (`/dashboard/fleet?asset=<id>`). Applied exactly once per
   * `focusAssetId` value: the ref guard keeps re-renders, websocket location
   * updates and the operator's own subsequent row clicks from being hijacked.
   * Because the default filters ("In transit only") and pagination can hide the
   * referenced row, the filter is relaxed and the page moved so the selection
   * is visible; an id that matches nothing raises a dismissible notice instead
   * of silently doing nothing.
   */
  useEffect(() => {
    if (!focusAssetId) {
      setFocusNotFound(null);
      return;
    }
    // Wait for the first load to settle before deciding "not found".
    if (loading) return;
    if (appliedFocusRef.current === focusAssetId) return;

    appliedFocusRef.current = focusAssetId;

    const truck = trucks.find((candidate) => candidate.id === focusAssetId);
    if (!truck) {
      setFocusNotFound(focusAssetId);
      return;
    }
    setFocusNotFound(null);

    const inTransit = IN_TRANSIT_STATUSES.includes(truck.status);
    if (!inTransit) setShowInTransit(false);

    // The list the row will live in once the filter above is applied.
    const visible =
      showInTransit && inTransit
        ? trucks.filter((candidate) =>
            IN_TRANSIT_STATUSES.includes(candidate.status),
          )
        : trucks;
    const index = visible.findIndex(
      (candidate) => candidate.id === focusAssetId,
    );
    setPage(index >= 0 ? Math.floor(index / PAGE_SIZE) + 1 : 1);

    setSelectedTruck(truck.id);
    onTruckSelect?.(truck);
  }, [focusAssetId, loading, trucks, showInTransit, onTruckSelect]);

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
    ? trucks.filter((truck) => IN_TRANSIT_STATUSES.includes(truck.status))
    : trucks;

  const totalPages = Math.max(1, Math.ceil(filteredTrucks.length / PAGE_SIZE));
  const paginatedTrucks = filteredTrucks.slice(
    (page - 1) * PAGE_SIZE,
    page * PAGE_SIZE,
  );

  // Lazily correlate compliance status for the visible assets (Req 11.2).
  // Keyed on the visible ids so it refetches when the page/filter changes;
  // failures degrade gracefully to "unknown" (rendered as an unlinked chip).
  const visibleIds = paginatedTrucks.map((t) => t.id).join(",");
  useEffect(() => {
    const ids = visibleIds ? visibleIds.split(",") : [];
    if (ids.length === 0) return;
    let cancelled = false;

    (async () => {
      const entries = await Promise.all(
        ids.map(async (id) => {
          try {
            const summary = await apiService.getAssetCompliance(id);
            return [id, summary.overall_status] as const;
          } catch {
            return [id, "unknown"] as const;
          }
        }),
      );
      if (cancelled) return;
      setCompliance((prev) => {
        const next = { ...prev };
        for (const [id, status] of entries) next[id] = status;
        return next;
      });
    })();

    return () => {
      cancelled = true;
    };
  }, [visibleIds]);

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
          <span className="text-gray-500 ml-1">
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
      key: "compliance",
      label: "Compliance",
      render: (truck) => {
        const status = compliance[truck.id];
        if (!status || status === "unknown") {
          return (
            <span
              className="text-xs text-gray-500"
              title="No compliance records"
            >
              Unlinked
            </span>
          );
        }
        const chip = COMPLIANCE_CHIP[status];
        return (
          <Badge variant={chip.variant} size="sm">
            {chip.label}
          </Badge>
        );
      },
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
    {
      key: "compartments",
      label: "",
      align: "right",
      render: (truck) =>
        (truck.assetType ?? "vehicle") === "vehicle" ? (
          <button
            type="button"
            onClick={(e) => {
              // Don't trigger the row's map-selection click.
              e.stopPropagation();
              setCompartmentsTruck(truck);
            }}
            className="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50"
            aria-label={`View compartments for ${truck.plateNumber || truck.name}`}
          >
            <Boxes className="h-3 w-3" />
            Compartments
          </button>
        ) : null,
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
          <div className="flex items-center gap-2">
            <Button
              type="button"
              size="sm"
              onClick={() => setShowAddAsset(true)}
              icon={<Plus className="w-4 h-4" />}
            >
              Add asset
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={loadFleetData}
              icon={<RefreshCw className="w-4 h-4" />}
              title="Refresh"
              aria-label="Refresh fleet data"
            />
          </div>
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

      {/* Deep-link (`?asset=`) that matched no loaded asset — say so rather
          than leaving the operator on an unchanged table. */}
      {focusNotFound && (
        <div
          role="status"
          data-testid="focus-asset-not-found"
          className="mx-4 mb-2 flex items-start justify-between gap-3 rounded-lg border border-warning-light bg-warning-light px-3 py-2 text-xs text-warning-dark"
        >
          <span>
            Asset <span className="font-medium">{focusNotFound}</span> was not
            found in this view. It may be a different asset type — try the Asset
            type filter — or it may no longer be tracked.
          </span>
          <button
            type="button"
            onClick={() => setFocusNotFound(null)}
            className="rounded p-0.5 text-warning-dark hover:text-gray-700"
            aria-label="Dismiss asset not found notice"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
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

      {/* Compartments slide-over — reached by clicking a truck's Compartments
          button (replaces the former Fuel Ops > Compartments tab). */}
      {compartmentsTruck && (
        <div
          className="fixed inset-0 z-50 flex justify-end bg-black/30"
          role="dialog"
          aria-modal="true"
          aria-label="Truck compartments"
          onClick={() => setCompartmentsTruck(null)}
        >
          <div
            ref={compartmentsPanelRef}
            tabIndex={-1}
            className="h-full w-full max-w-3xl overflow-y-auto bg-white shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4">
              <div>
                <h2 className="text-lg font-semibold text-primary">
                  Compartments
                </h2>
                <p className="text-xs text-gray-500">
                  {compartmentsTruck.plateNumber || compartmentsTruck.name} ·{" "}
                  {compartmentsTruck.id}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setCompartmentsTruck(null)}
                className="rounded p-1 text-gray-500 hover:text-gray-600"
                aria-label="Close compartments"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="p-6">
              <Suspense fallback={<LoadingSpinner message="Loading…" />}>
                <TruckCompartmentsPage
                  truckId={compartmentsTruck.id}
                  embedded
                />
              </Suspense>
            </div>
          </div>
        </div>
      )}

      {showAddAsset && (
        <AddAssetModal
          onClose={() => setShowAddAsset(false)}
          onCreated={() => {
            setShowAddAsset(false);
            void loadFleetData();
          }}
        />
      )}
    </div>
  );
}
