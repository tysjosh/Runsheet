"use client";

import { ArrowLeft, Clock, MapPin, Package, Truck, User } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import type React from "react";
import { useCallback, useEffect, useState } from "react";
import LoadingSpinner from "../../../../components/LoadingSpinner";
import ShipmentMap from "../../../../components/ops/ShipmentMap";
import ShipmentTimeline from "../../../../components/ops/ShipmentTimeline";
import { useOpsWebSocket } from "../../../../hooks/useOpsWebSocket";
import type { OpsEvent, ShipmentDetail } from "../../../../services/opsApi";
import { getShipmentById } from "../../../../services/opsApi";

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-gray-100 text-gray-700",
  in_transit: "bg-yellow-100 text-yellow-700",
  delivered: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  returned: "bg-orange-100 text-orange-700",
};

function formatDate(dateStr?: string): string {
  if (!dateStr) return "—";
  try {
    return new Date(dateStr).toLocaleString();
  } catch {
    return dateStr;
  }
}

/**
 * Shipment Tracking Monitor page.
 *
 * Displays the full event timeline, current status, and map for a single
 * shipment. Appends new events in real time via WebSocket.
 *
 * Validates: Requirements 15.1-15.5, 20.5
 */
export default function ShipmentTrackingPage() {
  const params = useParams<{ id: string }>();
  const shipmentId = params.id;

  const [shipment, setShipment] = useState<ShipmentDetail | null>(null);
  const [events, setEvents] = useState<OpsEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadShipment = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await getShipmentById(shipmentId);
      setShipment(res.data);
      setEvents(res.data.events ?? []);
    } catch (err) {
      console.error("Failed to load shipment:", err);
      setError("Failed to load shipment details. Please try again.");
    } finally {
      setLoading(false);
    }
  }, [shipmentId]);

  useEffect(() => {
    loadShipment();
  }, [loadShipment]);

  /**
   * Handle real-time shipment updates via WebSocket.
   * When a shipment_update arrives for this shipment, update the header
   * and append any new event to the timeline within 5 seconds.
   *
   * Validates: Requirement 15.5
   */
  const handleShipmentUpdate = useCallback(
    (updated: ShipmentDetail) => {
      if (updated.shipment_id !== shipmentId) return;

      setShipment((prev) => (prev ? { ...prev, ...updated } : updated));

      // If the update carries new events, merge them
      if (updated.events && updated.events.length > 0) {
        setEvents((prev) => {
          const existingIds = new Set(prev.map((e) => e.event_id));
          const newEvents = updated.events?.filter(
            (e) => !existingIds.has(e.event_id),
          );
          return newEvents && newEvents.length > 0
            ? [...prev, ...newEvents]
            : prev;
        });
      }
    },
    [shipmentId],
  );

  useOpsWebSocket({
    subscriptions: ["shipment_update"],
    onShipmentUpdate: handleShipmentUpdate,
  });

  if (loading) {
    return <LoadingSpinner message="Loading shipment details..." />;
  }

  if (error || !shipment) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center">
          <p className="text-red-600 mb-4">{error ?? "Shipment not found."}</p>
          <Link
            href="/ops"
            className="text-sm text-[#232323] underline hover:no-underline"
          >
            Back to Fleet
          </Link>
        </div>
      </div>
    );
  }

  const statusStyle =
    STATUS_COLORS[shipment.status] ?? "bg-gray-100 text-gray-700";

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <Link
          href="/ops"
          className="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-[#232323] mb-4"
        >
          <ArrowLeft className="w-4 h-4" />
          Back to Fleet
        </Link>

        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <Package className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-[#232323]">
              Shipment {shipment.shipment_id}
            </h1>
            <span
              className={`inline-block mt-1 px-2 py-0.5 rounded text-xs font-medium ${statusStyle}`}
            >
              {shipment.status.replace(/_/g, " ").toUpperCase()}
            </span>
          </div>
        </div>

        {/* Info grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <InfoItem
            icon={<User className="w-4 h-4" />}
            label="Rider"
            value={shipment.rider_id ?? "—"}
          />
          <InfoItem
            icon={<MapPin className="w-4 h-4" />}
            label="Origin"
            value={shipment.origin ?? "—"}
          />
          <InfoItem
            icon={<Truck className="w-4 h-4" />}
            label="Destination"
            value={shipment.destination ?? "—"}
          />
          <InfoItem
            icon={<Clock className="w-4 h-4" />}
            label="Est. Delivery"
            value={formatDate(shipment.estimated_delivery)}
          />
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-8 py-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Timeline — takes 2 columns on large screens */}
          <div className="lg:col-span-2">
            <h2 className="text-lg font-semibold text-[#232323] mb-4">
              Event Timeline
            </h2>
            <ShipmentTimeline events={events} />
          </div>

          {/* Map sidebar */}
          <div>
            <h2 className="text-lg font-semibold text-[#232323] mb-4">
              Route Map
            </h2>
            <ShipmentMap events={events} />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Small info display used in the header grid. */
function InfoItem({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-start gap-2">
      <div className="text-gray-400 mt-0.5">{icon}</div>
      <div>
        <p className="text-xs text-gray-500">{label}</p>
        <p className="text-sm font-medium text-[#232323]">{value}</p>
      </div>
    </div>
  );
}
