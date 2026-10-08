"use client";

import { AlertTriangle, Plus, RefreshCw } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOpsWebSocket } from "../../hooks/useOpsWebSocket";
import { gallons, number, pct, relative } from "../../lib/format";
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
  getNetworkCapacityGallons,
  getNetworkCurrentStockGallons,
  getNetworkSummary,
  getStation,
  getStations,
} from "../../services/fuelApi";
import FuelConsumptionChart from "../ops/FuelConsumptionChart";
import FuelStationDetail from "../ops/FuelStationDetail";
import FuelStationForm, { STATION_FUEL_TYPES } from "../ops/FuelStationForm";
import FuelStationList, { STATION_STATUS } from "../ops/FuelStationList";
import {
  Button,
  Drawer,
  Field,
  FilterChips,
  FilterPopover,
  IconButton,
  PageTitle,
  ProductSelect,
  Skeleton,
  Toolbar,
  usePageChrome,
} from "../ui";

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

// Fallback poll so stock levels recover if the ops WebSocket drops or misses
// a push: a monitoring screen can't silently go stale.
const REFRESH_INTERVAL_MS = 60_000;

const STATUS_CHIPS: { id: "" | StationStatus; label: string }[] = [
  { id: "", label: "All" },
  { id: "critical", label: "Critical" },
  { id: "low", label: "Low" },
  { id: "empty", label: "Empty" },
  { id: "normal", label: "Normal" },
];

interface FuelDashboardPageProps {
  /**
   * Inside the Fuel hub the hub owns the title row and tabs; this view only
   * contributes its counts and Add station action (`usePageChrome`). The
   * prop is kept for callers; the view is always rendered inside the hub.
   */
  embedded?: boolean;
  /** Which panel to show. */
  view?: "stations" | "efficiency";
}

/**
 * Fuel → Stations and Consumption.
 *
 * Network totals sit in the hub's title row (stock, alerts, days left) and
 * station status counts are the filter chips (the old 4-card summary bar is
 * gone, R4.4). One toolbar: location search, status chips, a Filters popover
 * (product), refresh. The station detail opens in a side drawer; the list
 * honours `?station=<id>` (Dashboard low-tank rows link here). Each data
 * source fails open independently, with a fallback poll + manual refresh.
 *
 * Validates: Requirements 6.1-6.7, 8.1, 8.3
 */
