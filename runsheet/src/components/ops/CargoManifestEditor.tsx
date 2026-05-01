"use client";

/**
 * CargoManifestEditor — Wraps CargoManifestView with an edit mode toggle.
 *
 * In view mode, renders the read-only CargoManifestView.
 * In edit mode, cargo item fields become editable inputs.
 * Submit calls updateCargo and displays the updated manifest.
 * Status change buttons per item call updateCargoItemStatus.
 * On API error: displays error message and reverts form to previous state.
 *
 * Validates:
 * - Requirement 7.1: Edit button opens editable form for cargo items
 * - Requirement 7.2: Submit calls updateCargo API and displays updated manifest
 * - Requirement 7.3: Status change buttons call updateCargoItemStatus
 * - Requirement 7.6: On API error, display error and revert form to previous state
 */

import {
  AlertTriangle,
  CheckCircle,
  Package,
  Pencil,
  Save,
  Truck,
  X,
} from "lucide-react";
import { useCallback, useState } from "react";
import type { CargoItemStatus, SchedulingCargoItem } from "../../types/api";
import {
  updateCargo,
  updateCargoItemStatus,
} from "../../services/schedulingApi";
import CargoManifestView from "./CargoManifestView";

// ─── Props ───────────────────────────────────────────────────────────────────

interface CargoManifestEditorProps {
  /** The job ID this cargo manifest belongs to */
  jobId: string;
  /** Current cargo items */
  items: SchedulingCargoItem[];
  /** Callback when items are updated (so parent can sync state) */
  onItemsChange?: (items: SchedulingCargoItem[]) => void;
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function CargoManifestEditor({
  jobId,
  items,
  onItemsChange,
}: CargoManifestEditorProps) {
  const [editing, setEditing] = useState(false);
  const [editItems, setEditItems] = useState<SchedulingCargoItem[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // ── Enter edit mode ──────────────────────────────────────────────────────

  const handleStartEdit = () => {
    setEditItems(items.map((item) => ({ ...item })));
    setError("");
    setEditing(true);
  };

  const handleCancelEdit = () => {
    setEditing(false);
    setEditItems([]);
    setError("");
  };

  // ── Field change handler ─────────────────────────────────────────────────

  const handleFieldChange = (
    itemId: string,
    field: keyof SchedulingCargoItem,
    value: string | number,
  ) => {
    setEditItems((prev) =>
      prev.map((item) =>
        item.item_id === itemId ? { ...item, [field]: value } : item,
      ),
    );
  };

  // ── Save edited manifest ─────────────────────────────────────────────────

  const handleSave = async () => {
    setError("");
    setSaving(true);
    try {
      const res = await updateCargo(jobId, editItems);
      onItemsChange?.(res.data);
      setEditing(false);
      setEditItems([]);
    } catch (err) {
      // Revert form to previous state (editItems stays as snapshot of items before edit)
      setEditItems(items.map((item) => ({ ...item })));
      setError(
        err instanceof Error ? err.message : "Failed to update cargo manifest",
      );
    } finally {
      setSaving(false);
    }
  };

  // ── Status change handler (used in both view and edit modes) ─────────────

  const handleUpdateItemStatus = useCallback(
    async (itemId: string, newStatus: CargoItemStatus) => {
      setError("");
      try {
        const res = await updateCargoItemStatus(jobId, itemId, newStatus);
        const updatedItem = res.data;

        // Update the parent's items list
        const updatedItems = items.map((item) =>
          item.item_id === itemId ? updatedItem : item,
        );
        onItemsChange?.(updatedItems);

        // Also update edit items if currently editing
        if (editing) {
          setEditItems((prev) =>
            prev.map((item) =>
              item.item_id === itemId ? updatedItem : item,
            ),
          );
        }
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : "Failed to update cargo item status",
        );
      }
    },
    [jobId, items, editing, onItemsChange],
  );

  // ── Shared styles ────────────────────────────────────────────────────────

  const inputClass =
    "w-full px-2 py-1 text-sm border border-gray-200 rounded focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center justify-between px-6 py-3 border-b border-gray-100">
        <h3 className="text-sm font-medium text-gray-600 uppercase tracking-wider">
          Cargo Manifest
        </h3>
        <div className="flex items-center gap-2">
          {editing ? (
            <>
              <button
                onClick={handleCancelEdit}
                disabled={saving}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50 disabled:opacity-50"
                aria-label="Cancel editing"
              >
                <X className="w-4 h-4" />
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={saving}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-white rounded-lg disabled:opacity-50"
                style={{ backgroundColor: "#232323" }}
                aria-label="Save cargo manifest"
              >
                {saving ? (
                  <div className="w-4 h-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
                ) : (
                  <Save className="w-4 h-4" />
                )}
                {saving ? "Saving..." : "Save"}
              </button>
            </>
          ) : (
            <button
              onClick={handleStartEdit}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
              aria-label="Edit cargo manifest"
            >
              <Pencil className="w-4 h-4" />
              Edit
            </button>
          )}
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="mx-6 mt-3">
          <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
            {error}
          </p>
        </div>
      )}

      {/* Content */}
      {editing ? (
        <EditableCargoTable
          items={editItems}
          onFieldChange={handleFieldChange}
          onUpdateItemStatus={handleUpdateItemStatus}
          inputClass={inputClass}
        />
      ) : (
        <CargoManifestView
          items={items}
          onUpdateItemStatus={handleUpdateItemStatus}
        />
      )}
    </div>
  );
}

