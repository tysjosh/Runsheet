"use client";

import { Fuel } from "lucide-react";
import type { FuelAlert } from "../../services/fuelApi";

interface FuelStatusSidebarProps {
  alerts: FuelAlert[];
}

const STATUS_CONFIG: Record<string, { bg: string; text: string }> = {
  critical: { bg: "bg-red-100", text: "text-red-700" },
  low: { bg: "bg-yellow-100", text: "text-yellow-700" },
  empty: { bg: "bg-gray-100", text: "text-gray-700" },
};

/**
 * Fuel status sidebar showing stations with low or critical fuel levels.
 *
 * Validates: Requirement 10.6
 */
export default function FuelStatusSidebar({ alerts }: FuelStatusSidebarProps) {
  return (
    <div className="bg-white rounded-xl border border-gray-100">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100">
        <Fuel className="w-4 h-4 text-orange-600" />
        <h3 className="text-sm font-semibold text-[#232323]">Fuel Alerts</h3>
        <span className="ml-auto text-xs text-orange-500 font-medium">
          {alerts.length}
        </span>
      </div>

      <div className="max-h-48 overflow-y-auto divide-y divide-gray-50">
        {alerts.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-green-600">
            All stations normal
          </div>
        ) : (
          alerts.map((alert) => {
            const config =
              STATUS_CONFIG[alert.status] ?? STATUS_CONFIG.low;
            return (
              <div
                key={`${alert.station_id}-${alert.fuel_type}`}
                className="px-4 py-2.5 hover:bg-gray-50"
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-[#232323] truncate">
                    {alert.name}
                  </span>
                  <span
                    className={`px-2 py-0.5 rounded text-xs font-medium ${config.bg} ${config.text}`}
                  >
                    {alert.status}
                  </span>
                </div>
                <div className="flex items-center justify-between mt-1">
                  <span className="text-xs text-gray-500">
                    {alert.fuel_type}
                  </span>
                  <span className="text-xs text-gray-400">
                    {alert.stock_percentage.toFixed(1)}% remaining
                  </span>
                </div>
                {/* Stock bar */}
                <div
                  className="mt-1 h-1.5 bg-gray-200 rounded-full overflow-hidden"
                  role="progressbar"
                  aria-valuenow={Math.round(alert.stock_percentage)}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={`Stock level ${Math.round(alert.stock_percentage)}%`}
                >
                  <div
                    className={`h-full rounded-full ${
                      alert.status === "critical"
                        ? "bg-red-500"
                        : alert.status === "empty"
                          ? "bg-gray-400"
                          : "bg-yellow-500"
                    }`}
                    style={{
                      width: `${Math.min(alert.stock_percentage, 100)}%`,
                    }}
                  />
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
