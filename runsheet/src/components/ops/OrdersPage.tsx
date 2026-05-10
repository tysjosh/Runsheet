"use client";

/**
 * Orders Page — lists fuel orders with filters, pagination, and
 * real-time updates via `/ws/orders`.
 *
 * Validates: Requirements 2.5.1, 8.1.3, 8.1.5
 */

import { Plus, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useOrdersWebSocket } from "../../hooks/useOrdersWebSocket";
import {
  listOrders,
  type CallType,
  type FuelOrder,
  type IntakeChannelType,
  type OrderListFilters,
  type OrderStatus,
  type PaginatedResponse,
} from "../../services/ordersApi";

// ─── Constants ───────────────────────────────────────────────────────────────

const PAGE_SIZE = 20;

const STATUS_OPTIONS: { value: OrderStatus | ""; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "placed", label: "Placed" },
  { value: "confirmed", label: "Confirmed" },
  { value: "scheduled", label: "Scheduled" },
  { value: "dispatched", label: "Dispatched" },
  { value: "in_transit", label: "In Transit" },
  { value: "delivered", label: "Delivered" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
  { value: "on_hold", label: "On Hold" },
];

const CALL_TYPE_OPTIONS: { value: CallType | ""; label: string }[] = [
  { value: "", label: "All Call Types" },
  { value: "will_call", label: "Will Call" },
  { value: "auto_fill", label: "Auto Fill" },
  { value: "keep_full", label: "Keep Full" },
  { value: "one_off", label: "One Off" },
];

const CHANNEL_OPTIONS: { value: IntakeChannelType | ""; label: string }[] = [
  { value: "", label: "All Channels" },
  { value: "voice", label: "Voice" },
  { value: "web_portal", label: "Web Portal" },
  { value: "dispatcher", label: "Dispatcher" },
  { value: "csv", label: "CSV" },
  { value: "edi", label: "EDI" },
  { value: "api_partner", label: "API Partner" },
  { value: "legacy", label: "Legacy" },
];

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getStatusBadgeClass(status: OrderStatus): string {
  switch (status) {
    case "placed":
      return "bg-blue-100 text-blue-700";
    case "confirmed":
      return "bg-indigo-100 text-indigo-700";
    case "scheduled":
      return "bg-purple-100 text-purple-700";
    case "dispatched":
      return "bg-cyan-100 text-cyan-700";
    case "in_transit":
      return "bg-amber-100 text-amber-700";
    case "delivered":
      return "bg-green-100 text-green-700";
    case "failed":
      return "bg-red-100 text-red-700";
    case "cancelled":
      return "bg-gray-100 text-gray-700";
    case "on_hold":
      return "bg-yellow-100 text-yellow-700";
    default:
      return "bg-gray-100 text-gray-700";
  }
}

function getChannelBadgeClass(channel: IntakeChannelType): string {
  switch (channel) {
    case "voice":
      return "bg-violet-100 text-violet-700";
    case "web_portal":
      return "bg-sky-100 text-sky-700";
    case "dispatcher":
      return "bg-emerald-100 text-emerald-700";
    case "csv":
      return "bg-orange-100 text-orange-700";
    case "edi":
      return "bg-pink-100 text-pink-700";
    case "api_partner":
      return "bg-teal-100 text-teal-700";
    case "legacy":
      return "bg-gray-100 text-gray-600";
    default:
      return "bg-gray-100 text-gray-700";
  }
}

