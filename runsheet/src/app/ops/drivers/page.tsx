"use client";

import { Truck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import LoadingSpinner from "../../../components/LoadingSpinner";
import DriverUtilizationList from "../../../components/ops/DriverUtilizationList";
import type {
  DriverStatus,
  DriverUtilization,
} from "../../../components/ops/DriverUtilizationList";

/**
 * Drivers page — displays the DriverUtilizationList with data fetched
 * from the driver API.
 */
export default function DriversPage() {
  const [drivers, setDrivers] = useState<DriverUtilization[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<DriverStatus | "">("");

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const baseUrl =
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
      const params = new URLSearchParams();
      if (statusFilter) params.set("status", statusFilter);
      const url = `${baseUrl}/drivers/utilization${params.toString() ? `?${params}` : ""}`;
      const res = await fetch(url);
      if (res.ok) {
        const json = await res.json();
        setDrivers(json.data ?? json ?? []);
      }
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
    <div className="h-full flex flex-col bg-white">
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <Truck className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-[#232323]">
              Driver Utilization
            </h1>
            <p className="text-gray-500">
              Monitor driver availability and workload
            </p>
          </div>
        </div>
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
