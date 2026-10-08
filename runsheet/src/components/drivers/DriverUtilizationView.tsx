"use client";

import { useCallback, useEffect, useState } from "react";
import { apiService } from "../../services/api";
import type {
  DriverStatus,
  DriverUtilization,
} from "../ops/DriverUtilizationList";
import DriverUtilizationList from "../ops/DriverUtilizationList";
import { Skeleton } from "../ui";

/**
 * Driver Utilization View — displays real-time driver availability and workload
 * for dispatchers to manage daily operations
 */
export default function DriverUtilizationView() {
  const [drivers, setDrivers] = useState<DriverUtilization[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<DriverStatus | "">("");

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      // Session-aware fetch (SuperTokens cookie + anti-CSRF). Replaces the
      // legacy raw-fetch + Bearer-token path so Drivers matches every other
      // module's auth posture.
      // Every driver is read once and the status chips filter on the client,
      // so the chip counts stay right whichever chip is pressed.
      const data =
        (await apiService.getDriverUtilization()) as DriverUtilization[];

      // Correlate each driver's compliance qualification status via the
      // profile read so the list can surface a qualification-status chip
      // (cross-module-entity-linkage task 4 / Req 4.2, 4.3). Failures degrade
      // gracefully — the row simply renders an "unlinked" chip.
      const enriched = await Promise.all(
        data.map(async (driver) => {
          try {
            const profile = await apiService.getDriverProfile(driver.driver_id);
            const qualification_status =
              profile.qualification.status === "resolved"
                ? (profile.qualification.summary?.overall_status ?? null)
                : null;
            return { ...driver, qualification_status };
          } catch {
            return { ...driver, qualification_status: null };
          }
        }),
      );

      setDrivers(enriched);
    } catch (error) {
      console.error("Failed to load driver utilization data:", error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  if (loading) {
    return (
      <div className="p-4">
        <Skeleton rows={6} label="Loading driver utilization" />
      </div>
    );
  }
  return (
    <div className="flex h-full flex-col bg-surface">
      <DriverUtilizationList
        drivers={drivers}
        statusFilter={statusFilter}
        onStatusFilterChange={setStatusFilter}
      />
    </div>
  );
}