export default function FuelDashboardView({
  view = "stations",
}: FuelDashboardPageProps = {}) {
  const searchParams = useSearchParams();
  const deepLinkStation = searchParams?.get("station") ?? null;
  const [stations, setStations] = useState<FuelStation[]>([]);
  const [summary, setSummary] = useState<FuelNetworkSummary>(EMPTY_SUMMARY);
  const [consumptionData, setConsumptionData] = useState<ConsumptionMetric[]>(
    [],
  );
  const [selectedStationId, setSelectedStationId] = useState<string | null>(
    deepLinkStation,
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

  // Station form state
  const [form, setForm] = useState<
    { mode: "create" } | { mode: "edit"; station: FuelStation } | null
  >(null);

  const loadData = useCallback(async () => {
    setRefreshing(true);
    const filters: StationFilters = {};
    if (fuelTypeFilter) filters.fuel_type = fuelTypeFilter;
    if (statusFilter) filters.status = statusFilter;
    if (locationFilter) filters.location = locationFilter;

    // Each source fails open independently: one bad endpoint must not blank
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

  // `?station=` deep link: open that station's detail once.
  const deepLinked = useRef(false);
  useEffect(() => {
    if (deepLinked.current || !deepLinkStation) return;
    deepLinked.current = true;
    loadStationDetail(deepLinkStation);
  }, [deepLinkStation, loadStationDetail]);

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

  const handleStationFormSuccess = useCallback(
    (savedStation: FuelStation) => {
      if (form?.mode === "create") {
        setStations((prev) => [savedStation, ...prev]);
      } else {
        setStations((prev) =>
          prev.map((s) =>
            s.station_id === savedStation.station_id ? savedStation : s,
          ),
        );
        if (selectedStationId === savedStation.station_id) {
          setStationDetail((prev) =>
            prev ? { ...prev, station: savedStation } : prev,
          );
        }
      }
    },
    [form, selectedStationId],
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

  // Title-row contributions: network totals and the create action.
  const capacity = getNetworkCapacityGallons(summary);
  const stock = getNetworkCurrentStockGallons(summary);
  const counts = useMemo(
    () =>
      loading ? null : (
        <span className="inline-flex items-center gap-3 whitespace-nowrap">
          <span>
            Stock{" "}
            <b className="font-semibold text-text tabular-nums">
              {capacity > 0 ? pct((stock / capacity) * 100) : "—"}
            </b>{" "}
            of {gallons(capacity)}
          </span>
          <span>
            <b className="font-semibold text-text tabular-nums">
              {summary.active_alerts}
            </b>{" "}
            {summary.active_alerts === 1 ? "alert" : "alerts"}
          </span>
          {summary.average_days_until_empty > 0 && (
            <span>
              Avg{" "}
              <b className="font-semibold text-text tabular-nums">
                {number(summary.average_days_until_empty, { decimals: 1 })}
              </b>{" "}
              days left
            </span>
          )}
        </span>
      ),
    [loading, capacity, stock, summary],
  );
  const actions = useMemo(
    () => (
      <Button
        type="button"
        size="sm"
        onClick={() => setForm({ mode: "create" })}
        icon={<Plus className="h-3.5 w-3.5" aria-hidden="true" />}
      >
        Add station
      </Button>
    ),
    [],
  );
  const embedded = usePageChrome({ counts, actions });

  const statusCount: Record<string, number> = {
    "": summary.total_stations,
    normal: summary.stations_normal,
    low: summary.stations_low,
    critical: summary.stations_critical,
    empty: summary.stations_empty,
  };

  const detailPanel = detailLoading ? (
    <Skeleton rows={6} />
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
    <p className="py-8 text-center text-sm text-text-muted">
      Couldn't load the station detail.
    </p>
  );

  const toolbar = (
    <Toolbar
      label="Stations"
      search={
        <input
          type="search"
          value={locationFilter}
          onChange={(e) => setLocationFilter(e.target.value)}
          placeholder="Search location"
          aria-label="Filter by location"
          className="h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        />
      }
      filters={
        <>
          <FilterChips
            label="Station status"
            options={STATUS_CHIPS.map((c) => ({
              id: c.id || "all",
              label: c.label,
              count: loading ? undefined : statusCount[c.id],
              status: c.id ? STATION_STATUS[c.id].status : undefined,
            }))}
            value={statusFilter || "all"}
            onChange={(v) =>
              setStatusFilter(v === "all" ? "" : (v as StationStatus))
            }
            collapse
          />
          <FilterPopover
            count={fuelTypeFilter ? 1 : 0}
            label="Station filters"
            onClear={() => setFuelTypeFilter("")}
          >
            <Field label="Product" id="fuel-filter-product">
              <ProductSelect
                id="fuel-filter-product"
                value={fuelTypeFilter || null}
                options={STATION_FUEL_TYPES}
                placeholder="All products"
                onChange={(code) => setFuelTypeFilter(code as FuelType)}
              />
            </Field>
          </FilterPopover>
        </>
      }
      end={
        <>
          {loadError && (
            <span
              role="alert"
              className="inline-flex items-center gap-1 whitespace-nowrap rounded-full border border-amber-300 bg-amber-100 px-2 text-xs font-semibold text-amber-800"
            >
              <AlertTriangle aria-hidden="true" className="h-3 w-3" />
              Some data didn't load
            </span>
          )}
          {lastUpdated && (
            <span className="whitespace-nowrap text-xs text-text-muted">
              Updated {relative(lastUpdated)}
            </span>
          )}
          <IconButton
            label="Refresh"
            size="sm"
            onClick={() => loadData()}
            icon={
              <RefreshCw
                className={`h-3.5 w-3.5 ${refreshing ? "animate-spin" : ""}`}
              />
            }
          />
        </>
      }
    />
  );

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Fuel stations
          </PageTitle>
          <div className="ml-auto">{actions}</div>
        </div>
      )}
      {view === "efficiency" ? (
        <div className="flex-1 overflow-y-auto px-4 py-4">
          <h2 className="mb-3 text-sm font-semibold text-text">
            Daily consumption trend
          </h2>
          {loading ? (
            <Skeleton rows={6} />
          ) : (
            <FuelConsumptionChart data={consumptionData} />
          )}
        </div>
      ) : (
        <>
          {toolbar}
          <div className="min-h-0 flex-1 overflow-auto">
            <FuelStationList
              stations={loading ? [] : stations}
              loading={loading}
              onSelectStation={handleSelectStation}
              selectedStationId={selectedStationId}
              onEditStation={(station) => setForm({ mode: "edit", station })}
            />
          </div>
        </>
      )}

      <Drawer
        open={selectedStationId !== null && view !== "efficiency"}
        onClose={handleCloseDetail}
        title="Station detail"
        width={480}
      >
        {detailPanel}
      </Drawer>

      {form && (
        <FuelStationForm
          mode={form.mode}
          station={form.mode === "edit" ? form.station : null}
          onClose={() => setForm(null)}
          onSuccess={handleStationFormSuccess}
        />
      )}
    </div>
  );
}
