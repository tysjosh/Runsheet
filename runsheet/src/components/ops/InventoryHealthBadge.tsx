"use client";

import { Package } from "lucide-react";

interface InventoryHealthBadgeProps {
  alertCount: number;
}

/**
 * Compact inventory health indicator for the Operations Control View.
 * Shows the count of low-stock + out-of-stock items as a status badge.
 *
 * Validates: Requirement 7.6
 */
export default function InventoryHealthBadge({
  alertCount,
}: InventoryHealthBadgeProps) {
  const hasAlerts = alertCount > 0;

  return (
    <div
      className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium w-fit ${
        hasAlerts
          ? "bg-warning-light text-warning-dark border border-warning-light"
          : "bg-success-light text-success-dark border border-success-light"
      }`}
      aria-label={`Inventory health: ${alertCount} items need attention.`}
    >
      <Package className="w-3.5 h-3.5" aria-hidden="true" />
      <span>
        {hasAlerts
          ? `${alertCount} inventory ${alertCount === 1 ? "alert" : "alerts"}`
          : "Inventory healthy"}
      </span>
    </div>
  );
}
