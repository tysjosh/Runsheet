"use client";

import type { OpsShipment } from "../../services/opsApi";

interface ShipmentSummaryBarProps {
  shipments: OpsShipment[];
  slaBreachIds: Set<string>;
}

/**
 * Summary bar showing counts of shipments by status.
 *
 * Validates: Requirement 12.5
 */
export default function ShipmentSummaryBar({
  shipments,
  slaBreachIds,
}: ShipmentSummaryBarProps) {
  const total = shipments.length;
  const inTransit = shipments.filter((s) => s.status === "in_transit").length;
  const delivered = shipments.filter((s) => s.status === "delivered").length;
  const failed = shipments.filter((s) => s.status === "failed").length;
  const slaBreach = slaBreachIds.size;

  const stats = [
    { label: "Total", value: total, color: "text-[#232323]" },
    { label: "In Transit", value: inTransit, color: "text-yellow-600" },
    { label: "Delivered", value: delivered, color: "text-green-600" },
    { label: "Failed", value: failed, color: "text-red-600" },
    { label: "SLA Breached", value: slaBreach, color: "text-orange-600" },
  ];

  return (
    <div className="grid grid-cols-5 gap-4">
      {stats.map((stat) => (
        <div key={stat.label} className="text-center">
          <div className={`text-2xl font-semibold ${stat.color}`}>
            {stat.value}
          </div>
          <div className="text-sm text-gray-500">{stat.label}</div>
        </div>
      ))}
    </div>
  );
}
