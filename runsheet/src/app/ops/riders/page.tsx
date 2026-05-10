"use client";

import { Users } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import LoadingSpinner from "../../../components/LoadingSpinner";
import DriverUtilizationList from "../../../components/ops/DriverUtilizationList";
import type { DriverUtilization } from "../../../components/ops/DriverUtilizationList";
import { useOpsWebSocket } from "../../../hooks/useOpsWebSocket";
import type {
  RiderStatus,
  RiderUtilization,
  RiderUtilizationFilters,
} from "../../../services/opsApi";
import { getRiderUtilization } from "../../../services/opsApi";

/**
 * Rider Utilization and Availability View page.
 *
 * Displays a live rider list with utilization bars, status filtering,
 * and real-time WebSocket updates.
 *
 * Validates: Requirements 13.1-13.5
 */
export default function OpsRiderUtilizationPage() {
  const [riders, setRiders] = useState<RiderUtilization[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<RiderStatus | "">("");

  const loadData = useCallback(async () => {
    try {
      setLoading(true);

      const apiFilters: RiderUtilizationFilters = {};
      if (statusFilter) apiFilters.status = statusFilter;

      const res = await getRiderUtilization(apiFilters);
      setRiders(res.data);
    } catch (error) {
      console.error("Failed to load rider utilization data:", error);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  /**
   * Handle real-time rider updates via WebSocket.
   * Updates the affected row within 5 seconds.
   *
   * Validates: Requirement 13.5
   */
  const handleRiderUpdate = useCallback((updated: RiderUtilization) => {
    setRiders((prev) => {
      const idx = prev.findIndex((r) => r.rider_id === updated.rider_id);
      if (idx >= 0) {
        const next = [...prev];
        next[idx] = { ...next[idx], ...updated };
        return next;
      }
      return [updated, ...prev];
    });
  }, []);

  useOpsWebSocket({
    subscriptions: ["rider_update"],
    onRiderUpdate: handleRiderUpdate,
  });

  if (loading) {
    return <LoadingSpinner message="Loading rider utilization..." />;
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <Users className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-[#232323]">
              Rider Utilization
            </h1>
            <p className="text-gray-500">
              Monitor rider availability and workload
            </p>
          </div>
        </div>
      </div>

      {/* Rider List */}
      <div className="flex-1 overflow-y-auto">
        <DriverUtilizationList
          drivers={riders.map((rider) => ({
            driver_id: rider.rider_id,
            driver_name: rider.rider_name ?? null,
            status: rider.status === "idle" ? "on_break" : rider.status === "offline" ? "off_duty" : "active",
            active_order_count: rider.active_shipment_count,
            completed_today: rider.completed_today,
            last_seen: rider.last_seen ?? null,
            medical_card_expiry: null,
            assigned_truck_id: null,
            cdl_class: null,
            hazmat_endorsement: null,
            utilization_percentage: rider.utilization_percentage ?? null,
          } as DriverUtilization))}
          statusFilter={statusFilter === "idle" ? "on_break" : statusFilter === "offline" ? "off_duty" : statusFilter === "active" ? "active" : ""}
          onStatusFilterChange={(s) => {
            const mapped = s === "on_break" ? "idle" : s === "off_duty" ? "offline" : s === "active" ? "active" : "";
            setStatusFilter(mapped as RiderStatus | "");
          }}
        />
      </div>
    </div>
  );
}
