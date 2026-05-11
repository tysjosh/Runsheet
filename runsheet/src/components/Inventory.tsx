import {
  AlertTriangle,
  ArrowDownCircle,
  ArrowUpCircle,
  Clock,
  Filter,
  Package,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { apiService, type InventoryItem } from "../services/api";
import {
  adjustStock,
  type CreateInventoryItemPayload,
  createItem,
  deleteItem,
  getAlerts,
  getItemHistory,
  getSummary,
  type InventoryItem as InvApiItem,
  type InventoryCategory,
  type InventorySummary,
  type StockAdjustment,
  type StockMovementEvent,
} from "../services/inventoryApi";

import LoadingSpinner from "./LoadingSpinner";
import {
  Badge,
  type BadgeVariant,
  Button,
  type Column,
  EmptyState,
  FilterBar,
  PageHeader,
  Pagination,
  StatsBar,
  Table,
} from "./ui";

const INVENTORY_STATUSES: { value: InventoryItem["status"]; label: string }[] =
  [
    { value: "in_stock", label: "In Stock" },
    { value: "low_stock", label: "Low Stock" },
    { value: "out_of_stock", label: "Out of Stock" },
  ];

export default function Inventory() {
  const [inventory, setInventory] = useState<InventoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [filterCategory, setFilterCategory] = useState("all");
  const [filterStatus, setFilterStatus] = useState("all");
  const [page, setPage] = useState(1);
  const [editingItem, setEditingItem] = useState<InventoryItem | null>(null);
  const [adjustingItem, setAdjustingItem] = useState<InventoryItem | null>(
    null,
  );
  const [adjustType, setAdjustType] = useState<"restock" | "consume">(
    "restock",
  );
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [deletingItem, setDeletingItem] = useState<InventoryItem | null>(null);
  const [historyItem, setHistoryItem] = useState<InventoryItem | null>(null);

  // Dashboard state
  const [summary, setSummary] = useState<InventorySummary | null>(null);
  const [alerts, setAlerts] = useState<InvApiItem[]>([]);
  const [showAlerts, setShowAlerts] = useState(false);

  const loadInventoryData = useCallback(async () => {
    try {
      setLoading(true);
      const response = await apiService.getInventory();
      setInventory(response.data);
    } catch (error) {
      console.error("Failed to load inventory data:", error);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDashboardData = useCallback(async () => {
    try {
      const [summaryRes, alertsRes] = await Promise.allSettled([
        getSummary(),
        getAlerts(),
      ]);
      if (summaryRes.status === "fulfilled") {
        setSummary(summaryRes.value.data);
      }
      if (alertsRes.status === "fulfilled") {
        setAlerts(alertsRes.value.data);
      }
    } catch (error) {
      console.error("Failed to load dashboard data:", error);
    }
  }, []);

  useEffect(() => {
    loadInventoryData();
    loadDashboardData();
  }, [loadInventoryData, loadDashboardData]);

  const getStatusVariant = (status: string): BadgeVariant => {
    switch (status) {
      case "in_stock":
        return "success";
      case "low_stock":
        return "warning";
      case "out_of_stock":
        return "error";
      default:
        return "neutral";
    }
  };

  const getStatusText = (status: string) => {
    switch (status) {
      case "in_stock":
        return "In Stock";
      case "low_stock":
        return "Low Stock";
      case "out_of_stock":
        return "Out of Stock";
      default:
        return status;
    }
  };

  const PAGE_SIZE = 20;

  const filteredInventory = inventory.filter((item) => {
    const matchesSearch =
      item.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      item.category.toLowerCase().includes(searchTerm.toLowerCase());
    const matchesCategory =
      filterCategory === "all" ||
      item.category.toLowerCase() === filterCategory;
    const matchesStatus =
      filterStatus === "all" || item.status === filterStatus;
    return matchesSearch && matchesCategory && matchesStatus;
  });

  const totalPages = Math.max(
    1,
    Math.ceil(filteredInventory.length / PAGE_SIZE),
  );
  const paginatedInventory = filteredInventory.slice(
    (page - 1) * PAGE_SIZE,
    page * PAGE_SIZE,
  );
  const inventoryColumns: Column<InventoryItem>[] = [
    {
      key: "item",
      label: "Item",
      render: (item) => (
        <div>
          <div className="font-medium text-primary">{item.name}</div>
          <div className="text-sm text-gray-500">{item.id}</div>
        </div>
      ),
    },
    {
      key: "category",
      label: "Category",
      render: (item) => (
        <span className="text-sm text-gray-700">{item.category}</span>
      ),
    },
    {
      key: "location",
      label: "Location",
      render: (item) => (
        <span className="text-sm text-gray-700">{item.location}</span>
      ),
    },
    {
      key: "quantity",
      label: "Quantity",
      render: (item) => (
        <div className="text-sm font-semibold text-primary">
          {item.quantity.toLocaleString()} {item.unit}
        </div>
      ),
    },
    {
      key: "status",
      label: "Status",
      render: (item) => (
        <Badge variant={getStatusVariant(item.status)} size="sm">
          {getStatusText(item.status)}
        </Badge>
      ),
    },
    {
      key: "lastUpdated",
      label: "Last Updated",
      render: (item) => (
        <span className="text-sm text-gray-600">
          {new Date(item.lastUpdated).toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
            year: "numeric",
          })}
        </span>
      ),
    },
    {
      key: "actions",
      label: "Actions",
      render: (item) => (
        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant="success"
            size="sm"
            icon={<ArrowUpCircle className="w-3 h-3" />}
            onClick={() => {
              setAdjustingItem(item);
              setAdjustType("restock");
            }}
            aria-label={`Restock ${item.name}`}
            title="Restock"
          />
          <Button
            type="button"
            variant="warning"
            size="sm"
            icon={<ArrowDownCircle className="w-3 h-3" />}
            onClick={() => {
              setAdjustingItem(item);
              setAdjustType("consume");
            }}
            aria-label={`Consume ${item.name}`}
            title="Consume"
          />
          <Button
            type="button"
            variant="secondary"
            size="sm"
            icon={<Pencil className="w-3 h-3" />}
            onClick={() => setEditingItem(item)}
            aria-label={`Edit ${item.name}`}
            title="Edit"
          />
          <Button
            type="button"
            variant="secondary"
            size="sm"
            icon={<Clock className="w-3 h-3" />}
            onClick={() => setHistoryItem(item)}
            aria-label={`View history for ${item.name}`}
            title="History"
          />
          <Button
            type="button"
            variant="danger"
            size="sm"
            icon={<Trash2 className="w-3 h-3" />}
            onClick={() => setDeletingItem(item)}
            aria-label={`Delete ${item.name}`}
            title="Delete"
          />
        </div>
      ),
    },
  ];

  const categories = [
    "all",
    ...Array.from(
      new Set(inventory.map((item) => item.category.toLowerCase())),
    ),
  ];

  const handleEditSaved = (updatedItem: InventoryItem) => {
    setInventory((prev) =>
      prev.map((item) => (item.id === updatedItem.id ? updatedItem : item)),
    );
    setEditingItem(null);
  };

  const handleAdjustComplete = () => {
    setAdjustingItem(null);
    loadInventoryData();
    loadDashboardData();
  };

  const handleCreateComplete = () => {
    setShowCreateModal(false);
    loadInventoryData();
    loadDashboardData();
  };

  const handleDeleteConfirm = async () => {
    if (!deletingItem) return;
    try {
      await deleteItem(deletingItem.id);
      setDeletingItem(null);
      loadInventoryData();
      loadDashboardData();
    } catch (error) {
      console.error("Failed to delete item:", error);
    }
  };

  if (loading) {
    return <LoadingSpinner message="Loading inventory..." />;
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <PageHeader
        title="Inventory Management"
        subtitle="Track and manage inventory levels"
        icon={<Package className="w-5 h-5" />}
        badge={
          alerts.length > 0 ? (
            <Button
              type="button"
              variant="warning"
              size="sm"
              onClick={() => setShowAlerts(!showAlerts)}
              icon={<AlertTriangle className="w-4 h-4" />}
            >
              {alerts.length} Alert{alerts.length !== 1 ? "s" : ""}
            </Button>
          ) : undefined
        }
        actions={
          <Button
            variant="primary"
            size="md"
            icon={<Plus className="w-4 h-4" />}
            onClick={() => setShowCreateModal(true)}
          >
            Add Item
          </Button>
        }
      />

      {/* Search and Filters */}
      <FilterBar
        searchPlaceholder="Search inventory..."
        searchValue={searchTerm}
        onSearchChange={(value) => {
          setSearchTerm(value);
          setPage(1);
        }}
        filters={
          <>
            <div className="relative">
              <Filter className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
              <select
                value={filterCategory}
                onChange={(e) => {
                  setFilterCategory(e.target.value);
                  setPage(1);
                }}
                className="pl-10 pr-8 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
                aria-label="Category"
              >
                {categories.map((category) => (
                  <option key={category} value={category}>
                    {category === "all"
                      ? "All Categories"
                      : category.charAt(0).toUpperCase() + category.slice(1)}
                  </option>
                ))}
              </select>
            </div>
            <select
              value={filterStatus}
              onChange={(e) => {
                setFilterStatus(e.target.value);
                setPage(1);
              }}
              className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
              aria-label="Status"
            >
              <option value="all">All Statuses</option>
              {INVENTORY_STATUSES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </>
        }
      />

      {/* Summary Stats Bar */}
      <StatsBar
        variant="grid"
        stats={[
          {
            label: "Total Items",
            value: summary?.total_items ?? inventory.length,
          },
          {
            label: "In Stock",
            value:
              summary?.in_stock ??
              inventory.filter((i) => i.status === "in_stock").length,
            color: "success",
          },
          {
            label: "Low Stock",
            value:
              summary?.low_stock ??
              inventory.filter((i) => i.status === "low_stock").length,
            color: "warning",
          },
          {
            label: "Out of Stock",
            value:
              summary?.out_of_stock ??
              inventory.filter((i) => i.status === "out_of_stock").length,
            color: "error",
          },
          {
            label: "Total Value",
            value:
              summary?.total_value != null
                ? `$${summary.total_value.toLocaleString()}`
                : "—",
          },
        ]}
      />

      {/* Alerts Panel (collapsible) */}
      {showAlerts && alerts.length > 0 && (
        <div className="border-b border-warning-light bg-warning-light px-8 py-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-warning-dark">
              Low Stock Alerts
            </h3>
            <button
              onClick={() => setShowAlerts(false)}
              className="text-warning hover:text-warning-dark"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
          <div className="space-y-2 max-h-40 overflow-y-auto">
            {alerts.map((alert) => (
              <div
                key={alert.item_id}
                className="flex items-center justify-between bg-white rounded-lg px-3 py-2 border border-warning-light"
              >
                <div>
                  <span className="text-sm font-medium text-gray-800">
                    {alert.name}
                  </span>
                  <span className="text-xs text-gray-500 ml-2">
                    {alert.category} · {alert.location}
                  </span>
                </div>
                <Badge variant={getStatusVariant(alert.status)} size="sm">
                  {alert.quantity} / {alert.min_threshold} min
                </Badge>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        <Table
          columns={inventoryColumns}
          data={paginatedInventory}
          getRowId={(item) => item.id}
          emptyState={
            <EmptyState
              icon={<Package />}
              title="No inventory items found"
              description="Try adjusting your search or filter criteria"
            />
          }
        />
        <Pagination
          currentPage={page}
          totalPages={totalPages}
          totalItems={filteredInventory.length}
          onPageChange={setPage}
        />
      </div>

      {/* Edit Inventory Modal */}
      {editingItem && (
        <EditInventoryModal
          item={editingItem}
          onClose={() => setEditingItem(null)}
          onSaved={handleEditSaved}
        />
      )}

      {/* Stock Adjustment Modal */}
      {adjustingItem && (
        <StockAdjustmentModal
          item={adjustingItem}
          type={adjustType}
          onClose={() => setAdjustingItem(null)}
          onComplete={handleAdjustComplete}
        />
      )}

      {/* Create Item Modal */}
      {showCreateModal && (
        <CreateInventoryModal
          onClose={() => setShowCreateModal(false)}
          onCreated={handleCreateComplete}
        />
      )}

      {/* Delete Confirmation Modal */}
      {deletingItem && (
        <DeleteConfirmModal
          item={deletingItem}
          onClose={() => setDeletingItem(null)}
          onConfirm={handleDeleteConfirm}
        />
      )}

      {/* Stock History Modal */}
      {historyItem && (
        <StockHistoryModal
          item={historyItem}
          onClose={() => setHistoryItem(null)}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Stock Adjustment Modal                                              */
/* ------------------------------------------------------------------ */

interface StockAdjustmentModalProps {
  item: InventoryItem;
  type: "restock" | "consume";
  onClose: () => void;
  onComplete: () => void;
}

function StockAdjustmentModal({
  item,
  type,
  onClose,
  onComplete,
}: StockAdjustmentModalProps) {
  const [quantity, setQuantity] = useState("");
  const [reason, setReason] = useState(
    type === "restock" ? "restock" : "used_for_maintenance",
  );
  const [referenceId, setReferenceId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const parsedQty = parseInt(quantity, 10);
    if (Number.isNaN(parsedQty) || parsedQty <= 0) {
      setError("Quantity must be a positive number.");
      return;
    }

    setError("");
    setSubmitting(true);
    try {
      const adjustment: StockAdjustment = {
        quantity_change: type === "consume" ? -parsedQty : parsedQty,
        reason,
        ...(referenceId.trim() && { reference_id: referenceId.trim() }),
      };
      await adjustStock(item.id, adjustment);
      setSuccess(true);
      setTimeout(() => {
        onComplete();
      }, 800);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to adjust stock");
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-primary">
            {type === "restock" ? "Restock Item" : "Consume Stock"}
          </h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-error bg-error-light px-3 py-2 rounded-lg">
              {error}
            </p>
          )}
          {success && (
            <p className="text-sm text-success bg-success-light px-3 py-2 rounded-lg">
              Stock adjusted successfully!
            </p>
          )}

          {/* Item info */}
          <div className="bg-gray-50 px-3 py-2 rounded-lg">
            <p className="text-sm font-medium text-primary">{item.name}</p>
            <p className="text-xs text-gray-500">
              Current: {item.quantity} {item.unit} · {item.location}
            </p>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Quantity to {type === "restock" ? "add" : "deduct"}
            </label>
            <input
              type="number"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              min="1"
              placeholder="Enter quantity"
              className={inputClass}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Reason
            </label>
            <select
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className={inputClass}
            >
              {type === "restock" ? (
                <>
                  <option value="restock">Restock</option>
                  <option value="return">Return</option>
                  <option value="correction">Correction</option>
                </>
              ) : (
                <>
                  <option value="used_for_maintenance">
                    Used for Maintenance
                  </option>
                  <option value="damaged">Damaged</option>
                  <option value="expired">Expired</option>
                  <option value="correction">Correction</option>
                </>
              )}
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Reference ID (optional)
            </label>
            <input
              type="text"
              value={referenceId}
              onChange={(e) => setReferenceId(e.target.value)}
              placeholder="e.g. PO-12345 or JOB-456"
              className={inputClass}
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || success}
              className={`px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50 ${
                type === "restock"
                  ? "bg-success hover:bg-success-dark"
                  : "bg-warning hover:bg-warning-dark"
              }`}
            >
              {submitting
                ? "Processing..."
                : type === "restock"
                  ? "Restock"
                  : "Consume"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Edit Inventory Modal                                                */
/* ------------------------------------------------------------------ */

interface EditInventoryModalProps {
  item: InventoryItem;
  onClose: () => void;
  onSaved: (updatedItem: InventoryItem) => void;
}

function EditInventoryModal({
  item,
  onClose,
  onSaved,
}: EditInventoryModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    quantity: String(item.quantity),
    status: item.status,
    location: item.location,
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const parsedQuantity = parseInt(form.quantity, 10);
    if (Number.isNaN(parsedQuantity) || parsedQuantity < 0) {
      setError("Quantity must be a non-negative number.");
      return;
    }
    if (!form.location.trim()) {
      setError("Location is required.");
      return;
    }

    setError("");
    setSubmitting(true);
    try {
      const response = await apiService.updateInventoryItem(item.id, {
        quantity: parsedQuantity,
        status: form.status,
        location: form.location.trim(),
      });
      onSaved(response.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to update inventory item",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-primary">
            Edit Inventory Item
          </h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-error bg-error-light px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          {/* Read-only item info */}
          <div className="bg-gray-50 px-3 py-2 rounded-lg">
            <p className="text-sm font-medium text-primary">{item.name}</p>
            <p className="text-xs text-gray-500">
              {item.id} · {item.category}
            </p>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Quantity ({item.unit})
            </label>
            <input
              type="number"
              value={form.quantity}
              onChange={(e) => setForm({ ...form, quantity: e.target.value })}
              min="0"
              className={inputClass}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Status
            </label>
            <select
              value={form.status}
              onChange={(e) =>
                setForm({
                  ...form,
                  status: e.target.value as InventoryItem["status"],
                })
              }
              className={inputClass}
            >
              {INVENTORY_STATUSES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Location
            </label>
            <input
              type="text"
              value={form.location}
              onChange={(e) => setForm({ ...form, location: e.target.value })}
              placeholder="e.g. Warehouse A"
              className={inputClass}
              required
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <Button type="button" onClick={onClose} variant="ghost">
              Cancel
            </Button>
            <Button type="submit" disabled={submitting} loading={submitting}>
              {submitting ? "Saving..." : "Save Changes"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Create Inventory Item Modal                                         */
/* ------------------------------------------------------------------ */

const CATEGORY_OPTIONS: { value: InventoryCategory; label: string }[] = [
  { value: "tires", label: "Tires" },
  { value: "engine_parts", label: "Engine Parts" },
  { value: "brake_parts", label: "Brake Parts" },
  { value: "fluids", label: "Fluids" },
  { value: "filters", label: "Filters" },
  { value: "electrical", label: "Electrical" },
  { value: "fuel_equipment", label: "Fuel Equipment" },
  { value: "safety", label: "Safety" },
  { value: "general", label: "General" },
];

interface CreateInventoryModalProps {
  onClose: () => void;
  onCreated: () => void;
}

function CreateInventoryModal({
  onClose,
  onCreated,
}: CreateInventoryModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    name: "",
    category: "general" as InventoryCategory,
    quantity: "0",
    unit: "pieces",
    min_threshold: "5",
    max_capacity: "100",
    location: "",
    unit_cost: "",
    supplier: "",
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.name.trim()) {
      setError("Name is required.");
      return;
    }
    if (!form.location.trim()) {
      setError("Location is required.");
      return;
    }

    setError("");
    setSubmitting(true);
    try {
      const payload: CreateInventoryItemPayload = {
        name: form.name.trim(),
        category: form.category,
        quantity: parseInt(form.quantity, 10) || 0,
        unit: form.unit.trim(),
        min_threshold: parseInt(form.min_threshold, 10) || 0,
        max_capacity: parseInt(form.max_capacity, 10) || 100,
        location: form.location.trim(),
        unit_cost: form.unit_cost ? parseFloat(form.unit_cost) : null,
        supplier: form.supplier.trim() || null,
      };
      await createItem(payload);
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create item");
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-primary">
            Add Inventory Item
          </h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-error bg-error-light px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Name *
            </label>
            <input
              type="text"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="e.g. Bridgestone R260 Tire"
              className={inputClass}
              required
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Category *
              </label>
              <select
                value={form.category}
                onChange={(e) =>
                  setForm({
                    ...form,
                    category: e.target.value as InventoryCategory,
                  })
                }
                className={inputClass}
              >
                {CATEGORY_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Unit *
              </label>
              <input
                type="text"
                value={form.unit}
                onChange={(e) => setForm({ ...form, unit: e.target.value })}
                placeholder="pieces, liters, sets"
                className={inputClass}
                required
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Location *
            </label>
            <input
              type="text"
              value={form.location}
              onChange={(e) => setForm({ ...form, location: e.target.value })}
              placeholder="e.g. Houston Terminal, Dallas Depot"
              className={inputClass}
              required
            />
          </div>

          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Initial Qty
              </label>
              <input
                type="number"
                value={form.quantity}
                onChange={(e) => setForm({ ...form, quantity: e.target.value })}
                min="0"
                className={inputClass}
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Min Threshold
              </label>
              <input
                type="number"
                value={form.min_threshold}
                onChange={(e) =>
                  setForm({ ...form, min_threshold: e.target.value })
                }
                min="0"
                className={inputClass}
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Max Capacity
              </label>
              <input
                type="number"
                value={form.max_capacity}
                onChange={(e) =>
                  setForm({ ...form, max_capacity: e.target.value })
                }
                min="1"
                className={inputClass}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Unit Cost
              </label>
              <input
                type="number"
                value={form.unit_cost}
                onChange={(e) =>
                  setForm({ ...form, unit_cost: e.target.value })
                }
                min="0"
                step="0.01"
                placeholder="Optional"
                className={inputClass}
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Supplier
              </label>
              <input
                type="text"
                value={form.supplier}
                onChange={(e) => setForm({ ...form, supplier: e.target.value })}
                placeholder="Optional"
                className={inputClass}
              />
            </div>
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <Button type="button" onClick={onClose} variant="ghost">
              Cancel
            </Button>
            <Button type="submit" disabled={submitting} loading={submitting}>
              {submitting ? "Creating..." : "Create Item"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Delete Confirmation Modal                                           */
/* ------------------------------------------------------------------ */

interface DeleteConfirmModalProps {
  item: InventoryItem;
  onClose: () => void;
  onConfirm: () => void;
}

function DeleteConfirmModal({
  item,
  onClose,
  onConfirm,
}: DeleteConfirmModalProps) {
  const [submitting, setSubmitting] = useState(false);

  const handleConfirm = async () => {
    setSubmitting(true);
    await onConfirm();
    setSubmitting(false);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-sm mx-4">
        <div className="px-6 py-5">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 bg-error-light rounded-full flex items-center justify-center">
              <Trash2 className="w-5 h-5 text-error" />
            </div>
            <h2 className="text-lg font-semibold text-primary">Delete Item</h2>
          </div>
          <p className="text-sm text-gray-600 mb-1">
            Are you sure you want to delete this inventory item?
          </p>
          <div className="bg-gray-50 px-3 py-2 rounded-lg mt-3">
            <p className="text-sm font-medium text-primary">{item.name}</p>
            <p className="text-xs text-gray-500">
              {item.id} · {item.category} · {item.location}
            </p>
          </div>
          <p className="text-xs text-error mt-3">
            This action cannot be undone.
          </p>
        </div>
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-gray-100">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={submitting}
            className="px-4 py-2 text-sm text-white bg-error rounded-lg hover:bg-error-dark disabled:opacity-50"
          >
            {submitting ? "Deleting..." : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Stock History Modal                                                  */
/* ------------------------------------------------------------------ */

interface StockHistoryModalProps {
  item: InventoryItem;
  onClose: () => void;
}

function StockHistoryModal({ item, onClose }: StockHistoryModalProps) {
  const [events, setEvents] = useState<StockMovementEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError("");
      try {
        const result = await getItemHistory(item.id, 1, 50);
        if (!cancelled) {
          setEvents(result.data ?? []);
        }
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Failed to load history",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [item.id]);

  const historyColumns: Column<StockMovementEvent>[] = [
    {
      key: "timestamp",
      label: "Timestamp",
      render: (event) => (
        <span className="text-xs text-gray-600">
          {new Date(event.event_timestamp).toLocaleString()}
        </span>
      ),
    },
    {
      key: "change",
      label: "Change",
      render: (event) => (
        <span
          className={`text-xs font-semibold ${
            event.quantity_change > 0
              ? "text-success"
              : event.quantity_change < 0
                ? "text-error"
                : "text-gray-600"
          }`}
        >
          {event.quantity_change > 0 ? "+" : ""}
          {event.quantity_change}
        </span>
      ),
    },
    {
      key: "reason",
      label: "Reason",
      render: (event) => (
        <span className="text-xs text-gray-600">{event.reason}</span>
      ),
    },
    {
      key: "reference",
      label: "Reference",
      render: (event) => (
        <span className="text-xs text-gray-500 font-mono">
          {event.reference_id || "-"}
        </span>
      ),
    },
    {
      key: "quantityAfter",
      label: "Result Qty",
      render: (event) => (
        <span className="text-xs text-gray-700 font-medium">
          {event.quantity_after}
        </span>
      ),
    },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl mx-4 max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <div>
            <h2 className="text-lg font-semibold text-primary">
              Stock History
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {item.name} — Current qty: {item.quantity} {item.unit}
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
            aria-label="Close history modal"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {loading && (
            <div className="flex items-center justify-center py-16">
              <div className="w-6 h-6 border-2 border-gray-300 border-t-primary rounded-full animate-spin" />
            </div>
          )}

          {error && (
            <p className="text-sm text-error bg-error-light px-4 py-3 rounded-lg">
              {error}
            </p>
          )}

          {!loading && !error && events.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 text-gray-400">
              <Clock className="w-8 h-8 mb-2" />
              <p className="text-sm">No stock movements recorded</p>
            </div>
          )}

          {!loading && !error && events.length > 0 && (
            <Table
              variant="compact"
              columns={historyColumns}
              data={events}
              getRowId={(event) => event.event_id}
            />
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end px-6 py-4 border-t border-gray-100">
          <Button onClick={onClose}>Close</Button>
        </div>
      </div>
    </div>
  );
}
