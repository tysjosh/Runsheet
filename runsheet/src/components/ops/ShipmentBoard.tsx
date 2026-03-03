"use client";

import { ChevronDown, ChevronUp } from "lucide-react";
import { useCallback, useState } from "react";
import type { OpsShipment } from "../../services/opsApi";

type SortField =
  | "shipment_id"
  | "status"
  | "rider_id"
  | "origin"
  | "destination"
  | "estimated_delivery"
  | "last_event_timestamp";

type SortOrder = "asc" | "desc";

interface ShipmentBoardProps {
  shipments: OpsShipment[];
  slaBreachIds: Set<string>;
}

const COLUMNS: { key: SortField; label: string }[] = [
  { key: "shipment_id", label: "Shipment ID" },
  { key: "status", label: "Status" },
  { key: "rider_id", label: "Rider" },
  { key: "origin", label: "Origin" },
  { key: "destination", label: "Destination" },
  { key: "estimated_delivery", label: "Est. Delivery" },
  { key: "last_event_timestamp", label: "Last Update" },
];

/**
 * Returns the row background color class based on shipment status and SLA breach.
 *
 * Validates: Requirement 12.2
 * - green=delivered, yellow=in_transit, red=failed, orange=SLA breach
 */
function getRowColor(shipment: OpsShipment, isBreach: boolean): string {
  if (isBreach) return "bg-orange-50";
  switch (shipment.status) {
    case "delivered":
      return "bg-green-50";
    case "in_transit":
      return "bg-yellow-50";
    case "failed":
      return "bg-red-50";
    default:
      return "";
  }
}

function getStatusBadge(status: string, isBreach: boolean): string {
  if (isBreach) return "text-orange-700 bg-orange-100";
  switch (status) {
    case "delivered":
      return "text-green-700 bg-green-100";
    case "in_transit":
      return "text-yellow-700 bg-yellow-100";
    case "failed":
      return "text-red-700 bg-red-100";
    case "pending":
      return "text-blue-700 bg-blue-100";
    case "returned":
      return "text-gray-700 bg-gray-100";
    default:
      return "text-gray-700 bg-gray-100";
  }
}

function formatDate(dateStr?: string): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function compareValues(
  a: string | undefined,
  b: string | undefined,
  order: SortOrder,
): number {
  const aVal = a ?? "";
  const bVal = b ?? "";
  const cmp = aVal.localeCompare(bVal);
  return order === "asc" ? cmp : -cmp;
}

/**
 * Sortable shipment status board with color-coded rows.
 *
 * Validates: Requirements 12.1, 12.2, 12.4
 */
export default function ShipmentBoard({
  shipments,
  slaBreachIds,
}: ShipmentBoardProps) {
  const [sortField, setSortField] = useState<SortField>("last_event_timestamp");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");

  const handleSort = useCallback(
    (field: SortField) => {
      if (sortField === field) {
        setSortOrder((prev) => (prev === "asc" ? "desc" : "asc"));
      } else {
        setSortField(field);
        setSortOrder("asc");
      }
    },
    [sortField],
  );

  const sorted = [...shipments].sort((a, b) => {
    const aVal = a[sortField] as string | undefined;
    const bVal = b[sortField] as string | undefined;
    return compareValues(aVal, bVal, sortOrder);
  });

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null;
    return sortOrder === "asc" ? (
      <ChevronUp className="w-3 h-3 inline ml-1" />
    ) : (
      <ChevronDown className="w-3 h-3 inline ml-1" />
    );
  };

  if (shipments.length === 0) {
    return (
      <div className="text-center py-16 text-gray-500">
        <p className="text-lg font-medium text-gray-400">No shipments found</p>
        <p className="text-sm text-gray-400 mt-1">Try adjusting your filters</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full" aria-label="Shipment status board">
        <thead className="bg-gray-50 sticky top-0 border-b border-gray-100">
          <tr>
            {COLUMNS.map((col) => (
              <th
                key={col.key}
                className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer select-none hover:bg-gray-100"
                onClick={() => handleSort(col.key)}
                aria-sort={
                  sortField === col.key
                    ? sortOrder === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
              >
                {col.label}
                <SortIcon field={col.key} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {sorted.map((shipment) => {
            const isBreach = slaBreachIds.has(shipment.shipment_id);
            return (
              <tr
                key={shipment.shipment_id}
                className={`${getRowColor(shipment, isBreach)} transition-colors`}
              >
                <td className="px-6 py-3 text-sm font-medium text-[#232323]">
                  {shipment.shipment_id}
                </td>
                <td className="px-6 py-3">
                  <span
                    className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(shipment.status, isBreach)}`}
                  >
                    {isBreach
                      ? "SLA Breach"
                      : shipment.status.replace("_", " ")}
                  </span>
                </td>
                <td className="px-6 py-3 text-sm text-gray-700">
                  {shipment.rider_id ?? "—"}
                </td>
                <td className="px-6 py-3 text-sm text-gray-700">
                  {shipment.origin ?? "—"}
                </td>
                <td className="px-6 py-3 text-sm text-gray-700">
                  {shipment.destination ?? "—"}
                </td>
                <td className="px-6 py-3 text-sm text-gray-600">
                  {formatDate(shipment.estimated_delivery)}
                </td>
                <td className="px-6 py-3 text-sm text-gray-600">
                  {formatDate(shipment.last_event_timestamp)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
