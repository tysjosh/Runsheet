/**
 * @deprecated Use DriverUtilizationList instead. This file is a re-export
 * shim kept during the deprecation window for backward compatibility.
 * It adapts the legacy RiderUtilization props to the new DriverUtilization shape.
 */
"use client";

import type { RiderStatus, RiderUtilization } from "../../services/opsApi";
import DriverUtilizationList from "./DriverUtilizationList";
import type { DriverUtilization } from "./DriverUtilizationList";

interface RiderUtilizationListProps {
  riders: RiderUtilization[];
  capacity?: number;
  statusFilter?: RiderStatus | "";
  onStatusFilterChange?: (status: RiderStatus | "") => void;
}

function mapRiderToDriver(rider: RiderUtilization): DriverUtilization {
  return {
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
  };
}

export default function RiderUtilizationList({
  riders,
  capacity,
  statusFilter,
  onStatusFilterChange,
}: RiderUtilizationListProps) {
  const drivers = riders.map(mapRiderToDriver);
  const mappedFilter = statusFilter === "idle" ? "on_break" : statusFilter === "offline" ? "off_duty" : statusFilter === "active" ? "active" : "";

  return (
    <DriverUtilizationList
      drivers={drivers}
      capacity={capacity}
      statusFilter={mappedFilter || ""}
      onStatusFilterChange={onStatusFilterChange ? (s) => {
        const mapped = s === "on_break" ? "idle" : s === "off_duty" ? "offline" : s === "active" ? "active" : "";
        onStatusFilterChange(mapped as RiderStatus | "");
      } : undefined}
    />
  );
}

export type { RiderStatus, RiderUtilization };