// ─── Editable Table Sub-component ────────────────────────────────────────────

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

interface EditableCargoTableProps {
  items: SchedulingCargoItem[];
  onFieldChange: (
    itemId: string,
    field: keyof SchedulingCargoItem,
    value: string | number,
  ) => void;
  onUpdateItemStatus: (
    itemId: string,
    newStatus: CargoItemStatus,
  ) => Promise<void>;
  inputClass: string;
}

function EditableCargoTable({
  items,
  onFieldChange,
  onUpdateItemStatus,
  inputClass,
}: EditableCargoTableProps) {
  if (items.length === 0) {
    return (
      <div className="text-center py-16 text-gray-500">
        <p className="text-lg font-medium text-gray-400">No cargo items</p>
        <p className="text-sm text-gray-400 mt-1">
          This job has no cargo manifest items to edit
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full" aria-label="Editable cargo manifest">
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
            <EditableCargoRow
              key={item.item_id}
              item={item}
              onFieldChange={onFieldChange}
              onUpdateItemStatus={onUpdateItemStatus}
              inputClass={inputClass}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Editable Row Sub-component ──────────────────────────────────────────────

interface EditableCargoRowProps {
  item: SchedulingCargoItem;
  onFieldChange: (
    itemId: string,
    field: keyof SchedulingCargoItem,
    value: string | number,
  ) => void;
  onUpdateItemStatus: (
    itemId: string,
    newStatus: CargoItemStatus,
  ) => Promise<void>;
  inputClass: string;
}

function EditableCargoRow({
  item,
  onFieldChange,
  onUpdateItemStatus,
  inputClass,
}: EditableCargoRowProps) {
  return (
    <tr className="transition-colors hover:bg-gray-50">
      {/* Item ID — read-only */}
      <td className="px-6 py-3 text-sm font-medium text-[#232323]">
        {item.item_id}
      </td>

      {/* Description — editable */}
      <td className="px-6 py-3">
        <input
          type="text"
          value={item.description}
          onChange={(e) =>
            onFieldChange(item.item_id, "description", e.target.value)
          }
          className={inputClass}
          aria-label={`Description for item ${item.item_id}`}
        />
      </td>

      {/* Weight — editable */}
      <td className="px-6 py-3">
        <input
          type="number"
          value={item.weight_kg}
          onChange={(e) =>
            onFieldChange(
              item.item_id,
              "weight_kg",
              e.target.value === "" ? 0 : Number(e.target.value),
            )
          }
          min="0"
          step="any"
          className={inputClass}
          aria-label={`Weight for item ${item.item_id}`}
        />
      </td>

      {/* Container — editable */}
      <td className="px-6 py-3">
        <input
          type="text"
          value={item.container_number ?? ""}
          onChange={(e) =>
            onFieldChange(item.item_id, "container_number", e.target.value)
          }
          className={inputClass}
          aria-label={`Container number for item ${item.item_id}`}
        />
      </td>

      {/* Seal Number — editable */}
      <td className="px-6 py-3">
        <input
          type="text"
          value={item.seal_number ?? ""}
          onChange={(e) =>
            onFieldChange(item.item_id, "seal_number", e.target.value)
          }
          className={inputClass}
          aria-label={`Seal number for item ${item.item_id}`}
        />
      </td>

      {/* Status — read-only badge */}
      <td className="px-6 py-3">
        <span
          className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(item.item_status)}`}
        >
          {formatStatus(item.item_status)}
        </span>
      </td>

      {/* Actions — status change buttons */}
      <td className="px-6 py-3">
        <CargoItemStatusButtons
          itemId={item.item_id}
          currentStatus={item.item_status}
          onUpdateStatus={onUpdateItemStatus}
        />
      </td>
    </tr>
  );
}

// ─── Inline Status Buttons (mirrors CargoItemActions pattern) ────────────────

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
    className: "text-blue-700 bg-blue-100 hover:bg-blue-200",
  },
  {
    status: "in_transit",
    label: "In Transit",
    icon: <Truck className="w-3 h-3" />,
    className: "text-yellow-700 bg-yellow-100 hover:bg-yellow-200",
  },
  {
    status: "delivered",
    label: "Delivered",
    icon: <CheckCircle className="w-3 h-3" />,
    className: "text-green-700 bg-green-100 hover:bg-green-200",
  },
  {
    status: "damaged",
    label: "Damaged",
    icon: <AlertTriangle className="w-3 h-3" />,
    className: "text-red-700 bg-red-100 hover:bg-red-200",
  },
];

interface CargoItemStatusButtonsProps {
  itemId: string;
  currentStatus: CargoItemStatus;
  onUpdateStatus: (itemId: string, newStatus: CargoItemStatus) => Promise<void>;
}

function CargoItemStatusButtons({
  itemId,
  currentStatus,
  onUpdateStatus,
}: CargoItemStatusButtonsProps) {
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
