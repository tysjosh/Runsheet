"use client";

/**
 * Orders Page — lists fuel orders with filters, pagination, and
 * real-time updates via `/ws/orders`.
 *
 * Validates: Requirements 2.5.1, 8.1.3, 8.1.5
 */

import { Package, Plus, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  type BadgeVariant,
  Button,
  type Column,
  EmptyState,
  PageHeader,
  Pagination,
  Table,
} from "@/components/ui";
import { useOrdersWebSocket } from "../../hooks/useOrdersWebSocket";
import {
  type CallType,
  type FuelOrder,
  type IntakeChannelType,
  listOrders,
  type OrderListFilters,
  type OrderStatus,
  type PaginatedResponse,
} from "../../services/ordersApi";
import { getCurrentTenantId } from "../../services/tenant";
import CustomerPicker from "./CustomerPicker";
import DriverPicker from "./DriverPicker";
import ProductPicker from "./ProductPicker";

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

function getStatusVariant(status: OrderStatus): BadgeVariant {
  switch (status) {
    case "placed":
      return "info";
    case "confirmed":
      return "info";
    case "scheduled":
      return "warning";
    case "dispatched":
      return "info";
    case "in_transit":
      return "warning";
    case "delivered":
      return "success";
    case "failed":
      return "error";
    case "cancelled":
      return "neutral";
    case "on_hold":
      return "warning";
    default:
      return "neutral";
  }
}

