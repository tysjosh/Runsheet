/**
 * Stat component for displaying a single metric
 */

import type React from "react";

export interface StatProps {
  label: string;
  value: string | number;
  trend?: string;
  className?: string;
}

export const Stat: React.FC<StatProps> = ({
  label,
  value,
  trend,
  className = "",
}) => {
  return (
    <div
      className={`bg-white rounded-xl border border-gray-100 p-6 ${className}`}
    >
      <div className="text-sm font-medium text-gray-600 mb-1">{label}</div>
      <div className="text-2xl font-bold text-gray-900">{value}</div>
      {trend && <div className="text-sm text-gray-500 mt-1">{trend}</div>}
    </div>
  );
};
