"use client";

import { AlertTriangle, CheckCircle, Package, Truck } from "lucide-react";
import { useState } from "react";
import type { CargoItemStatus } from "../../types/api";

/**
 * Target statuses a cargo item can be transitioned to.
 */
const TARGET_STATUSES: {
  status: CargoItemStatus;
  label: string;
  icon: React.ReactNode;
  className: string;
}[] = [
  {
    status: "loaded",
    label: "Loaded",
    icon: <Package className="w-3 h-3" />,
    className: "text-info-dark bg-info-light hover:bg-info-light",
  },
  {
    status: "in_transit",
    label: "In Transit",
    icon: <Truck className="w-3 h-3" />,
    className: "text-warning-dark bg-warning-light hover:bg-warning-light",
  },
  {
    status: "delivered",
    label: "Delivered",
    icon: <CheckCircle className="w-3 h-3" />,
    className: "text-success-dark bg-success-light hover:bg-success-light",
  },
  {
    status: "damaged",
    label: "Damaged",
    icon: <AlertTriangle className="w-3 h-3" />,
    className: "text-error-dark bg-error-light hover:bg-error-light",
  },
];

interface CargoItemActionsProps {
  itemId: string;
  currentStatus: CargoItemStatus;
  onUpdateStatus: (itemId: string, newStatus: CargoItemStatus) => Promise<void>;
}

/**
 * Action buttons to update individual cargo item statuses.
 * Shows only statuses different from the current one.
 *
 * Validates: Requirement 12.4
 */
export default function CargoItemActions({
  itemId,
  currentStatus,
  onUpdateStatus,
}: CargoItemActionsProps) {
  const [loading, setLoading] = useState<CargoItemStatus | null>(null);

  const available = TARGET_STATUSES.filter((t) => t.status !== currentStatus);

  if (available.length === 0) return null;

  const handleClick = async (status: CargoItemStatus) => {
    setLoading(status);
    try {
      await onUpdateStatus(itemId, status);
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="flex items-center gap-1 flex-wrap">
      {available.map((target) => {
        const isLoading = loading === target.status;
        return (
          <button
            key={target.status}
            onClick={() => handleClick(target.status)}
            disabled={loading !== null}
            className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium transition-colors ${target.className} disabled:opacity-50`}
            aria-label={`Mark item ${itemId} as ${target.label}`}
          >
            {isLoading ? (
              <div className="w-3 h-3 animate-spin rounded-full border border-current border-t-transparent" />
            ) : (
              target.icon
            )}
            {target.label}
          </button>
        );
      })}
    </div>
  );
}