function getChannelVariant(channel: IntakeChannelType): BadgeVariant {
  switch (channel) {
    case "voice":
      return "info";
    case "web_portal":
      return "info";
    case "dispatcher":
      return "success";
    case "csv":
      return "warning";
    case "edi":
      return "info";
    case "api_partner":
      return "info";
    case "legacy":
      return "neutral";
    default:
      return "neutral";
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

// ─── Customer Cell (cross-module-entity-linkage Req 1.2, 1.3, 1.4, 13.1, 13.3) ─

/** Pull a human display name out of a resolved customer reference summary. */
function summaryLabel(summary: Record<string, unknown>): string | undefined {
  for (const key of ["display_name", "legal_name", "name", "customer_name"]) {
    const value = summary[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

/**
 * Renders the Orders-table customer cell as navigation to the commerce
 * customer record when the reference is resolvable, or an explicit "Unlinked"
 * affordance when it is not (Req 1.2, 1.3, 13.1, 13.3).
 *
 * Display-name precedence honours the commerce record as the source of truth
 * (Req 1.4): a resolved ``links.customer`` summary name wins over the
 * denormalized ``customer_name`` snapshot. List reads carry no ``links`` object
 * — there the cell links optimistically on ``customer_id`` and uses the
 * snapshot as the display label (mirroring the JobDetailPage fallback path).
 */
function CustomerCell({ order }: { order: FuelOrder }) {
  const link = order.links?.customer;
  const snapshot = order.customer_name?.trim() || undefined;

  // Explicitly unresolved reference — never show a stale name as if linked.
  if (link?.status === "unresolved") {
    return (
      <span className="inline-flex items-center gap-1.5">
        {snapshot && <span className="text-sm text-gray-500">{snapshot}</span>}
        <Badge variant="warning" size="sm">
          Unlinked
        </Badge>
      </span>
    );
  }

  // Resolved reference — prefer the resolved name (source of truth, Req 1.4).
  if (link?.status === "resolved") {
    const display = summaryLabel(link.summary) ?? snapshot ?? link.id;
    return (
      <Link
        href={`/commerce/customers/${encodeURIComponent(link.id)}`}
        className="text-sm font-medium text-info hover:text-info-dark underline underline-offset-2"
        onClick={(e) => e.stopPropagation()}
      >
        {display}
      </Link>
    );
  }

  // No links payload (list read): link optimistically when a customer_id is
  // present, otherwise surface the row as unlinked.
  const customerId = order.customer_id?.trim();
  if (customerId) {
    const display = snapshot ?? customerId;
    return (
      <Link
        href={`/commerce/customers/${encodeURIComponent(customerId)}`}
        className="text-sm font-medium text-info hover:text-info-dark underline underline-offset-2"
        onClick={(e) => e.stopPropagation()}
      >
        {display}
      </Link>
    );
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      {snapshot && <span className="text-sm text-gray-500">{snapshot}</span>}
      <Badge variant="warning" size="sm">
        Unlinked
      </Badge>
    </span>
  );
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
  tenantId,
  onCreateOrder,
  onOrderClick,
}: OrdersPageProps) {
  const resolvedTenantId = tenantId ?? getCurrentTenantId();
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

  useOrdersWebSocket(resolvedTenantId, {
    onOrderPlaced: handleOrderUpdate,
    onOrderStatusChanged: handleOrderUpdate,
    onOrderAssigned: handleOrderUpdate,
  });

  // ── Filter change resets page ───────────────────────────────────────────

  const resetPage = useCallback(() => setPage(1), []);

  const orderColumns: Column<FuelOrder>[] = [
    {
      key: "order",
      label: "Order ID",
      render: (order) => (
        <span className="text-sm font-medium text-primary">
          {order.order_id.slice(0, 12)}...
        </span>
      ),
    },
    {
      key: "customer",
      label: "Customer",
      render: (order) => <CustomerCell order={order} />,
    },
    {
      key: "product",
      label: "Product",
      render: (order) => (
        <span className="text-sm text-gray-700">
          {order.product_code ?? "-"}
        </span>
      ),
    },
    {
      key: "gallons",
      label: "Gallons",
      render: (order) => (
        <span className="text-sm text-gray-700">
          {order.fill_to_full
            ? "Fill to Full"
            : order.gallons_requested
              ? `${order.gallons_requested} gal`
              : "-"}
        </span>
      ),
    },
    {
      key: "status",
      label: "Status",
      render: (order) => (
        <Badge variant={getStatusVariant(order.status)} size="sm">
          {order.status.replace("_", " ")}
        </Badge>
      ),
    },
    {
      key: "channel",
      label: "Channel",
      render: (order) => (
        <Badge
          variant={getChannelVariant(order.intake_channel)}
          size="sm"
          data-testid="intake-channel-badge"
        >
          {order.intake_channel.replace("_", " ")}
        </Badge>
      ),
    },
    {
      key: "callType",
      label: "Call Type",
      render: (order) => (
        <span className="text-sm text-gray-700">
          {order.call_type.replace("_", " ")}
        </span>
      ),
    },
    {
      key: "created",
      label: "Created",
      render: (order) => (
        <span className="text-sm text-gray-600">
          {formatDate(order.created_at)}
        </span>
      ),
    },
  ];

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <div className="h-full flex flex-col bg-white">
      <PageHeader
        title="Orders"
        subtitle="Manage fuel orders and delivery requests"
        icon={<Package className="w-5 h-5" />}
        actions={
          <>
            <Button
              type="button"
              onClick={fetchOrders}
              disabled={loading}
              variant="secondary"
              icon={
                <RefreshCw
                  className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
                />
              }
              aria-label="Refresh orders"
            >
              Refresh
            </Button>
            {onCreateOrder && (
              <Button
                type="button"
                onClick={onCreateOrder}
                icon={<Plus className="w-4 h-4" />}
                aria-label="Create order"
              >
                Create Order
              </Button>
            )}
          </>
        }
      />

      <div className="border-b border-gray-100 px-8 py-4">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
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
              className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
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
              className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
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
              className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
              aria-label="Filter by intake channel"
            >
              {CHANNEL_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          <CustomerPicker
            value={customerIdFilter || null}
            onChange={(id) => {
              setCustomerIdFilter(id);
              resetPage();
            }}
            placeholder="All customers"
            allowClear
            aria-label="Filter by customer"
          />

          <DriverPicker
            value={driverIdFilter || null}
            onChange={(id) => {
              setDriverIdFilter(id);
              resetPage();
            }}
            placeholder="All drivers"
            allowClear
            aria-label="Filter by driver"
          />

          <ProductPicker
            value={productCodeFilter || null}
            onChange={(code) => {
              setProductCodeFilter(code);
              resetPage();
            }}
            placeholder="All products"
            allowClear
            aria-label="Filter by product"
          />

          <input
            type="date"
            value={startDate}
            onChange={(e) => {
              setStartDate(e.target.value);
              resetPage();
            }}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
            aria-label="Start date"
          />

          <input
            type="date"
            value={endDate}
            onChange={(e) => {
              setEndDate(e.target.value);
              resetPage();
            }}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-primary focus:outline-none"
            aria-label="End date"
          />
        </div>
      </div>

      {error && (
        <div
          role="alert"
          className="mx-8 mt-4 rounded-lg bg-error-light border border-error/20 px-4 py-3 text-sm text-error-dark"
        >
          {error}
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-y-auto">
        {loading && orders.length === 0 ? (
          <div className="flex items-center justify-center py-16">
            <RefreshCw className="w-5 h-5 text-gray-400 animate-spin" />
          </div>
        ) : (
          <Table
            columns={orderColumns}
            data={orders}
            getRowId={(order) => order.order_id}
            onRowClick={
              onOrderClick ? (order) => onOrderClick(order.order_id) : undefined
            }
            emptyState={
              <EmptyState
                icon={<Package />}
                title="No orders found"
                description="Try adjusting your filters"
              />
            }
          />
        )}
        <Pagination
          currentPage={page}
          totalPages={totalPages}
          totalItems={total}
          onPageChange={setPage}
        />
      </div>
    </div>
  );
}
