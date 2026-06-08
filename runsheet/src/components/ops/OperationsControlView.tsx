"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type {
  DelayAlertEvent,
  JobCreatedEvent,
  StatusChangedEvent,
} from "../../hooks/useSchedulingWebSocket";
import { useSchedulingWebSocket } from "../../hooks/useSchedulingWebSocket";
import { apiService } from "../../services/api";
import type { FuelAlert } from "../../services/fuelApi";
import { getAlerts as getFuelAlerts } from "../../services/fuelApi";
import { getAlerts as getInventoryAlerts } from "../../services/inventoryApi";
import { getActiveJobs, getDelayedJobs } from "../../services/schedulingApi";
import type { Job, JobStatus, OperationsControlSummary } from "../../types/api";
import LoadingSpinner from "../LoadingSpinner";
import { Button } from "../ui";
import AgentActivityFeed from "./AgentActivityFeed";
import AgentAutonomyBanner from "./AgentAutonomyBanner";
import AgentHealth from "./AgentHealth";
import ApprovalQueuePanel from "./ApprovalQueuePanel";
import DelayedOperationsPanel from "./DelayedOperationsPanel";
import FuelStatusSidebar from "./FuelStatusSidebar";
import InventoryHealthBadge from "./InventoryHealthBadge";
import JobQueuePanel from "./JobQueuePanel";
import OperationsMap from "./OperationsMap";
import OperationsSummaryBar from "./OperationsSummaryBar";
import StormModeBanner from "./StormModeBanner";

// Fallback poll so the command center recovers if the WebSocket drops or a
// server-side push is missed — a monitoring surface can't silently go stale.
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

interface AssetLocation {
  asset_id: string;
  name: string;
  lat: number;
  lng: number;
  job_status?: JobStatus;
  job_id?: string;
}

/**
 * Command center layout composing all operations control panels.
 * Subscribes to scheduling + fuel WebSocket events for real-time updates,
 * with a slow fallback poll and a manual refresh so the view can always
 * re-sync.
 *
 * Layout (responsive): map + right rail stack on small screens and sit
 * side-by-side (≈60/40) from `lg` up.
 *
 * Validates: Requirements 10.1-10.7
 */
