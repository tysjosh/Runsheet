"use client";

import { ClipboardList, Filter, Plus, Search, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { apiService, type Order } from "../../services/api";
import LoadingSpinner from "../LoadingSpinner";

const ORDER_STATUSES: { value: string; label: string }[] = [
  { value: "all", label: "All Status" },
  { value: "pending", label: "Pending" },
  { value: "in_transit", label: "In Transit" },
  { value: "delivered", label: "Delivered" },
  { value: "cancelled", label: "Cancelled" },
];

const ORDER_PRIORITIES: { value: string; label: string }[] = [
  { value: "all", label: "All Priorities" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "urgent", label: "Urgent" },
];

const STATUS_TRANSITIONS: Record<string, string[]> = {
  pending: ["in_transit", "cancelled"],
  in_transit: ["delivered", "cancelled"],
  delivered: [],
  cancelled: [],
};

/**
 * OrdersPage — full orders management page.
 *
 * Summary bar, data table, search, filters, create modal, inline status updates.
 *
 * Validates: Requirements 1.1–1.9, 12.1–12.4
 */
export default function OrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [priorityFilter, setPriorityFilter] = useState("all");
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [actionError, setActionError] = useState<Record<string, string>>({});

  const loadOrders = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await apiService.getOrders();
      setOrders(response.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load orders");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadOrders();
  }, [loadOrders]);

  const handleStatusUpdate = async (orderId: string, newStatus: string) => {
    setActionError((prev) => {
      const next = { ...prev };
      delete next[orderId];
      return next;
    });
    try {
      const response = await apiService.updateOrderStatus(orderId, newStatus);
      setOrders((prev) =>
        prev.map((o) => (o.id === orderId ? response.data : o)),
      );
    } catch (err) {
      setActionError((prev) => ({
        ...prev,
        [orderId]: err instanceof Error ? err.message : "Failed to update status",
      }));
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "pending":
        return "text-yellow-700 bg-yellow-50";
      case "in_transit":
        return "text-blue-700 bg-blue-50";
      case "delivered":
        return "text-green-700 bg-green-50";
      case "cancelled":
        return "text-red-700 bg-red-50";
      default:
        return "text-gray-700 bg-gray-50";
    }
  };

  const getPriorityColor = (priority: string) => {
    switch (priority) {
      case "urgent":
        return "text-red-700 bg-red-50";
      case "high":
        return "text-orange-700 bg-orange-50";
      case "medium":
        return "text-yellow-700 bg-yellow-50";
      case "low":
        return "text-gray-700 bg-gray-50";
      default:
        return "text-gray-700 bg-gray-50";
    }
  };

  const getStatusText = (status: string) => {
    return status
      .replace("_", " ")
      .split(" ")
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(" ");
  };

  const filteredOrders = orders.filter((order) => {
    const matchesSearch =
      order.customer.toLowerCase().includes(searchTerm.toLowerCase()) ||
      order.id.toLowerCase().includes(searchTerm.toLowerCase()) ||
      order.items.toLowerCase().includes(searchTerm.toLowerCase());
    const matchesStatus =
      statusFilter === "all" || order.status === statusFilter;
    const matchesPriority =
      priorityFilter === "all" || order.priority === priorityFilter;
    return matchesSearch && matchesStatus && matchesPriority;
  });

  if (loading) {
    return <LoadingSpinner message="Loading orders..." />;
  }

  if (error && orders.length === 0) {
    return (
      <div className="h-full flex items-center justify-center bg-white">
        <div className="text-center">
          <div className="bg-red-50 text-red-700 px-6 py-4 rounded-xl mb-4 max-w-md">
            <p className="text-sm font-medium">Failed to load orders</p>
            <p className="text-sm mt-1">{error}</p>
          </div>
          <button
            onClick={loadOrders}
            className="px-4 py-2 text-sm text-white rounded-lg hover:opacity-90"
            style={{ backgroundColor: "#232323" }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
              <ClipboardList className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-2xl font-semibold text-[#232323]">
                Orders Management
              </h1>
              <p className="text-gray-500">View, create, and manage customer orders</p>
            </div>
          </div>
          <button
            onClick={() => setShowCreateModal(true)}
            className="flex items-center gap-2 px-4 py-2 text-sm text-white rounded-lg transition-colors hover:opacity-90"
            style={{ backgroundColor: "#232323" }}
          >
            <Plus className="w-4 h-4" />
            Create Order
          </button>
        </div>

        {/* Search and Filters */}
        <div className="flex gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              type="text"
              placeholder="Search by customer, order ID, or items..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full pl-10 pr-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
            />
          </div>
          <div className="relative">
            <Filter className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="pl-10 pr-8 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white min-w-[140px]"
            >
              {ORDER_STATUSES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
          <select
            value={priorityFilter}
            onChange={(e) => setPriorityFilter(e.target.value)}
            className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white min-w-[140px]"
          >
            {ORDER_PRIORITIES.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Summary Bar */}
      <div className="border-b border-gray-100 px-8 py-4">
        <div className="grid grid-cols-4 gap-6">
          <div className="text-center">
            <div className="text-2xl font-semibold text-[#232323]">
              {orders.length}
            </div>
            <div className="text-sm text-gray-500">Total Orders</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-semibold text-yellow-600">
              {orders.filter((o) => o.status === "pending").length}
            </div>
            <div className="text-sm text-gray-500">Pending</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-semibold text-blue-600">
              {orders.filter((o) => o.status === "in_transit").length}
            </div>
            <div className="text-sm text-gray-500">In Transit</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-semibold text-green-600">
              {orders.filter((o) => o.status === "delivered").length}
            </div>
            <div className="text-sm text-gray-500">Delivered</div>
          </div>
        </div>
      </div>

      {/* Data Table */}
      <div className="flex-1 overflow-y-auto">
        <table className="w-full">
          <thead className="bg-gray-50 sticky top-0 border-b border-gray-100">
            <tr>
              <th className="px-8 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Order ID
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Customer
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Items
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Value
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Priority
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Status
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Region
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Created Date
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Delivery ETA
              </th>
              <th className="px-6 py-4 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {filteredOrders.map((order) => (
              <tr key={order.id} className="hover:bg-gray-50 transition-colors">
                <td className="px-8 py-4">
                  <div className="font-medium text-[#232323]">{order.id}</div>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-[#232323]">{order.customer}</span>
                </td>
                <td className="px-6 py-4">
                  <div className="text-sm text-[#232323] line-clamp-1">{order.items}</div>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm font-semibold text-[#232323]">
                    ${order.value.toLocaleString()}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <span
                    className={`inline-flex items-center px-3 py-1 rounded-lg text-xs font-medium ${getPriorityColor(order.priority)}`}
                  >
                    {order.priority.charAt(0).toUpperCase() + order.priority.slice(1)}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <span
                    className={`inline-flex items-center px-3 py-1 rounded-lg text-xs font-medium ${getStatusColor(order.status)}`}
                  >
                    {getStatusText(order.status)}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-gray-700">{order.region}</span>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-gray-600">
                    {new Date(order.createdAt).toLocaleDateString("en-US", {
                      month: "short",
                      day: "numeric",
                      year: "numeric",
                    })}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-gray-600">
                    {order.deliveryEta
                      ? new Date(order.deliveryEta).toLocaleDateString("en-US", {
                          month: "short",
                          day: "numeric",
                          year: "numeric",
                        })
                      : "—"}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <div className="flex flex-col gap-1">
                    {(STATUS_TRANSITIONS[order.status] || []).length > 0 ? (
                      <div className="flex gap-1">
                        {STATUS_TRANSITIONS[order.status].map((targetStatus) => (
                          <button
                            key={targetStatus}
                            onClick={() => handleStatusUpdate(order.id, targetStatus)}
                            className="px-2 py-1 text-xs rounded-md border border-gray-200 text-gray-600 hover:bg-gray-100 transition-colors whitespace-nowrap"
                          >
                            {getStatusText(targetStatus)}
                          </button>
                        ))}
                      </div>
                    ) : (
                      <span className="text-xs text-gray-400">—</span>
                    )}
                    {actionError[order.id] && (
                      <p className="text-xs text-red-600">{actionError[order.id]}</p>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {filteredOrders.length === 0 && (
          <div className="text-center py-16 text-gray-500">
            <ClipboardList className="w-16 h-16 mx-auto mb-4 text-gray-300" />
            <p className="text-lg font-medium text-gray-400">
              No orders found
            </p>
            <p className="text-sm text-gray-400 mt-1">
              Try adjusting your search or filter criteria
            </p>
          </div>
        )}
      </div>

      {/* Create Order Modal */}
      {showCreateModal && (
        <CreateOrderModal
          onClose={() => setShowCreateModal(false)}
          onCreated={(order) => setOrders((prev) => [order, ...prev])}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Create Order Modal                                                  */
/* ------------------------------------------------------------------ */

interface CreateOrderModalProps {
  onClose: () => void;
  onCreated: (order: Order) => void;
}

function CreateOrderModal({ onClose, onCreated }: CreateOrderModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    customer: "",
    items: "",
    value: "",
    region: "",
    priority: "medium" as Order["priority"],
    status: "pending" as Order["status"],
    deliveryEta: "",
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.customer || !form.items || !form.value || !form.region) {
      setError("Customer, items, value, and region are required.");
      return;
    }
    const parsedValue = parseFloat(form.value);
    if (isNaN(parsedValue) || parsedValue <= 0) {
      setError("Value must be a positive number.");
      return;
    }
    setError("");
    setSubmitting(true);
    try {
      const response = await apiService.createOrder({
        customer: form.customer,
        items: form.items,
        value: parsedValue,
        region: form.region,
        priority: form.priority,
        status: form.status,
        deliveryEta: form.deliveryEta || new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString(),
      });
      onCreated(response.data);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create order");
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
          <h2 className="text-lg font-semibold text-[#232323]">Create Order</h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {error && (
            <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Customer
            </label>
            <input
              type="text"
              value={form.customer}
              onChange={(e) => setForm({ ...form, customer: e.target.value })}
              placeholder="e.g. Acme Corp"
              className={inputClass}
              required
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Items
            </label>
            <input
              type="text"
              value={form.items}
              onChange={(e) => setForm({ ...form, items: e.target.value })}
              placeholder="e.g. 500L Diesel, 200L Petrol"
              className={inputClass}
              required
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Value ($)
              </label>
              <input
                type="number"
                value={form.value}
                onChange={(e) => setForm({ ...form, value: e.target.value })}
                placeholder="e.g. 5000"
                min="0"
                step="0.01"
                className={inputClass}
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Region
              </label>
              <input
                type="text"
                value={form.region}
                onChange={(e) => setForm({ ...form, region: e.target.value })}
                placeholder="e.g. Lagos"
                className={inputClass}
                required
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Priority
            </label>
            <select
              value={form.priority}
              onChange={(e) =>
                setForm({ ...form, priority: e.target.value as Order["priority"] })
              }
              className={inputClass}
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="urgent">Urgent</option>
            </select>
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
              disabled={submitting}
              className="px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50"
              style={{ backgroundColor: "#232323" }}
            >
              {submitting ? "Creating..." : "Create Order"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
