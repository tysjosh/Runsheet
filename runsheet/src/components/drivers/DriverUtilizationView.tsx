"use client";

import { useCallback, useEffect, useState } from "react";
import LoadingSpinner from "../LoadingSpinner";
import type {
  DriverStatus,
  DriverUtilization,
} from "../ops/DriverUtilizationList";
import DriverUtilizationList from "../ops/DriverUtilizationList";
import { getAuthToken } from "../../utils/auth";

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
      const baseUrl =
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
      const params = new URLSearchParams();
      if (statusFilter) params.set("status", statusFilter);
      const url = `${baseUrl}/ops/drivers/utilization${params.toString() ? `?${params}` : ""}`;

      // Get auth token and include in request
      const token = await getAuthToken();
      const headers: HeadersInit = {
        "Content-Type": "application/json",
      };
      if (token) {
        headers.Authorization = `Bearer ${token}`;
      }

      const res = await fetch(url, { headers });
      if (res.ok) {
        const json = await res.json();
        const data = json.items ?? json.data ?? json;
        setDrivers(Array.isArray(data) ? data : []);
      } else {
        console.error(`Failed to load drivers: ${res.status} ${res.statusText}`);
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
