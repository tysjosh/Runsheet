"use client";

/**
 * Orders (`/dashboard/orders`, UI revamp R10.2, task 2.7).
 *
 * The list template: the compact title row (Export CSV and Refresh; the top
 * bar's "New order" is the one create action), then one toolbar: the list's
 * search, status chips with counts plus "Awaiting confirmation", and a
 * "Filters · n" popover (call type, channel, customer, driver, product and a
 * date preset). Rows show the product as a chip with its readable name,
 * gallons and dates through `lib/format`, and status as a `StatusBadge`.
 *
 * Selecting rows offers bulk Confirm and Hold. They call the existing
 * per-order endpoints one order at a time (`PATCH /orders/:id/status`,
 * `POST /orders/:id/hold`) and report each row's result.
 *
 * Live: `/ws/orders` events patch rows in place.
 */
import { Check, Package, PauseCircle, RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  EmptyState,
  EntityLink,
  ExportCsvButton,
  Field,
  FilterChips,
  FilterPopover,
  FormDialog,
  IconButton,
  INPUT_CLASS,
  PageHeader,
  ProductChip,
  StatusBadge,
  statusKeyFor,
  Toolbar,
} from "@/components/ui";
import { useOrdersWebSocket } from "../../hooks/useOrdersWebSocket";
import {
  dateTime,
  number as formatNumber,
  gallons,
  humanize,
  productName,
  window as timeWindow,
} from "../../lib/format";
import { classifyLoadError } from "../../services/apiErrors";
import { PORTAL_REVIEW_HOLD_REASON } from "../../services/orderHoldReasons";
import {
  type CallType,
  type FuelOrder,
  holdOrder,
  type IntakeChannelType,
  listOrders,
  type OrderListFilters,
  type OrderListResponse,
  type OrderStatus,
  updateOrderStatus,
} from "../../services/ordersApi";
import { getCurrentTenantId } from "../../services/tenant";
import type { StatusKey } from "../../styles/tokens";
import { NETWORK_COPY } from "../ui/LoadErrorState";
import { notify } from "../ui/toast/notify";
import CustomerPicker from "./CustomerPicker";
import DriverPicker from "./DriverPicker";
import ProductPicker from "./ProductPicker";

// ─── Constants ───────────────────────────────────────────────────────────────

export const PAGE_SIZE = 20;

/** Order status → display status, label (hue + icon + label, R5.4). */
export const ORDER_STATUS: Record<
  OrderStatus,
  { status: StatusKey; label: string }
> = {
  placed: { status: "draft", label: "Placed" },
  confirmed: { status: "planned", label: "Confirmed" },
  scheduled: { status: "planned", label: "Scheduled" },
  dispatched: { status: "dispatched", label: "Dispatched" },
  in_transit: { status: "in_transit", label: "In transit" },
  delivered: { status: "delivered", label: "Delivered" },
  failed: { status: "exception", label: "Failed" },
  cancelled: { status: "cancelled", label: "Cancelled" },
  on_hold: { status: "delayed", label: "On hold" },
};

/** Status chips, in workflow order. */
export const STATUS_CHIPS: OrderStatus[] = [
  "placed",
  "on_hold",
  "confirmed",
  "scheduled",
  "dispatched",
  "in_transit",
  "delivered",
  "failed",
  "cancelled",
];

const AWAITING = "awaiting";

const CALL_TYPE_OPTIONS: { value: CallType | ""; label: string }[] = [
  { value: "", label: "All call types" },
  { value: "will_call", label: "Will call" },
  { value: "auto_fill", label: "Auto fill" },
  { value: "keep_full", label: "Keep full" },
  { value: "one_off", label: "One off" },
];
const CALL_TYPE_LABEL: Record<string, string> = Object.fromEntries(
  CALL_TYPE_OPTIONS.filter((o) => o.value).map((o) => [o.value, o.label]),
);

const CHANNEL_OPTIONS: { value: IntakeChannelType | ""; label: string }[] = [
  { value: "", label: "All channels" },
  { value: "voice", label: "Voice" },
  { value: "web_portal", label: "Web portal" },
  { value: "dispatcher", label: "Dispatcher" },
  { value: "csv", label: "CSV" },
  { value: "edi", label: "EDI" },
  { value: "api_partner", label: "API partner" },
  { value: "legacy", label: "Legacy" },
];
const CHANNEL_LABEL: Record<string, string> = Object.fromEntries(
  CHANNEL_OPTIONS.filter((o) => o.value).map((o) => [o.value, o.label]),
);

