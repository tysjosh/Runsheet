"use client";

import type { CargoItemStatus, SchedulingCargoItem } from "../../types/api";
import CargoItemActions from "./CargoItemActions";

/**
 * Status badge color-coding for cargo items.
 *
 * pending: gray, loaded: blue, in_transit: yellow, delivered: green, damaged: red
 *
 * Validates: Requirement 12.2
 */
function getStatusBadge(status: CargoItemStatus): string {
  switch (status) {
    case "pending":
      return "text-gray-700 bg-gray-100";
    case "loaded":
      return "text-blue-700 bg-blue-100";
    case "in_transit":
      return "text-yellow-700 bg-yellow-100";
    case "delivered":
      return "text-green-700 bg-green-100";
    case "damaged":
      return "text-red-700 bg-red-100";
    default:
      return "text-gray-700 bg-gray-100";
  }
}

function formatStatus(status: string): string {
  return status
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

interface CargoManifestViewProps {
  items: SchedulingCargoItem[];
  onUpdateItemStatus: (itemId: string, newStatus: CargoItemStatus) => Promise<void>;
}

/**
 * Cargo item list with item_id, description, weight_kg, container_number,
 * seal_number, item_status with status color-coding and action buttons.
 *
 * Validates: Requirements 12.2, 12.4
 */
export default function CargoManifestView({
  items,
  onUpdateItemStatus,
}: CargoManifestViewProps) {
  if (items.length === 0) {
    return (
      <div className="text-center py-16 text-gray-500">
        <p className="text-lg font-medium text-gray-400">No cargo items</p>
        <p className="text-sm text-gray-400 mt-1">
          This job has no cargo manifest items
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full" aria-label="Cargo manifest">
        <thead className="bg-gray-50 sticky top-0 border-b border-gray-100">
          <tr>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
              Item ID
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
              Description
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
              Weight (kg)
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
              Container
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
              Seal No.
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
              Status
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
              Actions
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {items.map((item) => (
            <tr key={item.item_id} className="transition-colors hover:bg-gray-50">
              <td className="px-6 py-3 text-sm font-medium text-[#232323]">
                {item.item_id}
              </td>
              <td className="px-6 py-3 text-sm text-gray-700">
                {item.description}
              </td>
              <td className="px-6 py-3 text-sm text-gray-700">
                {item.weight_kg.toLocaleString()}
              </td>
              <td className="px-6 py-3 text-sm text-gray-700">
                {item.container_number ?? "—"}
              </td>
              <td className="px-6 py-3 text-sm text-gray-700">
                {item.seal_number ?? "—"}
              </td>
              <td className="px-6 py-3">
                <span
                  className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(item.item_status)}`}
                >
                  {formatStatus(item.item_status)}
                </span>
              </td>
              <td className="px-6 py-3">
                <CargoItemActions
                  itemId={item.item_id}
                  currentStatus={item.item_status}
                  onUpdateStatus={onUpdateItemStatus}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
