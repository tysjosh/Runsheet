"use client";

import { useCallback, useEffect, useState } from "react";
import type { Job, OperationsControlSummary } from "../../types/api";
import type { FuelAlert } from "../../services/fuelApi";
import { getActiveJobs, getDelayedJobs } from "../../services/schedulingApi";
import { getAlerts as getFuelAlerts } from "../../services/fuelApi";
import { apiService } from "../../services/api";
import { useSchedulingWebSocket } from "../../hooks/useSchedulingWebSocket";
import type {
  JobCreatedEvent,
  StatusChangedEvent,
  DelayAlertEvent,
} from "../../hooks/useSchedulingWebSocket";
import type { JobStatus } from "../../types/api";
import OperationsSummaryBar from "./OperationsSummaryBar";
import OperationsMap from "./OperationsMap";
import JobQueuePanel from "./JobQueuePanel";
import DelayedOperationsPanel from "./DelayedOperationsPanel";
import ApprovalQueuePanel from "./ApprovalQueuePanel";
import FuelStatusSidebar from "./FuelStatusSidebar";
import LoadingSpinner from "../LoadingSpinner";

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
 * Subscribes to scheduling + fuel WebSocket events for real-time updates.
 *
 * Layout:
 * - Top: OperationsSummaryBar (full width)
 * - Left: OperationsMap (~60% width)
 * - Right: JobQueuePanel + DelayedOperationsPanel + FuelStatusSidebar (~40% width)
 *
 * Validates: Requirements 10.1-10.7
 */
export default function OperationsControlView() {
  const [activeJobs, setActiveJobs] = useState<Job[]>([]);
  const [delayedJobs, setDelayedJobs] = useState<Job[]>([]);
  const [fuelAlerts, setFuelAlerts] = useState<FuelAlert[]>([]);
  const [assetLocations, setAssetLocations] = useState<AssetLocation[]>([]);
  const [loading, setLoading] = useState(true);

  /** Build summary from current state */
  const summary: OperationsControlSummary = {
    active_jobs: activeJobs.length,
    delayed_jobs: delayedJobs.length,
    available_assets: assetLocations.filter((a) => !a.job_id).length,
    fuel_alerts: fuelAlerts.length,
  };

  /** Load all data sources independently — each request handles its own errors */
  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const [activeRes, delayedRes, fuelRes, assetsRes] = await Promise.allSettled([
        getActiveJobs(),
        getDelayedJobs(),
        getFuelAlerts(),
        apiService.getAssets(),
      ]);

      setActiveJobs(activeRes.status === "fulfilled" ? activeRes.value.data : []);
      setDelayedJobs(delayedRes.status === "fulfilled" ? delayedRes.value.data : []);
      setFuelAlerts(fuelRes.status === "fulfilled" ? fuelRes.value.data : []);

      const assets = assetsRes.status === "fulfilled" ? assetsRes.value.data : [];

      // Build asset locations with job assignment info
      const jobsByAsset = new Map<string, Job>();
      for (const job of (activeRes.status === "fulfilled" ? activeRes.value.data : [])) {
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
            name: a.name || a.plateNumber || a.vesselName || a.containerNumber || a.id,
            lat: a.currentLocation.coordinates.lat,
            lng: a.currentLocation.coordinates.lon,
            job_status: assignedJob?.status,
            job_id: assignedJob?.job_id,
          };
        });
      setAssetLocations(locations);
    } catch (error) {
      console.error("Failed to load operations data:", error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  /**
   * WebSocket handlers for real-time updates.
   * Validates: Requirement 10.7
   */
  const handleJobCreated = useCallback((event: JobCreatedEvent) => {
    setActiveJobs((prev) => [event.job, ...prev]);
  }, []);

  const handleStatusChanged = useCallback(
    (event: StatusChangedEvent) => {
      setActiveJobs((prev) =>
        prev.map((j) =>
          j.job_id === event.job_id
            ? {
                ...j,
                status: event.new_status as JobStatus,
                asset_assigned: event.asset_assigned ?? j.asset_assigned,
                estimated_arrival:
                  event.estimated_arrival ?? j.estimated_arrival,
                updated_at: new Date().toISOString(),
              }
            : j,
        ),
      );
    },
    [],
  );

  const handleDelayAlert = useCallback(
    (event: DelayAlertEvent) => {
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
    },
    [],
  );

  useSchedulingWebSocket({
    subscriptions: ["job_created", "status_changed", "delay_alert", "cargo_update"],
    onJobCreated: handleJobCreated,
    onStatusChanged: handleStatusChanged,
    onDelayAlert: handleDelayAlert,
  });

  if (loading) {
    return <LoadingSpinner message="Loading operations data..." />;
  }

  return (
    <div className="h-full flex flex-col gap-4 p-6 bg-gray-50 overflow-hidden">
      {/* Top: Summary Bar */}
      <OperationsSummaryBar summary={summary} />

      {/* Main content: Map + Right sidebar */}
      <div className="flex-1 flex gap-4 min-h-0">
        {/* Left: Map (~60%) */}
        <div className="w-3/5 min-h-0">
          <OperationsMap assets={assetLocations} />
        </div>

        {/* Right sidebar (~40%) */}
        <div className="w-2/5 flex flex-col gap-4 overflow-y-auto min-h-0">
          <JobQueuePanel jobs={activeJobs} />
          <DelayedOperationsPanel jobs={activeJobs} />
          <ApprovalQueuePanel />
          <FuelStatusSidebar alerts={fuelAlerts} />
        </div>
      </div>
    </div>
  );
}
