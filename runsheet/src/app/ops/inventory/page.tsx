"use client";

import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Box,
  Filter,
  Package,
  Search,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import LoadingSpinner from "../../../components/LoadingSpinner";
import { useInventoryWebSocket } from "../../../hooks/useInventoryWebSocket";
import type {
  InventoryCategory,
  InventoryFilters,
  InventoryItem,
  InventoryStatus,
  InventorySummary,
} from "../../../services/inventoryApi";
import {
  adjustStock,
  getAlerts,
  getItems,
  getSummary,
} from "../../../services/inventoryApi";

// ─── Filter Options ──────────────────────────────────────────────────────────

const CATEGORY_OPTIONS: { value: "" | InventoryCategory; label: string }[] = [
  { value: "", label: "All Categories" },
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

const STATUS_OPTIONS: { value: "" | InventoryStatus; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "in_stock", label: "In Stock" },
  { value: "low_stock", label: "Low Stock" },
  { value: "out_of_stock", label: "Out of Stock" },
  { value: "on_order", label: "On Order" },
];

const EMPTY_SUMMARY: InventorySummary = {
  total_items: 0,
  total_value: 0,
  in_stock: 0,
  low_stock: 0,
  out_of_stock: 0,
  on_order: 0,
  categories: {},
};

// ─── Helper Functions ────────────────────────────────────────────────────────

function getStatusColor(status: string): string {
  switch (status) {
    case "in_stock":
      return "text-green-700 bg-green-50 border-green-200";
    case "low_stock":
      return "text-yellow-700 bg-yellow-50 border-yellow-200";
    case "out_of_stock":
      return "text-red-700 bg-red-50 border-red-200";
    case "on_order":
      return "text-blue-700 bg-blue-50 border-blue-200";
    default:
      return "text-gray-700 bg-gray-50 border-gray-200";
  }
}

