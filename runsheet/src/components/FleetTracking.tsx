"use client";

/**
 * Fleet → Trucks tracking table (UI revamp task 3.1): one toolbar (search,
 * status chips with counts, a Filters popover for asset type), a DataTable
 * with status and compliance badges, Compartments in a drawer from the row
 * menu, and "Add asset" in Fleet's title row (FormDialog). The default view
 * shows every asset ("All"); the old "In transit only" default hid parked
 * and loading trucks.
 */
import { Boxes, Plus, RefreshCw, X } from "lucide-react";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { type LocationUpdateData, useFleetWebSocket } from "../hooks";
import { humanize, number, relative } from "../lib/format";
import type { AssetComplianceSummary } from "../services/api";
import { apiService } from "../services/api";
import type { StatusKey } from "../styles/tokens";
import type {
  AssetSubtype,
  AssetSummary,
  AssetType,
  Truck,
} from "../types/api";
import AddAssetDialog, { subtypeLabel } from "./fleet/AddAssetDialog";
import LoadingSpinner from "./LoadingSpinner";
import {
  Button,
  type Column,
  DataTable,
  Drawer,
  Field,
  FilterChips,
  FilterPopover,
  IconButton,
  Select,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "./ui";
import { WebSocketStatusBadge } from "./WebSocketStatus";

// Compartments are a property of a truck, reached by clicking the asset rather
// than living as a separate top-level tab. Lazy-loaded into a slide-over.
const TruckCompartmentsPage = lazy(() => import("./ops/TruckCompartmentsPage"));

/** Rows per page in the tracking table. */
const PAGE_SIZE = 20;

/** Truck status → badge style and label (icon + text, never colour alone). */
export const TRUCK_STATUS: Record<
  string,
  { status: StatusKey; label: string }
> = {
  on_time: { status: "ok", label: "On time" },
  delayed: { status: "delayed", label: "Delayed" },
  stopped: { status: "warning", label: "Stopped" },
  loading: { status: "dispatched", label: "Loading" },
  unloading: { status: "in_transit", label: "Unloading" },
  maintenance: { status: "draft", label: "Maintenance" },
};
const STATUS_ORDER = Object.keys(TRUCK_STATUS);

/** Filter options for the asset type dropdown */
const ASSET_TYPE_OPTIONS: { label: string; value: AssetType | "all" }[] = [
  { label: "All types", value: "all" },
  { label: "Vehicles", value: "vehicle" },
  { label: "Vessels", value: "vessel" },
  { label: "Equipment", value: "equipment" },
  { label: "Containers", value: "container" },
];

/**
 * Chip styling for the per-asset compliance signal sourced from
 * GET /api/fleet/assets/{asset_id}/compliance (Req 11.2). `valid`/`expiring`/
 * `expired` mirror the Drivers qualification chip; `unknown` (no records) and a
 * missing entry render as an "unlinked" affordance.
 */
const COMPLIANCE_CHIP: Record<
  Exclude<AssetComplianceSummary["overall_status"], "unknown">,
  { status: StatusKey; label: string }
> = {
  expired: { status: "critical", label: "Expired" },
  expiring: { status: "warning", label: "Expiring" },
  valid: { status: "ok", label: "Valid" },
};

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
  /** Status chip ("all" or a truck status); the default is every asset. */
  const [statusFilter, setStatusFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [selectedTruck, setSelectedTruck] = useState<string | null>(null);
  // Truck whose compartments are shown in the slide-over (click-through from
  // the row), replacing the former top-level "Compartments" tab.
  const [compartmentsTruck, setCompartmentsTruck] = useState<Truck | null>(
    null,
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
      setLastUpdated(new Date());
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

    // Clear filters that would hide the row, then page to it.
    setStatusFilter("all");
    setSearch("");
    const index = trucks.findIndex(
      (candidate) => candidate.id === focusAssetId,
    );
    setPage(index >= 0 ? Math.floor(index / PAGE_SIZE) + 1 : 1);

    setSelectedTruck(truck.id);
    onTruckSelect?.(truck);
  }, [focusAssetId, loading, trucks, onTruckSelect]);

  const handleTruckClick = (truck: Truck) => {
    setSelectedTruck(truck.id);
    onTruckSelect?.(truck);
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

  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = { all: trucks.length };
    for (const t of trucks) counts[t.status] = (counts[t.status] ?? 0) + 1;
    return counts;
  }, [trucks]);
  const q = search.trim().toLowerCase();
  const filteredTrucks = trucks.filter(
    (t) =>
      (statusFilter === "all" || t.status === statusFilter) &&
      (!q ||
        [t.plateNumber, t.name, t.id, t.driverName]
          .filter(Boolean)
          .some((v) => String(v).toLowerCase().includes(q))),
  );

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
      header: "Asset",
      truncate: true,
      title: (t) => t.plateNumber || t.name,
      className: "font-medium text-text",
      cell: (t) => t.plateNumber || t.name,
    },
    {
      key: "type",
      header: "Type",
      truncate: true,
      title: (t) =>
        `${humanize(t.assetType ?? "vehicle")} · ${subtypeLabel(t.assetSubtype ?? "truck")}`,
      className: "text-slate-700",
      cell: (t) => subtypeLabel(t.assetSubtype ?? "truck"),
    },
    {
      key: "status",
      header: "Status",
      width: 120,
      cell: (t) => {
        const cfg = TRUCK_STATUS[t.status];
        return cfg ? (
          <StatusBadge status={cfg.status} label={cfg.label} />
        ) : (
          <span className="text-xs text-text-muted">{humanize(t.status)}</span>
        );
      },
    },
    {
      key: "compliance",
      header: "Compliance",
      width: 110,
      cell: (t) => {
        const status = compliance[t.id];
        if (!status || status === "unknown") {
          return (
            <span
              className="text-xs text-text-muted"
              title="No compliance records"
            >
              Unlinked
            </span>
          );
        }
        const chip = COMPLIANCE_CHIP[status];
        return <StatusBadge status={chip.status} label={chip.label} />;
      },
    },
    {
      key: "destination",
      header: "Destination",
      truncate: true,
      title: (t) =>
        t.route?.origin?.name && t.route?.destination?.name
          ? `${t.route.origin.name} → ${t.route.destination.name}`
          : (t.destination?.name ?? undefined),
      className: "text-slate-700",
      cell: (t) => t.destination?.name ?? "—",
    },
    {
      key: "eta",
      header: "ETA",
      width: 90,
      align: "right",
      className: "tabular-nums text-slate-700 whitespace-nowrap",
      cell: (t) =>
        t.estimatedArrival ? calculateTimeToArrival(t.estimatedArrival) : "—",
    },
  ];

  const actions = useMemo(
    () => (
      <Button
        type="button"
        size="sm"
        onClick={() => setShowAddAsset(true)}
        icon={<Plus className="h-3.5 w-3.5" />}
      >
        Add asset
      </Button>
    ),
    [],
  );
  const counts = useMemo(
    () =>
      fleetSummary ? (
        <span className="whitespace-nowrap text-xs text-text-muted">
          {number(fleetSummary.totalTrucks)} assets ·{" "}
          {number(fleetSummary.onTimeTrucks)} on time ·{" "}
          {number(fleetSummary.delayedTrucks)} delayed
        </span>
      ) : null,
    [fleetSummary],
  );
  // Inside Fleet the hub title row shows them; standalone, the toolbar does.
  const embedded = usePageChrome({ actions, counts });

  const statusOptions = [
    { id: "all", label: "All", count: statusCounts.all },
    ...STATUS_ORDER.filter((id) => statusCounts[id]).map((id) => ({
      id,
      label: TRUCK_STATUS[id].label,
      count: statusCounts[id],
      status: TRUCK_STATUS[id].status,
    })),
  ];

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-4">
        <div className="max-w-md text-center">
          <p className="mb-2 font-medium text-red-700">Connection error</p>
          <p className="mb-4 text-sm text-text-muted">{error}</p>
          <Button onClick={loadFleetData}>Retry</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <Toolbar
        label="Trucks"
        search={
          <input
            type="search"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            placeholder="Search plate, name or driver"
            aria-label="Search assets"
            className="h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          />
        }
        filters={
          <>
            <FilterChips
              label="Asset status"
              collapse
              options={loading ? [{ id: "all", label: "All" }] : statusOptions}
              value={statusFilter}
              onChange={(v) => {
                setStatusFilter(v as string);
                setPage(1);
              }}
            />
            <FilterPopover
              count={assetTypeFilter === "all" ? 0 : 1}
              label="Asset filters"
              onClear={() => {
                setAssetTypeFilter("all");
                setPage(1);
              }}
            >
              <Field label="Asset type" id="fleet-asset-type">
                <Select
                  id="fleet-asset-type"
                  value={assetTypeFilter}
                  onChange={(v) => {
                    setAssetTypeFilter(v as AssetType | "all");
                    setPage(1);
                  }}
                  options={ASSET_TYPE_OPTIONS}
                />
              </Field>
            </FilterPopover>
          </>
        }
        end={
          <>
            <WebSocketStatusBadge
              state={wsState}
              reconnectAttempt={reconnectAttempt}
            />
            {lastUpdated && (
              <span className="whitespace-nowrap text-xs text-text-muted">
                Updated {relative(lastUpdated)}
              </span>
            )}
            <IconButton
              label="Refresh fleet data"
              size="sm"
              onClick={loadFleetData}
              icon={
                <RefreshCw
                  className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
                />
              }
            />
            {!embedded && actions}
          </>
        }
      />

      {/* Deep-link (`?asset=`) that matched no loaded asset — say so rather
          than leaving the operator on an unchanged table. */}
      {focusNotFound && (
        <div
          role="status"
          data-testid="focus-asset-not-found"
          className="flex items-start justify-between gap-3 border-b border-amber-300 bg-amber-50 px-4 py-2 text-xs text-amber-900"
        >
          <span>
            Asset <span className="font-medium">{focusNotFound}</span> was not
            found in this view. It may be a different asset type (try the asset
            type filter) or it may no longer be tracked.
          </span>
          <button
            type="button"
            onClick={() => setFocusNotFound(null)}
            className="rounded p-0.5 text-amber-900 hover:bg-amber-100"
            aria-label="Dismiss asset not found notice"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<Truck>
          ariaLabel="Fleet assets"
          rowHeight="compact"
          columns={fleetColumns}
          data={loading ? [] : paginatedTrucks}
          loading={loading}
          getRowId={(t) => t.id}
          selectedId={selectedTruck ?? undefined}
          onRowClick={handleTruckClick}
          rowLabel={(t) => t.plateNumber || t.name}
          rowMenu={(t) =>
            (t.assetType ?? "vehicle") === "vehicle"
              ? [
                  {
                    id: "compartments",
                    label: "Compartments",
                    icon: <Boxes className="h-3.5 w-3.5" />,
                    onSelect: () => setCompartmentsTruck(t),
                  },
                ]
              : []
          }
          pagination={
            totalPages > 1
              ? {
                  page,
                  totalPages,
                  totalItems: filteredTrucks.length,
                  onPageChange: setPage,
                }
              : undefined
          }
          emptyState={
            <div className="text-text-muted">
              <p className="text-sm font-medium">No assets found</p>
              <p className="mt-1 text-xs">Try adjusting your filters</p>
            </div>
          }
        />
      </div>

      <Drawer
        open={compartmentsTruck !== null}
        onClose={() => setCompartmentsTruck(null)}
        title={
          compartmentsTruck
            ? `Compartments · ${compartmentsTruck.plateNumber || compartmentsTruck.name}`
            : "Compartments"
        }
        width={760}
      >
        {compartmentsTruck && (
          <Suspense fallback={<LoadingSpinner message="Loading…" />}>
            <TruckCompartmentsPage truckId={compartmentsTruck.id} embedded />
          </Suspense>
        )}
      </Drawer>

      {showAddAsset && (
        <AddAssetDialog
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