function formatDate(dateStr?: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ─── Props ───────────────────────────────────────────────────────────────────

export interface OrdersPageProps {
  /** Tenant ID for WebSocket scoping */
  tenantId?: string;
  /** Callback when user clicks "Create Order" */
  onCreateOrder?: () => void;
  /** Callback when user clicks an order row */
  onOrderClick?: (orderId: string) => void;
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function OrdersPage({
  tenantId = "default",
  onCreateOrder,
  onOrderClick,
}: OrdersPageProps) {
  // ── State ───────────────────────────────────────────────────────────────
  const [orders, setOrders] = useState<FuelOrder[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [total, setTotal] = useState(0);

  // Filters
  const [statusFilter, setStatusFilter] = useState<OrderStatus | "">("");
  const [callTypeFilter, setCallTypeFilter] = useState<CallType | "">("");
  const [channelFilter, setChannelFilter] = useState<IntakeChannelType | "">(
    "",
  );
  const [customerIdFilter, setCustomerIdFilter] = useState("");
  const [driverIdFilter, setDriverIdFilter] = useState("");
  const [productCodeFilter, setProductCodeFilter] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  // ── Data fetching ───────────────────────────────────────────────────────

  const filters: OrderListFilters = useMemo(
    () => ({
      status: statusFilter || undefined,
      call_type: callTypeFilter || undefined,
      intake_channel: channelFilter || undefined,
      customer_id: customerIdFilter || undefined,
      driver_id: driverIdFilter || undefined,
      product_code: productCodeFilter || undefined,
      start_date: startDate || undefined,
      end_date: endDate || undefined,
      page,
      size: PAGE_SIZE,
    }),
    [
      statusFilter,
      callTypeFilter,
      channelFilter,
      customerIdFilter,
      driverIdFilter,
      productCodeFilter,
      startDate,
      endDate,
      page,
    ],
  );

  const fetchOrders = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response: PaginatedResponse<FuelOrder> = await listOrders(filters);
      setOrders(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
      setTotal(response.pagination?.total ?? 0);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load orders");
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    fetchOrders();
  }, [fetchOrders]);

  // ── WebSocket real-time updates ─────────────────────────────────────────

  const handleOrderUpdate = useCallback(
    (order: FuelOrder) => {
      setOrders((prev) => {
        const idx = prev.findIndex((o) => o.order_id === order.order_id);
        if (idx >= 0) {
          const next = [...prev];
          next[idx] = order;
          return next;
        }
        // New order — prepend if on first page
        if (page === 1) {
          return [order, ...prev].slice(0, PAGE_SIZE);
        }
        return prev;
      });
    },
    [page],
  );

  useOrdersWebSocket(tenantId, {
    onOrderPlaced: handleOrderUpdate,
    onOrderStatusChanged: handleOrderUpdate,
    onOrderAssigned: handleOrderUpdate,
  });

  // ── Filter change resets page ───────────────────────────────────────────

  const resetPage = useCallback(() => setPage(1), []);

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-7xl mx-auto px-6 py-6 space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-semibold text-[#232323]">Orders</h1>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={fetchOrders}
              disabled={loading}
              className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 hover:text-gray-800 rounded-lg hover:bg-gray-100 border border-gray-200"
              aria-label="Refresh orders"
            >
              <RefreshCw
                className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
              />
              Refresh
            </button>
            {onCreateOrder && (
              <button
                type="button"
                onClick={onCreateOrder}
                className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white rounded-lg"
                style={{ backgroundColor: "#232323" }}
                aria-label="Create order"
              >
                <Plus className="w-4 h-4" />
                Create Order
              </button>
            )}
          </div>
        </div>

        {/* Filters */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div>
              <label htmlFor="order-status-filter" className="sr-only">
                Status
              </label>
              <select
                id="order-status-filter"
                value={statusFilter}
                onChange={(e) => {
                  setStatusFilter(e.target.value as OrderStatus | "");
                  resetPage();
                }}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#232323]"
                aria-label="Filter by status"
              >
                {STATUS_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="order-call-type-filter" className="sr-only">
                Call Type
              </label>
              <select
                id="order-call-type-filter"
                value={callTypeFilter}
                onChange={(e) => {
                  setCallTypeFilter(e.target.value as CallType | "");
                  resetPage();
                }}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#232323]"
                aria-label="Filter by call type"
              >
                {CALL_TYPE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="order-channel-filter" className="sr-only">
                Intake Channel
              </label>
              <select
                id="order-channel-filter"
                value={channelFilter}
                onChange={(e) => {
                  setChannelFilter(e.target.value as IntakeChannelType | "");
                  resetPage();
                }}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#232323]"
                aria-label="Filter by intake channel"
              >
                {CHANNEL_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
              <input
                type="text"
                value={customerIdFilter}
                onChange={(e) => {
                  setCustomerIdFilter(e.target.value);
                  resetPage();
                }}
                placeholder="Customer ID"
                className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-[#232323] focus:outline-none"
                aria-label="Filter by customer ID"
              />
            </div>

            <div>
              <input
                type="text"
                value={driverIdFilter}
                onChange={(e) => {
                  setDriverIdFilter(e.target.value);
                  resetPage();
                }}
                placeholder="Driver ID"
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-[#232323] focus:outline-none"
                aria-label="Filter by driver ID"
              />
            </div>

            <div>
              <input
                type="text"
                value={productCodeFilter}
                onChange={(e) => {
                  setProductCodeFilter(e.target.value);
                  resetPage();
                }}
                placeholder="Product Code"
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-[#232323] focus:outline-none"
                aria-label="Filter by product code"
              />
            </div>

            <div>
              <input
                type="date"
                value={startDate}
                onChange={(e) => {
                  setStartDate(e.target.value);
                  resetPage();
                }}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-[#232323] focus:outline-none"
                aria-label="Start date"
              />
            </div>

            <div>
              <input
                type="date"
                value={endDate}
                onChange={(e) => {
                  setEndDate(e.target.value);
                  resetPage();
                }}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-[#232323] focus:outline-none"
                aria-label="End date"
              />
            </div>
          </div>
        </div>

        {/* Error */}
        {error && (
          <div role="alert" className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
            {error}
          </div>
        )}

        {/* Table */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
          {loading && orders.length === 0 ? (
            <div className="flex items-center justify-center py-16">
              <RefreshCw className="w-5 h-5 text-gray-400 animate-spin" />
            </div>
          ) : orders.length === 0 ? (
            <div className="text-center py-16 text-gray-500">
              <p className="text-lg font-medium text-gray-400">
                No orders found
              </p>
              <p className="text-sm text-gray-400 mt-1">
                Try adjusting your filters
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full" aria-label="Orders list">
                <thead className="bg-gray-50 border-b border-gray-100">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">
                      Order ID
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">
                      Customer
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">
                      Product
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">
                      Gallons
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">
                      Status
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">
                      Channel
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">
                      Call Type
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-600 uppercase">
                      Created
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {orders.map((order) => (
                    <tr
                      key={order.order_id}
                      className="hover:bg-gray-50 cursor-pointer transition-colors"
                      onClick={() => onOrderClick?.(order.order_id)}
                    >
                      <td className="px-4 py-3 text-sm font-medium text-[#232323]">
                        {order.order_id.slice(0, 12)}…
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-700">
                        {order.customer_name}
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-700">
                        {order.product_code ?? "—"}
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-700">
                        {order.fill_to_full
                          ? "Fill to Full"
                          : order.gallons_requested
                            ? `${order.gallons_requested} gal`
                            : "—"}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${getStatusBadgeClass(order.status)}`}
                        >
                          {order.status.replace("_", " ")}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium ${getChannelBadgeClass(order.intake_channel)}`}
                          data-testid="intake-channel-badge"
                        >
                          {order.intake_channel.replace("_", " ")}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-700">
                        {order.call_type.replace("_", " ")}
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-600">
                        {formatDate(order.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 border-t border-gray-100">
              <p className="text-sm text-gray-600">
                Showing page {page} of {totalPages} ({total} total)
              </p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg disabled:opacity-50 hover:bg-gray-50"
                  aria-label="Previous page"
                >
                  Previous
                </button>
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg disabled:opacity-50 hover:bg-gray-50"
                  aria-label="Next page"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