function getStatusLabel(status: string): string {
  switch (status) {
    case "in_stock":
      return "In Stock";
    case "low_stock":
      return "Low Stock";
    case "out_of_stock":
      return "Out of Stock";
    case "on_order":
      return "On Order";
    default:
      return status;
  }
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat("en-NG", {
    style: "currency",
    currency: "NGN",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

function formatCategory(category: string): string {
  return category
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

// ─── Stock Adjustment Modal ──────────────────────────────────────────────────

interface AdjustmentModalProps {
  item: InventoryItem;
  mode: "restock" | "consume";
  onClose: () => void;
  onConfirm: (quantity: number, reason: string) => void;
  isSubmitting: boolean;
}

function StockAdjustmentModal({
  item,
  mode,
  onClose,
  onConfirm,
  isSubmitting,
}: AdjustmentModalProps) {
  const [quantity, setQuantity] = useState(1);
  const [reason, setReason] = useState(
    mode === "restock" ? "manual_restock" : "manual_consumption",
  );

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const adjustedQuantity = mode === "restock" ? quantity : -quantity;
    onConfirm(adjustedQuantity, reason);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="adjustment-modal-title"
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4 p-6">
        <h2
          id="adjustment-modal-title"
          className="text-lg font-semibold text-[#232323] mb-4"
        >
          {mode === "restock" ? "Restock Item" : "Consume Stock"}
        </h2>

        <div className="mb-4 p-3 bg-gray-50 rounded-lg">
          <p className="text-sm font-medium text-gray-900">{item.name}</p>
          <p className="text-xs text-gray-500">
            Current: {item.quantity} {item.unit} · Location: {item.location}
          </p>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="mb-4">
            <label
              htmlFor="adjustment-quantity"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Quantity ({item.unit})
            </label>
            <input
              id="adjustment-quantity"
              type="number"
              min={1}
              max={mode === "consume" ? item.quantity : item.max_capacity - item.quantity}
              value={quantity}
              onChange={(e) => setQuantity(Math.max(1, parseInt(e.target.value) || 1))}
              className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
              aria-describedby="quantity-help"
            />
            <p id="quantity-help" className="mt-1 text-xs text-gray-500">
              {mode === "restock"
                ? `Max capacity: ${item.max_capacity} ${item.unit}`
                : `Available: ${item.quantity} ${item.unit}`}
            </p>
          </div>

          <div className="mb-6">
            <label
              htmlFor="adjustment-reason"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Reason
            </label>
            <input
              id="adjustment-reason"
              type="text"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            />
          </div>

          <div className="flex gap-3 justify-end">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
              disabled={isSubmitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting || quantity < 1}
              className={`px-4 py-2 text-sm font-medium text-white rounded-lg transition-colors disabled:opacity-50 ${
                mode === "restock"
                  ? "bg-green-600 hover:bg-green-700"
                  : "bg-orange-600 hover:bg-orange-700"
              }`}
            >
              {isSubmitting
                ? "Processing..."
                : mode === "restock"
                  ? "Restock"
                  : "Consume"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Summary Bar Component ───────────────────────────────────────────────────

interface SummaryBarProps {
  summary: InventorySummary;
}

function SummaryBar({ summary }: SummaryBarProps) {
  const stats = [
    {
      label: "Total Items",
      value: summary.total_items.toLocaleString(),
      color: "text-[#232323]",
      bgColor: "bg-gray-50",
    },
    {
      label: "Total Value",
      value: formatCurrency(summary.total_value),
      color: "text-[#232323]",
      bgColor: "bg-gray-50",
    },
    {
      label: "In Stock",
      value: summary.in_stock.toLocaleString(),
      color: "text-green-700",
      bgColor: "bg-green-50",
    },
    {
      label: "Low Stock",
      value: summary.low_stock.toLocaleString(),
      color: "text-yellow-700",
      bgColor: "bg-yellow-50",
    },
    {
      label: "Out of Stock",
      value: summary.out_of_stock.toLocaleString(),
      color: "text-red-700",
      bgColor: "bg-red-50",
    },
  ];

  return (
    <div
      className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3"
      role="region"
      aria-label="Inventory summary"
    >
      {stats.map((stat) => (
        <div
          key={stat.label}
          className={`rounded-lg p-3 ${stat.bgColor} border border-gray-100`}
        >
          <p className="text-xs text-gray-500 mb-0.5">{stat.label}</p>
          <p className={`text-lg font-semibold ${stat.color}`}>{stat.value}</p>
        </div>
      ))}
    </div>
  );
}

// ─── Alerts Panel Component ──────────────────────────────────────────────────

interface AlertsPanelProps {
  alerts: InventoryItem[];
  onRestock: (item: InventoryItem) => void;
}

function AlertsPanel({ alerts, onRestock }: AlertsPanelProps) {
  if (alerts.length === 0) {
    return null;
  }

  return (
    <div
      className="border border-orange-200 bg-orange-50 rounded-lg p-4"
      role="region"
      aria-label="Low stock alerts"
    >
      <div className="flex items-center gap-2 mb-3">
        <AlertTriangle
          className="w-4 h-4 text-orange-600"
          aria-hidden="true"
        />
        <h2 className="text-sm font-semibold text-orange-800">
          Low Stock Alerts ({alerts.length})
        </h2>
      </div>
      <div className="space-y-2 max-h-48 overflow-y-auto">
        {alerts.map((item) => (
          <div
            key={item.item_id}
            className="flex items-center justify-between bg-white rounded-md px-3 py-2 border border-orange-100"
          >
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-gray-900 truncate">
                {item.name}
              </p>
              <p className="text-xs text-gray-500">
                {formatCategory(item.category)} · {item.location} ·{" "}
                <span
                  className={
                    item.status === "out_of_stock"
                      ? "text-red-600 font-medium"
                      : "text-yellow-600 font-medium"
                  }
                >
                  {item.quantity}/{item.min_threshold} {item.unit}
                </span>
              </p>
            </div>
            <button
              type="button"
              onClick={() => onRestock(item)}
              className="ml-3 px-3 py-1 text-xs font-medium text-green-700 bg-green-50 border border-green-200 rounded-md hover:bg-green-100 transition-colors"
              aria-label={`Restock ${item.name}`}
            >
              Restock
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Item List Component ─────────────────────────────────────────────────────

interface ItemListProps {
  items: InventoryItem[];
  onRestock: (item: InventoryItem) => void;
  onConsume: (item: InventoryItem) => void;
}

function ItemList({ items, onRestock, onConsume }: ItemListProps) {
  if (items.length === 0) {
    return (
      <div className="text-center py-12">
        <Package className="w-12 h-12 text-gray-300 mx-auto mb-3" aria-hidden="true" />
        <p className="text-sm text-gray-500">No inventory items found</p>
        <p className="text-xs text-gray-400 mt-1">
          Try adjusting your filters
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm" aria-label="Inventory items">
        <thead>
          <tr className="border-b border-gray-100">
            <th className="text-left py-3 px-4 text-xs font-medium text-gray-500 uppercase tracking-wider">
              Item
            </th>
            <th className="text-left py-3 px-4 text-xs font-medium text-gray-500 uppercase tracking-wider">
              Category
            </th>
            <th className="text-left py-3 px-4 text-xs font-medium text-gray-500 uppercase tracking-wider">
              Location
            </th>
            <th className="text-right py-3 px-4 text-xs font-medium text-gray-500 uppercase tracking-wider">
              Quantity
            </th>
            <th className="text-center py-3 px-4 text-xs font-medium text-gray-500 uppercase tracking-wider">
              Status
            </th>
            <th className="text-right py-3 px-4 text-xs font-medium text-gray-500 uppercase tracking-wider">
              Unit Cost
            </th>
            <th className="text-right py-3 px-4 text-xs font-medium text-gray-500 uppercase tracking-wider">
              Actions
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-50">
          {items.map((item) => (
            <tr
              key={item.item_id}
              className="hover:bg-gray-50/50 transition-colors"
            >
              <td className="py-3 px-4">
                <p className="font-medium text-gray-900">{item.name}</p>
                <p className="text-xs text-gray-400">{item.item_id}</p>
              </td>
              <td className="py-3 px-4 text-gray-600">
                {formatCategory(item.category)}
              </td>
              <td className="py-3 px-4 text-gray-600">{item.location}</td>
              <td className="py-3 px-4 text-right">
                <span className="font-medium text-gray-900">
                  {item.quantity}
                </span>
                <span className="text-gray-400 ml-1">{item.unit}</span>
                <p className="text-xs text-gray-400">
                  min: {item.min_threshold}
                </p>
              </td>
              <td className="py-3 px-4 text-center">
                <span
                  className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full border ${getStatusColor(item.status)}`}
                >
                  {getStatusLabel(item.status)}
                </span>
              </td>
              <td className="py-3 px-4 text-right text-gray-600">
                {item.unit_cost != null
                  ? formatCurrency(item.unit_cost)
                  : "—"}
              </td>
              <td className="py-3 px-4 text-right">
                <div className="flex items-center justify-end gap-1">
                  <button
                    type="button"
                    onClick={() => onRestock(item)}
                    className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-green-700 bg-green-50 border border-green-200 rounded hover:bg-green-100 transition-colors"
                    aria-label={`Restock ${item.name}`}
                  >
                    <ArrowUp className="w-3 h-3" aria-hidden="true" />
                    Restock
                  </button>
                  <button
                    type="button"
                    onClick={() => onConsume(item)}
                    disabled={item.quantity === 0}
                    className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-orange-700 bg-orange-50 border border-orange-200 rounded hover:bg-orange-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    aria-label={`Consume ${item.name}`}
                  >
                    <ArrowDown className="w-3 h-3" aria-hidden="true" />
                    Consume
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Main Inventory Dashboard Page ───────────────────────────────────────────

/**
 * Inventory Dashboard page — comprehensive inventory management view.
 *
 * Displays summary bar, filterable item list, low-stock alerts panel,
 * and inline stock adjustment controls. Subscribes to inventory WebSocket
 * events for real-time updates.
 *
 * Validates: Requirements 7.3, 7.4, 7.5
 */
export default function InventoryDashboardPage() {
  // Data state
  const [items, setItems] = useState<InventoryItem[]>([]);
  const [summary, setSummary] = useState<InventorySummary>(EMPTY_SUMMARY);
  const [alerts, setAlerts] = useState<InventoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filter state
  const [categoryFilter, setCategoryFilter] = useState<"" | InventoryCategory>("");
  const [statusFilter, setStatusFilter] = useState<"" | InventoryStatus>("");
  const [locationFilter, setLocationFilter] = useState("");

  // Adjustment modal state
  const [adjustmentTarget, setAdjustmentTarget] = useState<InventoryItem | null>(null);
  const [adjustmentMode, setAdjustmentMode] = useState<"restock" | "consume">("restock");
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Toast notification state
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" | "info" } | null>(null);

  // ─── Data Loading ────────────────────────────────────────────────────────

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const filters: InventoryFilters = {};
      if (categoryFilter) filters.category = categoryFilter;
      if (statusFilter) filters.status = statusFilter;
      if (locationFilter) filters.location = locationFilter;

      const [itemsRes, summaryRes, alertsRes] = await Promise.all([
        getItems(filters),
        getSummary(),
        getAlerts(),
      ]);

      setItems(itemsRes.data);
      setSummary(summaryRes.data);
      setAlerts(alertsRes.data);
    } catch (err) {
      console.error("Failed to load inventory data:", err);
      setError("Failed to load inventory data. Please try again.");
    } finally {
      setLoading(false);
    }
  }, [categoryFilter, statusFilter, locationFilter]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // ─── WebSocket Live Updates ──────────────────────────────────────────────

  const handleInventoryAlert = useCallback(
    (alert: { item_id: string; item_name: string; status: string; location: string }) => {
      setToast({
        message: `${alert.item_name} is ${getStatusLabel(alert.status)} at ${alert.location}`,
        type: alert.status === "out_of_stock" ? "error" : "info",
      });
      // Refresh alerts and summary on alert
      getSummary()
        .then((res) => setSummary(res.data))
        .catch(() => {});
      getAlerts()
        .then((res) => setAlerts(res.data))
        .catch(() => {});
    },
    [],
  );

  const handleStockChanged = useCallback(
    (event: { item_id: string; new_quantity: number; status: string }) => {
      // Update the item in the list in real-time
      setItems((prev) =>
        prev.map((item) =>
          item.item_id === event.item_id
            ? { ...item, quantity: event.new_quantity, status: event.status as InventoryStatus }
            : item,
        ),
      );
      // Refresh summary
      getSummary()
        .then((res) => setSummary(res.data))
        .catch(() => {});
    },
    [],
  );

  useInventoryWebSocket({
    onInventoryAlert: handleInventoryAlert,
    onStockChanged: handleStockChanged,
  });

  // ─── Toast Auto-Dismiss ──────────────────────────────────────────────────

  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 5000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  // ─── Stock Adjustment Handlers ───────────────────────────────────────────

  const handleOpenRestock = useCallback((item: InventoryItem) => {
    setAdjustmentTarget(item);
    setAdjustmentMode("restock");
  }, []);

  const handleOpenConsume = useCallback((item: InventoryItem) => {
    setAdjustmentTarget(item);
    setAdjustmentMode("consume");
  }, []);

  const handleCloseAdjustment = useCallback(() => {
    setAdjustmentTarget(null);
  }, []);

  const handleConfirmAdjustment = useCallback(
    async (quantityChange: number, reason: string) => {
      if (!adjustmentTarget) return;

      try {
        setIsSubmitting(true);
        const result = await adjustStock(adjustmentTarget.item_id, {
          quantity_change: quantityChange,
          reason,
        });

        // Update item in list with new quantity and status
        setItems((prev) =>
          prev.map((item) =>
            item.item_id === adjustmentTarget.item_id
              ? {
                  ...item,
                  quantity: result.data.new_quantity,
                  status: result.data.new_status,
                }
              : item,
          ),
        );

        // Refresh summary and alerts
        const [summaryRes, alertsRes] = await Promise.all([
          getSummary(),
          getAlerts(),
        ]);
        setSummary(summaryRes.data);
        setAlerts(alertsRes.data);

        setToast({
          message: `${adjustmentTarget.name}: ${result.data.previous_quantity} → ${result.data.new_quantity} ${adjustmentTarget.unit}`,
          type: "success",
        });

        setAdjustmentTarget(null);
      } catch (err) {
        console.error("Stock adjustment failed:", err);
        setToast({
          message: "Stock adjustment failed. Please try again.",
          type: "error",
        });
      } finally {
        setIsSubmitting(false);
      }
    },
    [adjustmentTarget],
  );

  // ─── Render ──────────────────────────────────────────────────────────────

  if (loading) {
    return <LoadingSpinner message="Loading inventory dashboard..." />;
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center">
          <p className="text-red-600 mb-4">{error}</p>
          <button
            type="button"
            onClick={loadData}
            className="px-4 py-2 text-sm font-medium text-white bg-[#232323] rounded-lg hover:opacity-90 transition-opacity"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Toast Notification */}
      {toast && (
        <div
          className={`fixed top-4 right-4 z-50 max-w-sm px-4 py-3 rounded-lg shadow-lg border transition-all ${
            toast.type === "success"
              ? "bg-green-50 border-green-200 text-green-800"
              : toast.type === "error"
                ? "bg-red-50 border-red-200 text-red-800"
                : "bg-blue-50 border-blue-200 text-blue-800"
          }`}
          role="alert"
          aria-live="polite"
        >
          <p className="text-sm font-medium">{toast.message}</p>
        </div>
      )}

      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <Box className="w-5 h-5 text-white" aria-hidden="true" />
          </div>
          <div className="flex-1">
            <h1 className="text-2xl font-semibold text-[#232323]">
              Inventory Dashboard
            </h1>
            <p className="text-gray-500">
              Manage stock levels, alerts, and adjustments
            </p>
          </div>
        </div>

        {/* Filters */}
        <div
          className="flex flex-wrap items-center gap-3"
          role="search"
          aria-label="Inventory filters"
        >
          <div className="flex items-center gap-1.5 text-xs text-gray-500">
            <Filter className="w-3.5 h-3.5" aria-hidden="true" />
            <span>Filters:</span>
          </div>

          <select
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value as "" | InventoryCategory)}
            className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg bg-white focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            aria-label="Filter by category"
          >
            {CATEGORY_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>

          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as "" | InventoryStatus)}
            className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg bg-white focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            aria-label="Filter by status"
          >
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>

          <div className="relative">
            <Search
              className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
              aria-hidden="true"
            />
            <input
              type="text"
              value={locationFilter}
              onChange={(e) => setLocationFilter(e.target.value)}
              placeholder="Filter by location..."
              className="pl-8 pr-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
              aria-label="Filter by location"
            />
          </div>
        </div>
      </div>

      {/* Summary Bar */}
      <div className="border-b border-gray-100 px-8 py-4">
        <SummaryBar summary={summary} />
      </div>

      {/* Main Content */}
      <div className="flex-1 overflow-y-auto px-8 py-6 space-y-6">
        {/* Low-Stock Alerts Panel */}
        <AlertsPanel alerts={alerts} onRestock={handleOpenRestock} />

        {/* Item List */}
        <div
          className="border border-gray-100 rounded-lg overflow-hidden"
          role="region"
          aria-label="Inventory items list"
        >
          <ItemList
            items={items}
            onRestock={handleOpenRestock}
            onConsume={handleOpenConsume}
          />
        </div>
      </div>

      {/* Stock Adjustment Modal */}
      {adjustmentTarget && (
        <StockAdjustmentModal
          item={adjustmentTarget}
          mode={adjustmentMode}
          onClose={handleCloseAdjustment}
          onConfirm={handleConfirmAdjustment}
          isSubmitting={isSubmitting}
        />
      )}
    </div>
  );
}
