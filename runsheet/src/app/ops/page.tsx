"use client";

import { Package } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import LoadingSpinner from "../../components/LoadingSpinner";
import OpsFilters, {
  type OpsFilterValues,
} from "../../components/ops/OpsFilters";
import ShipmentBoard from "../../components/ops/ShipmentBoard";
import ShipmentSummaryBar from "../../components/ops/ShipmentSummaryBar";
import { useOpsWebSocket } from "../../hooks/useOpsWebSocket";
import type { OpsShipment, ShipmentFilters } from "../../services/opsApi";
import { getShipments, getSlaBreaches } from "../../services/opsApi";

const INITIAL_FILTERS: OpsFilterValues = {
  status: "",
  rider_id: "",
  start_date: "",
  end_date: "",
};

/**
 * Shipment Status Board page.
 *
 * Displays a live, sortable, color-coded shipment board with summary bar,
 * filters, and real-time WebSocket updates.
 *
 * Validates: Requirements 12.1-12.6
 */
export default function OpsShipmentBoardPage() {
  const [shipments, setShipments] = useState<OpsShipment[]>([]);
  const [slaBreachIds, setSlaBreachIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<OpsFilterValues>(INITIAL_FILTERS);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);

      const apiFilters: ShipmentFilters = {};
      if (filters.status) apiFilters.status = filters.status;
      if (filters.rider_id) apiFilters.rider_id = filters.rider_id;
      if (filters.start_date) apiFilters.start_date = filters.start_date;
      if (filters.end_date) apiFilters.end_date = filters.end_date;

      const [shipmentsRes, slaRes] = await Promise.all([
        getShipments(apiFilters),
        getSlaBreaches(),
      ]);

      setShipments(shipmentsRes.data);
      setSlaBreachIds(new Set(slaRes.data.map((s) => s.shipment_id)));
    } catch (error) {
      console.error("Failed to load shipment data:", error);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  /**
   * Handle real-time shipment updates via WebSocket.
   * Updates the affected row within 5 seconds.
   *
   * Validates: Requirement 12.6
   */
  const handleShipmentUpdate = useCallback((updated: OpsShipment) => {
    setShipments((prev) => {
      const idx = prev.findIndex((s) => s.shipment_id === updated.shipment_id);
      if (idx >= 0) {
        const next = [...prev];
        next[idx] = updated;
        return next;
      }
      return [updated, ...prev];
    });
  }, []);

  const handleSlaBreach = useCallback((breach: { shipment_id: string }) => {
    setSlaBreachIds((prev) => new Set(prev).add(breach.shipment_id));
  }, []);

  useOpsWebSocket({
    subscriptions: ["shipment_update", "sla_breach"],
    onShipmentUpdate: handleShipmentUpdate,
    onSlaBreach: handleSlaBreach,
  });

  if (loading) {
    return <LoadingSpinner message="Loading shipments..." />;
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <Package className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-[#232323]">
              Shipment Status Board
            </h1>
            <p className="text-gray-500">
              Monitor all active shipments in real time
            </p>
          </div>
        </div>

        <OpsFilters filters={filters} onChange={setFilters} />
      </div>

      {/* Summary Bar */}
      <div className="border-b border-gray-100 px-8 py-4">
        <ShipmentSummaryBar shipments={shipments} slaBreachIds={slaBreachIds} />
      </div>

      {/* Board */}
      <div className="flex-1 overflow-y-auto">
        <ShipmentBoard shipments={shipments} slaBreachIds={slaBreachIds} />
      </div>
    </div>
  );
}
