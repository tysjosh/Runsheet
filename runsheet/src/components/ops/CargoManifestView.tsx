"use client";

import { type Column, Table } from "@/components/ui";
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
      return "text-info-dark bg-info-light";
    case "in_transit":
      return "text-warning-dark bg-warning-light";
    case "delivered":
      return "text-success-dark bg-success-light";
    case "damaged":
      return "text-error-dark bg-error-light";
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
  onUpdateItemStatus: (
    itemId: string,
    newStatus: CargoItemStatus,
  ) => Promise<void>;
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
  const columns: Column<SchedulingCargoItem>[] = [
    {
      key: "item_id",
      label: "Item ID",
      className: "text-sm font-medium text-primary",
      render: (item) => item.item_id,
    },
    {
      key: "description",
      label: "Description",
      className: "text-sm text-gray-700",
      render: (item) => item.description,
    },
    {
      key: "weight_kg",
      label: "Weight (kg)",
      className: "text-sm text-gray-700",
      render: (item) => item.weight_kg.toLocaleString(),
    },
    {
      key: "container_number",
      label: "Container",
      className: "text-sm text-gray-700",
      render: (item) => item.container_number ?? "—",
    },
    {
      key: "seal_number",
      label: "Seal No.",
      className: "text-sm text-gray-700",
      render: (item) => item.seal_number ?? "—",
    },
    {
      key: "item_status",
      label: "Status",
      render: (item) => (
        <span
          className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(item.item_status)}`}
        >
          {formatStatus(item.item_status)}
        </span>
      ),
    },
    {
      key: "actions",
      label: "Actions",
      render: (item) => (
        <CargoItemActions
          itemId={item.item_id}
          currentStatus={item.item_status}
          onUpdateStatus={onUpdateItemStatus}
        />
      ),
    },
  ];

  return (
    <Table<SchedulingCargoItem>
      ariaLabel="Cargo manifest"
      columns={columns}
      data={items}
      getRowId={(item) => item.item_id}
      emptyState={
        <div className="text-gray-500">
          <p className="text-lg font-medium text-gray-400">No cargo items</p>
          <p className="text-sm text-gray-400 mt-1">
            This job has no cargo manifest items
          </p>
        </div>
      }
    />
  );
}