export default function OperationsControlView() {
  const [activeJobs, setActiveJobs] = useState<Job[]>([]);
  const [delayedJobs, setDelayedJobs] = useState<Job[]>([]);
  const [fuelAlerts, setFuelAlerts] = useState<FuelAlert[]>([]);
  const [assetLocations, setAssetLocations] = useState<AssetLocation[]>([]);
  const [inventoryAlertCount, setInventoryAlertCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [loadError, setLoadError] = useState(false);

  /** Build summary from current state */
  const summary: OperationsControlSummary = {
    active_jobs: activeJobs.length,
    delayed_jobs: delayedJobs.length,
    available_assets: assetLocations.filter((a) => !a.job_id).length,
    fuel_alerts: fuelAlerts.length,
  };

  /**
   * Load all data sources independently — each request handles its own errors.
   * `background` refreshes (poll) update in place without the full-screen
   * spinner so they don't blank the command center.
   */
  const loadData = useCallback(async (opts?: { background?: boolean }) => {
    if (!opts?.background) setRefreshing(true);
    try {
      const results = await Promise.allSettled([
        getActiveJobs(),
        getDelayedJobs(),
        getFuelAlerts(),
        apiService.getAssets(),
        getInventoryAlerts(),
      ]);
      const [activeRes, delayedRes, fuelRes, assetsRes, inventoryRes] = results;

      setActiveJobs(
        activeRes.status === "fulfilled" ? activeRes.value.data : [],
      );
      setDelayedJobs(
        delayedRes.status === "fulfilled" ? delayedRes.value.data : [],
      );
      setFuelAlerts(fuelRes.status === "fulfilled" ? fuelRes.value.data : []);

      if (inventoryRes.status === "fulfilled") {
        setInventoryAlertCount(
          inventoryRes.value.count ?? inventoryRes.value.data?.length ?? 0,
        );
      } else {
        setInventoryAlertCount(0);
      }

      const assets =
        assetsRes.status === "fulfilled" ? assetsRes.value.data : [];

      const jobsByAsset = new Map<string, Job>();
      for (const job of activeRes.status === "fulfilled"
        ? activeRes.value.data
        : []) {
        if (job.asset_assigned) {
          jobsByAsset.set(job.asset_assigned, job);
        }
      }

      const locations: AssetLocation[] = assets
        .filter((a: any) => {
          const lat = a.currentLocation?.coordinates?.lat;
          const lng = a.currentLocation?.coordinates?.lon;
          return (
            typeof lat === "number" &&
            typeof lng === "number" &&
            !Number.isNaN(lat) &&
            !Number.isNaN(lng)
          );
        })
        .map((a: any) => {
          const assignedJob = jobsByAsset.get(a.id);
          return {
            asset_id: a.id,
            name:
              a.name ||
              a.plateNumber ||
              a.vesselName ||
              a.containerNumber ||
              a.id,
            lat: a.currentLocation.coordinates.lat,
            lng: a.currentLocation.coordinates.lon,
            job_status: assignedJob?.status,
            job_id: assignedJob?.job_id,
          };
        });
      setAssetLocations(locations);

      // Surface a non-blocking warning if any source failed (each still
      // fails open to empty, but the user should know data may be partial).
      setLoadError(results.some((r) => r.status === "rejected"));
    } catch (error) {
      console.error("Failed to load operations data:", error);
      setLoadError(true);
    } finally {
      setLastUpdated(new Date());
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadData();
    const id = setInterval(
      () => loadData({ background: true }),
      REFRESH_INTERVAL_MS,
    );
    return () => clearInterval(id);
  }, [loadData]);

  /**
   * WebSocket handlers for real-time updates.
   * Validates: Requirement 10.7
   */
  const handleJobCreated = useCallback((event: JobCreatedEvent) => {
    setActiveJobs((prev) => [event.job, ...prev]);
  }, []);

  const handleStatusChanged = useCallback((event: StatusChangedEvent) => {
    setActiveJobs((prev) =>
      prev.map((j) =>
        j.job_id === event.job_id
          ? {
              ...j,
              status: event.new_status as JobStatus,
              asset_assigned: event.asset_assigned ?? j.asset_assigned,
              estimated_arrival: event.estimated_arrival ?? j.estimated_arrival,
              updated_at: new Date().toISOString(),
            }
          : j,
      ),
    );
  }, []);

  const handleDelayAlert = useCallback((event: DelayAlertEvent) => {
    setActiveJobs((prev) =>
      prev.map((j) =>
        j.job_id === event.job_id
          ? {
              ...j,
              delayed: true,
              delay_duration_minutes: event.delay_duration_minutes,
            }
          : j,
      ),
    );
    setDelayedJobs((prev) => {
      const exists = prev.some((j) => j.job_id === event.job_id);
      if (exists) {
        return prev.map((j) =>
          j.job_id === event.job_id
            ? { ...j, delay_duration_minutes: event.delay_duration_minutes }
            : j,
        );
      }
      // Add a minimal delayed job entry
      return [
        ...prev,
        {
          job_id: event.job_id,
          job_type: event.job_type as Job["job_type"],
          status: "in_progress" as const,
          tenant_id: "",
          origin: event.origin,
          destination: event.destination,
          scheduled_time: "",
          created_at: "",
          updated_at: new Date().toISOString(),
          priority: "normal" as const,
          delayed: true,
          delay_duration_minutes: event.delay_duration_minutes,
          asset_assigned: event.asset_assigned,
        },
      ];
    });
  }, []);

  useSchedulingWebSocket({
    subscriptions: [
      "job_created",
      "status_changed",
      "delay_alert",
      "cargo_update",
    ],
    onJobCreated: handleJobCreated,
    onStatusChanged: handleStatusChanged,
    onDelayAlert: handleDelayAlert,
  });

  if (loading) {
    return <LoadingSpinner message="Loading operations data..." />;
  }

  return (
    <div className="h-full flex flex-col bg-gray-50 overflow-hidden">
      {/* Storm_Mode banner (Task 11.7, Req 9.4.1) — pinned to the top of
          the operations control center. Banner hides itself when
          Storm_Mode is inactive; the override form is gated on the
          verified session's role claims (Req 8.6). */}
      <StormModeBanner />

      <div className="h-full flex flex-col gap-4 p-4 sm:p-6 overflow-y-auto lg:overflow-hidden">
        {/* Sync toolbar — last updated, manual refresh, and a non-blocking
            warning when a source failed (data still fails open to empty). */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-h-[1.25rem]">
            {loadError && (
              <span
                role="alert"
                className="inline-flex items-center gap-1.5 text-xs text-warning-dark"
              >
                <AlertTriangle className="h-3.5 w-3.5" />
                Some data failed to load — showing last known values.
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
          </div>
        </div>

        {/* Top: Summary Bar */}
        <OperationsSummaryBar summary={summary} />

        {/* Agent supervision: current autonomy level. Under auto-medium/
            full-auto, agents act without per-action approval, so the
            supervisor needs this state visible alongside the live activity
            feed and pause controls in the right rail. */}
        <AgentAutonomyBanner />

        {/* Inventory Health Indicator */}
        <InventoryHealthBadge alertCount={inventoryAlertCount} />

        {/* Main content: Map + Right sidebar — stacks below lg. */}
        <div className="flex-1 flex flex-col gap-4 min-h-0 lg:flex-row">
          {/* Map (~60% on lg; fixed height when stacked) */}
          <div className="h-[55vh] w-full min-h-0 lg:h-auto lg:w-3/5">
            <OperationsMap assets={assetLocations} />
          </div>

          {/* Right rail (~40% on lg) */}
          <div className="flex w-full flex-col gap-4 lg:w-2/5 lg:min-h-0 lg:overflow-y-auto">
            {/* Agent supervision — primary under autonomous operation: live
                action feed + per-agent pause/resume. Height-bounded so the
                panels' internal scroll works inside the flex-col rail. */}
            <div className="h-80 flex-shrink-0">
              <AgentActivityFeed />
            </div>
            <div className="h-64 flex-shrink-0">
              <AgentHealth />
            </div>
            <JobQueuePanel jobs={activeJobs} />
            <DelayedOperationsPanel jobs={activeJobs} />
            <ApprovalQueuePanel />
            <FuelStatusSidebar alerts={fuelAlerts} />
          </div>
        </div>
      </div>
    </div>
  );
}
