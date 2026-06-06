"use client";

import { useCallback, useEffect, useState } from "react";
import { apiService } from "../../services/api";
import LoadingSpinner from "../LoadingSpinner";
import type {
  DriverStatus,
  DriverUtilization,
} from "../ops/DriverUtilizationList";
import DriverUtilizationList from "../ops/DriverUtilizationList";

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
      const data = (await apiService.getDriverUtilization(
        statusFilter || undefined,
      )) as DriverUtilization[];

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
  }, [statusFilter]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  if (loading) {
    return <LoadingSpinner message="Loading driver utilization..." />;
  }

  return (
    <div className="h-full flex flex-col bg-white p-6">
      <div className="mb-4">
        <p className="text-gray-600">
          Monitor driver availability and workload for dispatch operations
        </p>
      </div>

      <div className="flex-1 overflow-y-auto">
        <DriverUtilizationList
          drivers={drivers}
          statusFilter={statusFilter}
          onStatusFilterChange={setStatusFilter}
        />
      </div>
    </div>
  );
}
