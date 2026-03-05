"use client";

import { AlertTriangle, Droplets, Fuel, Timer } from "lucide-react";
import type { FuelNetworkSummary } from "../../services/fuelApi";

interface FuelSummaryBarProps {
  summary: FuelNetworkSummary;
}

function formatLiters(liters: number): string {
  if (liters >= 1_000_000) return `${(liters / 1_000_000).toFixed(1)}M L`;
  if (liters >= 1_000) return `${(liters / 1_000).toFixed(1)}K L`;
  return `${liters.toFixed(0)} L`;
}

/**
 * Network summary bar showing total capacity, current stock, active alerts,
 * and average days until empty across all fuel stations.
 *
 * Validates: Requirements 6.1, 6.2
 */
export default function FuelSummaryBar({ summary }: FuelSummaryBarProps) {
  const stockPct =
    summary.total_capacity_liters > 0
      ? (summary.total_current_stock_liters / summary.total_capacity_liters) *
        100
      : 0;

  const stats = [
    {
      label: "Total Capacity",
      value: formatLiters(summary.total_capacity_liters),
      icon: Fuel,
      color: "text-[#232323]",
    },
    {
      label: "Current Stock",
      value: `${formatLiters(summary.total_current_stock_liters)} (${stockPct.toFixed(1)}%)`,
      icon: Droplets,
      color: "text-blue-600",
    },
    {
      label: "Active Alerts",
      value: String(summary.active_alerts),
      icon: AlertTriangle,
      color: summary.active_alerts > 0 ? "text-red-600" : "text-green-600",
    },
    {
      label: "Avg Days Until Empty",
      value:
        summary.average_days_until_empty > 0
          ? `${summary.average_days_until_empty.toFixed(1)} days`
          : "N/A",
      icon: Timer,
      color:
        summary.average_days_until_empty < 3
          ? "text-red-600"
          : summary.average_days_until_empty < 7
            ? "text-yellow-600"
            : "text-green-600",
    },
  ];

  return (
    <div
      className="grid grid-cols-2 md:grid-cols-4 gap-4"
      role="region"
      aria-label="Fuel network summary"
    >
      {stats.map((stat) => (
        <div key={stat.label} className="text-center">
          <div className="flex items-center justify-center gap-1.5 mb-1">
            <stat.icon className={`w-4 h-4 ${stat.color}`} aria-hidden="true" />
            <span className={`text-2xl font-semibold ${stat.color}`}>
              {stat.value}
            </span>
          </div>
          <div className="text-sm text-gray-500">{stat.label}</div>
        </div>
      ))}
    </div>
  );
}