type DatePreset = "" | "today" | "tomorrow" | "week" | "custom";

/** YYYY-MM-DD in the browser's zone, `days` from today. */
function isoDay(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function presetRange(
  p: DatePreset,
): { start: string; end: string } | null {
  switch (p) {
    case "today":
      return { start: isoDay(0), end: isoDay(0) };
    case "tomorrow":
      return { start: isoDay(1), end: isoDay(1) };
    case "week":
      return { start: isoDay(0), end: isoDay(6) };
    default:
      return null;
  }
}

// ─── Customer Cell (cross-module-entity-linkage Req 1.2, 1.3, 1.4, 13.1, 13.3) ─

/**
 * Orders-table customer cell. Delegates to the shared {@link EntityLink} so
 * resolution/unlinked behaviour and in-shell vs route navigation are handled in
 * one place. The resolved summary name wins over the denormalized snapshot
 * (Req 1.4); list reads with no expanded ``links`` link optimistically on
 * ``customer_id``.
 */
function CustomerCell({ order }: { order: FuelOrder }) {
  return (
    <EntityLink
      type="customer"
      id={order.customer_id}
      label={order.customer_name?.trim() || undefined}
      link={order.links?.customer}
      showId={false}
      stopPropagation
      className="text-sm font-medium"
    />
  );
}

// ─── Bulk actions ────────────────────────────────────────────────────────────

export interface BulkResult {
  ok: string[];
  failed: { id: string; message: string }[];
}

/** Runs `fn` on each order in turn (the per-order endpoints, no bulk route). */
export async function runSequential(
  ids: string[],
  fn: (id: string) => Promise<unknown>,
): Promise<BulkResult> {
  const out: BulkResult = { ok: [], failed: [] };
  for (const id of ids) {
    try {
      await fn(id);
      out.ok.push(id);
    } catch (err) {
      out.failed.push({
        id,
        message: err instanceof Error && err.message ? err.message : "failed",
      });
    }
  }
  return out;
}

/** "Confirmed 3 of 4 orders. QA-ORD-1003: not allowed from delivered." */
export function bulkMessage(
  verb: string,
  total: number,
  r: BulkResult,
): string {
  const head = `${verb} ${formatNumber(r.ok.length)} of ${formatNumber(total)} ${total === 1 ? "order" : "orders"}.`;
  if (r.failed.length === 0) return head;
  const shown = r.failed
    .slice(0, 3)
    .map((f) => `${f.id}: ${f.message}`)
    .join("; ");
  const more = r.failed.length > 3 ? ` (+${r.failed.length - 3} more)` : "";
  return `${head} Not changed: ${shown}${more}.`;
}

// ─── Props ───────────────────────────────────────────────────────────────────

export interface OrdersPageProps {
  /** Tenant ID for WebSocket scoping */
  tenantId?: string;
  /**
   * Kept for callers; the page no longer renders its own create button (the
   * top bar's "New order" is the single create action).
   */
  onCreateOrder?: () => void;
  /** Callback when user clicks an order row */
  onOrderClick?: (orderId: string) => void;
  /** Seed text for the search (e.g. from the global header search). */
  initialQuery?: string;
  /** Seed status chip (e.g. `?status=placed` from the Dashboard). */
  initialStatus?: string;
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function OrdersPage({
  tenantId,
  onOrderClick,
  initialQuery = "",
  initialStatus = "",
}: OrdersPageProps) {
  const resolvedTenantId = tenantId ?? getCurrentTenantId();
  const [orders, setOrders] = useState<FuelOrder[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<Partial<Record<string, number>>>({});
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState<"confirm" | "hold" | null>(null);
  const [holdOpen, setHoldOpen] = useState(false);

  // Filters
  const [statusFilter, setStatusFilter] = useState<
    OrderStatus | "" | typeof AWAITING
  >(
    STATUS_CHIPS.includes(initialStatus as OrderStatus) ||
      initialStatus === AWAITING
      ? (initialStatus as OrderStatus)
      : "",
  );
  const [callTypeFilter, setCallTypeFilter] = useState<CallType | "">("");
  const [channelFilter, setChannelFilter] = useState<IntakeChannelType | "">(
    "",
  );
  const [customerIdFilter, setCustomerIdFilter] = useState("");
  const [driverIdFilter, setDriverIdFilter] = useState("");
  const [productCodeFilter, setProductCodeFilter] = useState("");
  const [datePreset, setDatePreset] = useState<DatePreset>("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [query, setQuery] = useState(initialQuery);
  useEffect(() => {
    setQuery(initialQuery);
  }, [initialQuery]);
  const [debouncedQuery, setDebouncedQuery] = useState(initialQuery);
  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedQuery(query);
      setPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [query]);

  const range = presetRange(datePreset);
  const start = range?.start ?? (datePreset === "custom" ? startDate : "");
  const end = range?.end ?? (datePreset === "custom" ? endDate : "");

  /** Everything but the status: shared by the list and the chip counts. */
  const baseFilters: OrderListFilters = useMemo(
    () => ({
      call_type: callTypeFilter || undefined,
      intake_channel: channelFilter || undefined,
      customer_id: customerIdFilter || undefined,
      driver_id: driverIdFilter || undefined,
      product_code: productCodeFilter || undefined,
      start_date: start || undefined,
      end_date: end || undefined,
      q: debouncedQuery.trim() || undefined,
    }),
    [
      callTypeFilter,
      channelFilter,
      customerIdFilter,
      driverIdFilter,
      productCodeFilter,
      start,
      end,
      debouncedQuery,
    ],
  );

  const filters: OrderListFilters = useMemo(
    () => ({
      ...baseFilters,
      status:
        statusFilter === AWAITING
          ? "on_hold"
          : (statusFilter as OrderStatus) || undefined,
      hold_reason:
        statusFilter === AWAITING ? PORTAL_REVIEW_HOLD_REASON : undefined,
      page,
      size: PAGE_SIZE,
    }),
    [baseFilters, statusFilter, page],
  );

  // The export takes the list's filters without the paging params.
  const exportParams = useMemo(() => {
    // The export route has no hold_reason filter, so it isn't sent there.
    const { page: _page, size: _size, hold_reason: _hold, ...rest } = filters;
    return rest;
  }, [filters]);

  const fetchOrders = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response: OrderListResponse = await listOrders(filters);
      setOrders(response.items ?? []);
      const size = response.size || PAGE_SIZE;
      setTotalPages(Math.max(1, Math.ceil((response.total ?? 0) / size)));
      setTotal(response.total ?? 0);
    } catch (err) {
      const f = classifyLoadError(err, "Orders couldn't be loaded.");
      setError(f.kind === "network" ? NETWORK_COPY : f.message);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void fetchOrders();
  }, [fetchOrders]);

  // Chip counts: one `size: 1` read per status with the other filters
  // applied (the list endpoint has no aggregate). They fail open to no count.
  const fetchCounts = useCallback(async () => {
    const keys = ["", ...STATUS_CHIPS, AWAITING];
    const results = await Promise.allSettled(
      keys.map((k) =>
        listOrders({
          ...baseFilters,
          status: k === AWAITING ? "on_hold" : (k as OrderStatus) || undefined,
          hold_reason: k === AWAITING ? PORTAL_REVIEW_HOLD_REASON : undefined,
          page: 1,
          size: 1,
        }),
      ),
    );
    const next: Partial<Record<string, number>> = {};
    results.forEach((r, i) => {
      if (r.status === "fulfilled" && typeof r.value?.total === "number")
        next[keys[i]] = r.value.total;
    });
    setCounts(next);
  }, [baseFilters]);
  useEffect(() => {
    void fetchCounts();
  }, [fetchCounts]);

  // Selection belongs to the rows on screen.
  useEffect(() => {
    setSelected([]);
  }, [filters]);

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
        if (page === 1) return [order, ...prev].slice(0, PAGE_SIZE);
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

  const refresh = useCallback(() => {
    void fetchOrders();
    void fetchCounts();
  }, [fetchOrders, fetchCounts]);

  // ── Bulk Confirm / Hold (R10.2) ─────────────────────────────────────────
  const afterBulk = (verb: string, ids: string[], r: BulkResult) => {
    notify({
      type: r.failed.length === 0 ? "success" : "error",
      message: bulkMessage(verb, ids.length, r),
      durationMs: r.failed.length === 0 ? 4000 : 10000,
    });
    // Rows that failed stay selected so they can be retried or inspected.
    setSelected(r.failed.map((f) => f.id));
    refresh();
  };
  const confirmSelected = async () => {
    const ids = [...selected];
    setBusy("confirm");
    try {
      const r = await runSequential(ids, (id) =>
        updateOrderStatus(id, { new_status: "confirmed" }),
      );
      afterBulk("Confirmed", ids, r);
    } finally {
      setBusy(null);
    }
  };
  const holdSelected = async (reason: string) => {
    const ids = [...selected];
    setBusy("hold");
    try {
      const r = await runSequential(ids, (id) =>
        holdOrder(id, { hold_reason: reason }),
      );
      afterBulk("Put on hold", ids, r);
    } finally {
      setBusy(null);
    }
  };

  const secondary =
    (callTypeFilter ? 1 : 0) +
    (channelFilter ? 1 : 0) +
    (customerIdFilter ? 1 : 0) +
    (driverIdFilter ? 1 : 0) +
    (productCodeFilter ? 1 : 0) +
    (datePreset ? 1 : 0);
  const clearSecondary = () => {
    setCallTypeFilter("");
    setChannelFilter("");
    setCustomerIdFilter("");
    setDriverIdFilter("");
    setProductCodeFilter("");
    setDatePreset("");
    setStartDate("");
    setEndDate("");
    setPage(1);
  };

  const columns: Column<FuelOrder>[] = [
    {
      key: "order",
      header: "Order",
      width: 140,
      truncate: true,
      title: (o) => o.order_id,
      className: "font-semibold text-slate-900",
      cell: (o) => o.order_id,
    },
    {
      key: "customer",
      header: "Customer",
      truncate: true,
      title: (o) => o.customer_name ?? o.customer_id,
      cell: (o) => <CustomerCell order={o} />,
    },
    {
      key: "product",
      header: "Product",
      width: 180,
      truncate: true,
      title: (o) => (o.product_code ? productName(o.product_code) : undefined),
      cell: (o) =>
        o.product_code ? (
          <ProductChip code={o.product_code} />
        ) : (
          <span className="text-text-muted">—</span>
        ),
    },
    {
      key: "gallons",
      header: "Gallons",
      width: 96,
      align: "right",
      className: "whitespace-nowrap tabular-nums text-slate-800",
      cell: (o) => (o.fill_to_full ? "Fill" : gallons(o.gallons_requested)),
    },
    {
      key: "window",
      header: "Window",
      width: 112,
      className: "whitespace-nowrap tabular-nums text-slate-700",
      cell: (o) =>
        o.delivery_window_start || o.delivery_window_end
          ? timeWindow(o.delivery_window_start, o.delivery_window_end)
          : "Any time",
    },
    {
      key: "status",
      header: "Status",
      width: 120,
      cell: (o) => {
        const s = ORDER_STATUS[o.status] ?? {
          status: statusKeyFor(o.status) ?? ("draft" as StatusKey),
          label: humanize(o.status),
        };
        return (
          <span
            title={
              o.status === "on_hold" && o.hold_reason
                ? o.hold_reason
                : undefined
            }
          >
            <StatusBadge status={s.status} label={s.label} />
          </span>
        );
      },
    },
    {
      key: "channel",
      header: "Channel",
      width: 96,
      truncate: true,
      className: "text-slate-700",
      cell: (o) => (
        <span data-testid="intake-channel-badge">
          {CHANNEL_LABEL[o.intake_channel] ?? o.intake_channel}
        </span>
      ),
    },
    {
      key: "callType",
      header: "Call type",
      width: 92,
      truncate: true,
      className: "text-slate-700",
      cell: (o) => CALL_TYPE_LABEL[o.call_type] ?? o.call_type,
    },
    {
      key: "created",
      header: "Created",
      width: 128,
      className: "whitespace-nowrap tabular-nums text-slate-600",
      cell: (o) => dateTime(o.created_at),
    },
  ];

  const chipOptions = [
    { id: "", label: "All", count: counts[""] },
    ...STATUS_CHIPS.map((s) => ({
      id: s,
      label: ORDER_STATUS[s].label,
      status: ORDER_STATUS[s].status,
      count: counts[s],
    })),
  ];

  const selectSmall =
    "h-8 w-full rounded-lg border border-slate-300 bg-surface px-2 text-sm text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus";

  return (
    <div className="flex h-full flex-col bg-surface">
      <PageHeader
        title="Orders"
        help="Every fuel order and delivery request. Select rows to confirm or hold them together; New order in the top bar creates one."
        counts={
          <span>
            <b className="text-sm text-slate-900 tabular-nums">
              {formatNumber(total)}
            </b>{" "}
            {total === 1 ? "order" : "orders"}
          </span>
        }
        actions={
          <>
            <ExportCsvButton
              type="orders"
              params={exportParams}
              subject="orders"
              allowedRoles={["admin", "dispatcher"]}
            />
            <IconButton
              label="Refresh orders"
              size="sm"
              onClick={refresh}
              disabled={loading}
              icon={
                <RefreshCw
                  className={`h-3.5 w-3.5 ${loading ? "motion-safe:animate-spin" : ""}`}
                />
              }
            />
          </>
        }
      />
      <Toolbar
        label="Orders"
        search={
          <input
            id="order-search"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search ID, customer, address"
            aria-label="Search orders"
            className="h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          />
        }
        filters={
          selected.length > 0 ? (
            <div
              role="group"
              aria-label="Selected orders"
              className="flex items-center gap-2"
            >
              <span
                className="whitespace-nowrap text-xs font-semibold text-slate-800"
                aria-live="polite"
              >
                {formatNumber(selected.length)} selected
              </span>
              <Button
                size="sm"
                variant="primary"
                icon={<Check className="h-3.5 w-3.5" />}
                loading={busy === "confirm"}
                disabled={busy !== null}
                onClick={() => void confirmSelected()}
              >
                Confirm
              </Button>
              <Button
                size="sm"
                variant="secondary"
                icon={<PauseCircle className="h-3.5 w-3.5" />}
                loading={busy === "hold"}
                disabled={busy !== null}
                onClick={() => setHoldOpen(true)}
              >
                Hold
              </Button>
              <Button
                size="sm"
                variant="ghost"
                icon={<X className="h-3.5 w-3.5" />}
                disabled={busy !== null}
                onClick={() => setSelected([])}
              >
                Clear selection
              </Button>
            </div>
          ) : (
            <>
              <FilterChips
                label="Order status"
                options={chipOptions}
                value={statusFilter === AWAITING ? "" : statusFilter}
                onChange={(v) => {
                  setStatusFilter(v as OrderStatus | "");
                  setPage(1);
                }}
                className="overflow-x-auto"
              />
              <button
                type="button"
                aria-pressed={statusFilter === AWAITING}
                onClick={() => {
                  setStatusFilter((s) => (s === AWAITING ? "" : AWAITING));
                  setPage(1);
                }}
                className={`inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs font-semibold focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
                  statusFilter === AWAITING
                    ? "border-fuchsia-600 bg-fuchsia-50 text-fuchsia-800"
                    : "border-slate-300 bg-surface text-slate-700 hover:bg-slate-50"
                }`}
              >
                Awaiting confirmation
                {counts[AWAITING] !== undefined && (
                  <span className="rounded-full bg-slate-100 px-1.5 tabular-nums">
                    {counts[AWAITING]}
                  </span>
                )}
              </button>
              <FilterPopover
                count={secondary}
                label="Order filters"
                onClear={clearSecondary}
              >
                <div className="grid w-[34rem] max-w-full grid-cols-2 gap-3">
                  <Field label="Call type" span={1}>
                    <select
                      value={callTypeFilter}
                      onChange={(e) => {
                        setCallTypeFilter(e.target.value as CallType | "");
                        setPage(1);
                      }}
                      className={selectSmall}
                      aria-label="Filter by call type"
                    >
                      {CALL_TYPE_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Channel" span={1}>
                    <select
                      value={channelFilter}
                      onChange={(e) => {
                        setChannelFilter(
                          e.target.value as IntakeChannelType | "",
                        );
                        setPage(1);
                      }}
                      className={selectSmall}
                      aria-label="Filter by intake channel"
                    >
                      {CHANNEL_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <div className="col-span-2 flex flex-col gap-1 sm:col-span-1">
                    <span className="text-sm font-medium text-text">
                      Customer
                    </span>
                    <CustomerPicker
                      value={customerIdFilter || null}
                      onChange={(id) => {
                        setCustomerIdFilter(id);
                        setPage(1);
                      }}
                      placeholder="All customers"
                      allowClear
                      aria-label="Filter by customer"
                    />
                  </div>
                  <div className="col-span-2 flex flex-col gap-1 sm:col-span-1">
                    <span className="text-sm font-medium text-text">
                      Driver
                    </span>
                    <DriverPicker
                      value={driverIdFilter || null}
                      onChange={(id) => {
                        setDriverIdFilter(id);
                        setPage(1);
                      }}
                      placeholder="All drivers"
                      allowClear
                      aria-label="Filter by driver"
                    />
                  </div>
                  <div className="col-span-2 flex flex-col gap-1 sm:col-span-1">
                    <span className="text-sm font-medium text-text">
                      Product
                    </span>
                    <ProductPicker
                      value={productCodeFilter || null}
                      onChange={(code) => {
                        setProductCodeFilter(code);
                        setPage(1);
                      }}
                      placeholder="All products"
                      allowClear
                      aria-label="Filter by product"
                    />
                  </div>
                  <Field label="Date" span={1}>
                    <select
                      value={datePreset}
                      onChange={(e) => {
                        setDatePreset(e.target.value as DatePreset);
                        setPage(1);
                      }}
                      className={selectSmall}
                      aria-label="Date"
                    >
                      <option value="">Any</option>
                      <option value="today">Today</option>
                      <option value="tomorrow">Tomorrow</option>
                      <option value="week">This week</option>
                      <option value="custom">Custom…</option>
                    </select>
                  </Field>
                  {datePreset === "custom" && (
                    <>
                      <Field label="From" span={1}>
                        <input
                          type="date"
                          value={startDate}
                          onChange={(e) => {
                            setStartDate(e.target.value);
                            setPage(1);
                          }}
                          className={INPUT_CLASS}
                          aria-label="Start date"
                        />
                      </Field>
                      <Field label="To" span={1}>
                        <input
                          type="date"
                          value={endDate}
                          onChange={(e) => {
                            setEndDate(e.target.value);
                            setPage(1);
                          }}
                          className={INPUT_CLASS}
                          aria-label="End date"
                        />
                      </Field>
                    </>
                  )}
                </div>
              </FilterPopover>
            </>
          )
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <DataTable<FuelOrder>
          ariaLabel="Orders"
          columns={columns}
          data={orders}
          getRowId={(o) => o.order_id}
          rowLabel={(o) => `order ${o.order_id}`}
          onRowClick={
            onOrderClick ? (o) => onOrderClick(o.order_id) : undefined
          }
          selectable
          selectedIds={selected}
          onSelectionChange={setSelected}
          loading={loading && orders.length === 0}
          error={error ? { message: error, onRetry: refresh } : null}
          emptyState={
            <EmptyState
              icon={<Package />}
              title="No orders found"
              description="Try another status or clear the filters."
            />
          }
          pagination={
            totalPages > 1
              ? { page, totalPages, totalItems: total, onPageChange: setPage }
              : undefined
          }
        />
      </div>
      {holdOpen && (
        <FormDialog<{ reason: string }>
          open
          size="sm"
          title={`Hold ${formatNumber(selected.length)} ${selected.length === 1 ? "order" : "orders"}`}
          help="The reason is recorded on each order. Release them from the order or the Dashboard."
          submitLabel="Put on hold"
          initialValues={{ reason: "" }}
          validate={(v) =>
            v.reason.trim() ? {} : { reason: "Enter a reason." }
          }
          // The per-row result toast replaces the generic one.
          successMessage={null}
          onSubmit={(v) => holdSelected(v.reason.trim())}
          onClose={() => setHoldOpen(false)}
        >
          {({ values, set, errors }) => (
            <Field label="Hold reason" required error={errors.reason}>
              <input
                type="text"
                value={values.reason}
                onChange={(e) => set("reason", e.target.value)}
                placeholder="Credit check, customer request…"
                className={INPUT_CLASS}
              />
            </Field>
          )}
        </FormDialog>
      )}
    </div>
  );
}
