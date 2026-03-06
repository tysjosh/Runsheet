"use client";

import { Activity, AlertTriangle, Clock, Fuel } from "lucide-react";
import type { OperationsControlSummary } from "../../types/api";

interface OperationsSummaryBarProps {
  summary: OperationsControlSummary;
}

/**
 * Summary bar showing active jobs, delayed count, available assets,
 * and fuel alerts count for the operations control dashboard.
 *
 * Validates: Requirement 10.2
 */
export default function OperationsSummaryBar({
  summary,
}: OperationsSummaryBarProps) {
  const stats = [
    {
      label: "Active Jobs",
      value: summary.active_jobs,
      icon: Activity,
      color: "text-blue-600",
      bg: "bg-blue-50",
    },
    {
      label: "Delayed",
      value: summary.delayed_jobs,
      icon: Clock,
      color: summary.delayed_jobs > 0 ? "text-red-600" : "text-green-600",
      bg: summary.delayed_jobs > 0 ? "bg-red-50" : "bg-green-50",
    },
    {
      label: "Available Assets",
      value: summary.available_assets,
      icon: AlertTriangle,
      color: "text-[#232323]",
      bg: "bg-gray-50",
    },
    {
      label: "Fuel Alerts",
      value: summary.fuel_alerts,
      icon: Fuel,
      color: summary.fuel_alerts > 0 ? "text-orange-600" : "text-green-600",
      bg: summary.fuel_alerts > 0 ? "bg-orange-50" : "bg-green-50",
    },
  ];

  return (
    <div
      className="grid grid-cols-2 md:grid-cols-4 gap-4"
      role="region"
      aria-label="Operations summary"
    >
      {stats.map((stat) => (
        <div
          key={stat.label}
          className={`${stat.bg} rounded-xl px-4 py-3 flex items-center gap-3`}
        >
          <div
            className={`w-10 h-10 rounded-lg flex items-center justify-center ${stat.bg}`}
          >
            <stat.icon className={`w-5 h-5 ${stat.color}`} aria-hidden="true" />
          </div>
          <div>
            <p className={`text-2xl font-semibold ${stat.color}`}>
              {stat.value}
            </p>
            <p className="text-xs text-gray-500">{stat.label}</p>
          </div>
        </div>
      ))}
    </div>
  );
}
