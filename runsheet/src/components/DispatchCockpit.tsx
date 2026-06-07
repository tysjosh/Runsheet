"use client";

/**
 * DispatchCockpit — the dispatcher's "Today" landing.
 *
 * A single "what needs my attention now" surface. Rather than siloing work
 * into per-type boxes, it merges every source (orders, delayed jobs, fuel
 * alerts, agent proposals) into ONE severity-ranked priority feed, with filter
 * chips to narrow by category. A station near dry outranks a freshly placed
 * order, so the dispatcher always sees the most urgent work first. Read paths
 * reuse existing endpoints; each source loads independently and fails open so
 * one outage can't blank the whole cockpit.
 */

import {
  Bot,
  CheckCircle2,
  ChevronRight,
  ClipboardList,
  Clock,
  Fuel,
  RefreshCw,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { useOrdersWebSocket } from "../hooks/useOrdersWebSocket";
import { useSchedulingWebSocket } from "../hooks/useSchedulingWebSocket";
import { getApprovals } from "../services/agentApi";
import { ApiError } from "../services/api";
import {
  type FuelAlert,
  getAlerts as getFuelAlerts,
} from "../services/fuelApi";
import {
  assignDriver,
  type FuelOrder,
  listOrders,
  type OrderStatus,
  releaseHoldOrder,
} from "../services/ordersApi";
import { getDelayedJobs } from "../services/schedulingApi";
import { getCurrentTenantId } from "../services/tenant";
import type { Job } from "../types/api";
import DriverPicker from "./ops/DriverPicker";
import {
  Badge,
  type BadgeVariant,
  Button,
  PageHeader,
  ToastContainer,
  useToasts,
} from "./ui";

// Safety-net fallback poll. Live updates arrive over the orders/scheduling
// WebSockets; the cockpit only polls this slowly so it still recovers if the
// socket drops or a server-side push is missed.
const REFRESH_INTERVAL_MS = 120_000;
const WS_REFRESH_DEBOUNCE_MS = 500;
const MAX_PER_SOURCE = 6;

/** Safely extract a list array from a settled list-endpoint promise. */
function settledArray<T>(
  result: PromiseSettledResult<
    { data?: T[] | null; items?: T[] | null } | undefined
  >,
): T[] {
  if (result.status !== "fulfilled") return [];
  const list = result.value?.data ?? result.value?.items;
  return Array.isArray(list) ? list : [];
}

/** Extract the authoritative total from a settled paginated response. */
function settledTotal(
  result: PromiseSettledResult<
    | { total?: number; data?: unknown[] | null; items?: unknown[] | null }
    | undefined
  >,
): number {
  if (result.status !== "fulfilled") return 0;
  const v = result.value;
  if (typeof v?.total === "number") return v.total;
  const list = v?.data ?? v?.items;
  return Array.isArray(list) ? list.length : 0;
}

interface DispatchCockpitProps {
  onNavigate?: (item: string) => void;
  /** In-shell order opener; falls back to routing to /orders/:id. */
  onOpenOrder?: (orderId: string) => void;
}

type Category = "order" | "delayed" | "fuel" | "agent";

function formatRelative(dateStr?: string | null): string {
  if (!dateStr) return "";
  const then = new Date(dateStr).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function formatDelay(minutes?: number): string {
  if (!minutes || minutes <= 0) return "—";
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

const FUEL_ALERT_VARIANT: Record<FuelAlert["status"], BadgeVariant> = {
  low: "warning",
  critical: "error",
  empty: "error",
};

// Cross-category severity so the single feed can rank a near-dry station above
// a freshly placed order. Bases are spaced so categories don't leapfrog except
// where it's intended (a critical fuel alert outranks any delay).
function fuelSeverity(a: FuelAlert): number {
  const base =
    a.status === "empty" ? 4000 : a.status === "critical" ? 3000 : 2000;
  return base - (a.stock_percentage ?? 0);
}
function delaySeverity(j: Job): number {
  return 1000 + Math.min(j.delay_duration_minutes ?? 0, 900);
}

export default function DispatchCockpit({
  onNavigate,
  onOpenOrder,
}: DispatchCockpitProps) {
  const router = useRouter();
  const { toasts, addToast, dismissToast } = useToasts();
  const [orders, setOrders] = useState<FuelOrder[]>([]);
  const [delayedJobs, setDelayedJobs] = useState<Job[]>([]);
  const [fuelAlerts, setFuelAlerts] = useState<FuelAlert[]>([]);
  const [approvalCount, setApprovalCount] = useState(0);
  const [ordersTotal, setOrdersTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [filter, setFilter] = useState<"all" | Category>("all");

  // `background` refreshes (poll / WebSocket) update in place — they must not
  // flip the feed back to a skeleton over data the dispatcher is reading.
  const loadData = useCallback(async (opts?: { background?: boolean }) => {
    if (!opts?.background) setRefreshing(true);
    const orderStatuses: OrderStatus[] = ["placed", "on_hold"];
    const [placedRes, holdRes, delayedRes, fuelRes, approvalsRes] =
      await Promise.allSettled([
        listOrders({ status: orderStatuses[0], size: MAX_PER_SOURCE }),
        listOrders({ status: orderStatuses[1], size: MAX_PER_SOURCE }),
        getDelayedJobs(),
        getFuelAlerts(),
        getApprovals(getCurrentTenantId()),
      ]);

    const placed = settledArray<FuelOrder>(placedRes);
    const held = settledArray<FuelOrder>(holdRes);
    const byId = new Map<string, FuelOrder>();
    for (const o of [...placed, ...held]) byId.set(o.order_id, o);
    setOrders(
      [...byId.values()].sort(
        (a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      ),
    );
    // Authoritative backlog size from the API totals — not the capped page,
    // so the chip never under-reports a busy morning.
    setOrdersTotal(settledTotal(placedRes) + settledTotal(holdRes));

    setDelayedJobs(settledArray<Job>(delayedRes));
    setFuelAlerts(settledArray<FuelAlert>(fuelRes));

    if (approvalsRes.status === "fulfilled") {
      const data = approvalsRes.value as {
        entries?: unknown[];
        items?: unknown[];
      };
      const list = data.entries ?? data.items ?? [];
      setApprovalCount(list.length);
    } else {
      setApprovalCount(0);
    }

    setLastUpdated(new Date());
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    loadData();
    const interval = setInterval(
      () => loadData({ background: true }),
      REFRESH_INTERVAL_MS,
    );
    return () => clearInterval(interval);
  }, [loadData]);

  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
    refreshTimer.current = setTimeout(() => {
      refreshTimer.current = null;
      loadData({ background: true });
    }, WS_REFRESH_DEBOUNCE_MS);
  }, [loadData]);

  useEffect(
    () => () => {
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
    },
    [],
  );

  useOrdersWebSocket(getCurrentTenantId(), {
    subscriptions: ["order_placed", "order_status_changed", "order_assigned"],
    onOrderPlaced: scheduleRefresh,
    onOrderStatusChanged: scheduleRefresh,
    onOrderAssigned: scheduleRefresh,
  });
  useSchedulingWebSocket({
    subscriptions: ["status_changed", "delay_alert"],
    onStatusChanged: scheduleRefresh,
    onDelayAlert: scheduleRefresh,
  });

  const openOrder = (orderId: string) => {
    if (onOpenOrder) onOpenOrder(orderId);
    else router.push(`/orders/${encodeURIComponent(orderId)}`);
  };
  const removeOrder = (orderId: string) =>
    setOrders((prev) => prev.filter((o) => o.order_id !== orderId));

  const counts = {
    order: ordersTotal,
    delayed: delayedJobs.length,
    fuel: fuelAlerts.length,
    agent: approvalCount,
  };
  const totalCount = counts.order + counts.delayed + counts.fuel + counts.agent;

  const show = (c: Category) => filter === "all" || filter === c;

  // Build the unified, severity-ranked feed. Orders keep their own row
  // component (inline assign/release); other categories render generic rows.
  type Entry = { key: string; severity: number; node: React.ReactNode };
  const entries: Entry[] = [];

  if (show("fuel")) {
    for (const alert of fuelAlerts.slice(0, MAX_PER_SOURCE)) {
      entries.push({
        key: `fuel-${alert.station_id}`,
        severity: fuelSeverity(alert),
        node: (
          <FeedRow
            key={`fuel-${alert.station_id}`}
            icon={<Fuel className="h-4 w-4 text-warning" />}
            onClick={() => onNavigate?.("fuel-ops")}
            title={alert.name}
            subtitle={alert.location_name ?? alert.station_id}
            meta={`${Math.round(alert.stock_percentage)}%`}
            badge={
              <Badge variant={FUEL_ALERT_VARIANT[alert.status]} size="sm">
                {alert.status}
              </Badge>
            }
          />
        ),
      });
    }
  }

  if (show("delayed")) {
    for (const job of [...delayedJobs]
      .sort((a, b) => delaySeverity(b) - delaySeverity(a))
      .slice(0, MAX_PER_SOURCE)) {
      entries.push({
        key: `delayed-${job.job_id}`,
        severity: delaySeverity(job),
        node: (
          <FeedRow
            key={`delayed-${job.job_id}`}
            icon={<Clock className="h-4 w-4 text-error" />}
            onClick={() => onNavigate?.("dispatch")}
            title={job.job_id}
            subtitle={`${job.origin} → ${job.destination}`}
            badge={
              <Badge variant="error" size="sm">
                +{formatDelay(job.delay_duration_minutes)}
              </Badge>
            }
          />
        ),
      });
    }
  }

  if (show("order")) {
    for (const order of orders.slice(0, MAX_PER_SOURCE)) {
      const sev = order.status === "on_hold" ? 600 : 400;
      entries.push({
        key: `order-${order.order_id}`,
        severity: sev,
        node: (
          <OrderAttentionRow
            key={`order-${order.order_id}`}
            order={order}
            onOpen={() => openOrder(order.order_id)}
            onActioned={removeOrder}
            onReload={() => loadData({ background: true })}
            addToast={addToast}
          />
        ),
      });
    }
  }

  if (show("agent") && approvalCount > 0) {
    entries.push({
      key: "agent-proposals",
      severity: 200,
      node: (
        <FeedRow
          key="agent-proposals"
          icon={<Bot className="h-4 w-4 text-gray-500" />}
          onClick={() => onNavigate?.("control")}
          title={`${approvalCount} proposal${approvalCount === 1 ? "" : "s"} awaiting review`}
          subtitle="Review in Control Center"
        />
      ),
    });
  }

  entries.sort((a, b) => b.severity - a.severity);

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Today"
        subtitle="What needs your attention right now"
        icon={<ClipboardList className="w-5 h-5" />}
        actions={
          <div className="flex items-center gap-3">
            {lastUpdated && (
              <span className="text-xs text-gray-500">
                Updated {formatRelative(lastUpdated.toISOString())}
              </span>
            )}
            <Button
              variant="secondary"
              size="sm"
              onClick={() => loadData()}
              icon={
                <RefreshCw
                  className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`}
                />
              }
            >
              Refresh
            </Button>
          </div>
        }
      />

      <div className="flex-1 overflow-auto bg-gray-50 p-4 sm:p-6">
        {/* Filter chips — counts shown once, here, and double as filters. */}
        <div
          className="mb-4 flex flex-wrap gap-2"
          role="tablist"
          aria-label="Filter attention items"
        >
          <FilterChip
            label="All"
            count={totalCount}
            active={filter === "all"}
            onClick={() => setFilter("all")}
          />
          <FilterChip
            label="Orders"
            count={counts.order}
            active={filter === "order"}
            onClick={() => setFilter("order")}
            tone="info"
          />
          <FilterChip
            label="Delayed"
            count={counts.delayed}
            active={filter === "delayed"}
            onClick={() => setFilter("delayed")}
            tone="error"
          />
          <FilterChip
            label="Fuel"
            count={counts.fuel}
            active={filter === "fuel"}
            onClick={() => setFilter("fuel")}
            tone="warning"
          />
          <FilterChip
            label="Agents"
            count={counts.agent}
            active={filter === "agent"}
            onClick={() => setFilter("agent")}
          />
        </div>

        {/* Unified priority feed */}
        <div className="rounded-xl border border-gray-200 bg-white shadow-sm">
          <div className="flex items-center gap-2 border-b border-gray-100 px-4 py-3">
            <h3 className="text-sm font-semibold text-primary">
              Priority queue
            </h3>
            <span className="ml-auto text-xs font-medium text-gray-500">
              {entries.length} item{entries.length === 1 ? "" : "s"}
            </span>
          </div>

          {loading ? (
            <div className="px-4 py-16 text-center text-sm text-gray-500">
              Loading…
            </div>
          ) : entries.length === 0 ? (
            <div className="flex flex-col items-center gap-2 px-4 py-16 text-center">
              <CheckCircle2 className="h-8 w-8 text-success" />
              <p className="text-sm font-medium text-gray-700">
                You&apos;re all caught up
              </p>
              <p className="text-xs text-gray-500">
                {filter === "all"
                  ? "No orders waiting, no delays, all stations healthy."
                  : "Nothing needs attention in this category."}
              </p>
            </div>
          ) : (
            <div className="divide-y divide-gray-50">
              {entries.map((e) => e.node)}
            </div>
          )}
        </div>
      </div>

      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}

// ─── Filter chip ─────────────────────────────────────────────────────────────

const CHIP_TONE: Record<string, string> = {
  info: "text-info",
  error: "text-error",
  warning: "text-warning",
  default: "text-primary",
};

function FilterChip({
  label,
  count,
  active,
  onClick,
  tone = "default",
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  tone?: "info" | "error" | "warning" | "default";
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`inline-flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 ${
        active
          ? "border-primary bg-primary text-white"
          : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50"
      }`}
    >
      <span>{label}</span>
      <span
        className={`rounded-full px-1.5 text-xs font-semibold ${
          active ? "bg-white/20 text-white" : `bg-gray-100 ${CHIP_TONE[tone]}`
        }`}
      >
        {count}
      </span>
    </button>
  );
}

// ─── Generic feed row ──────────────────────────────────────────────────────

function FeedRow({
  icon,
  onClick,
  title,
  subtitle,
  meta,
  badge,
}: {
  icon: React.ReactNode;
  onClick: () => void;
  title: string;
  subtitle?: string;
  meta?: string;
  badge?: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-gray-50 focus:outline-none focus-visible:bg-gray-50"
    >
      <span className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-gray-50">
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-gray-900">{title}</p>
        {subtitle && (
          <p className="truncate text-xs text-gray-500">{subtitle}</p>
        )}
      </div>
      {meta && <span className="text-xs text-gray-500">{meta}</span>}
      {badge}
      <ChevronRight className="h-4 w-4 flex-shrink-0 text-gray-300" />
    </button>
  );
}

// ─── Order Attention Row (with inline actions) ──────────────────────────────

/**
 * Order row with inline triage actions so the dispatcher can act without
 * leaving the cockpit:
 *  • on_hold orders → "Release" (re-runs intake hooks server-side)
 *  • placed/unassigned orders → "Assign" (opens an inline DriverPicker)
 * The title area still deep-links to the full order detail.
 */
function OrderAttentionRow({
  order,
  onOpen,
  onActioned,
  onReload,
  addToast,
}: {
  order: FuelOrder;
  onOpen: () => void;
  onActioned: (orderId: string) => void;
  onReload: () => void;
  addToast: (message: string, type: "success" | "error") => void;
}) {
  const [assigning, setAssigning] = useState(false);
  const [driverId, setDriverId] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  const isOnHold = order.status === "on_hold";
  const canAssign = !isOnHold && !order.assigned_driver_id;

  const handleAssign = async () => {
    if (!driverId) return;
    setWorking(true);
    try {
      await assignDriver(order.order_id, { driver_id: driverId });
      addToast("Driver assigned", "success");
      onActioned(order.order_id);
    } catch (err) {
      addToast(
        err instanceof ApiError ? err.message : "Failed to assign driver",
        "error",
      );
    } finally {
      setWorking(false);
    }
  };

  const handleRelease = async () => {
    setWorking(true);
    try {
      const res = await releaseHoldOrder(order.order_id);
      if (res.status === "on_hold") {
        addToast(
          `Still on hold: ${res.hold_reason ?? "intake check failed"}`,
          "error",
        );
        onReload();
      } else {
        addToast("Hold released", "success");
        onActioned(order.order_id);
      }
    } catch (err) {
      addToast(
        err instanceof ApiError ? err.message : "Failed to release hold",
        "error",
      );
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="px-4 py-3 hover:bg-gray-50">
      <div className="flex items-center gap-3">
        <span className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-gray-50">
          <ClipboardList className="h-4 w-4 text-info" />
        </span>
        <button
          type="button"
          onClick={onOpen}
          className="min-w-0 flex-1 text-left focus:outline-none focus-visible:underline"
        >
          <p className="truncate text-sm font-medium text-gray-900">
            {order.customer_name || order.customer_id}
          </p>
          <p className="truncate text-xs text-gray-500">
            {order.ship_to_address}
          </p>
        </button>
        <span className="text-xs text-gray-500">
          {formatRelative(order.created_at)}
        </span>
        <Badge variant={isOnHold ? "warning" : "info"} size="sm">
          {isOnHold ? "On hold" : "Placed"}
        </Badge>
        {isOnHold ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={handleRelease}
            loading={working}
          >
            Release
          </Button>
        ) : canAssign ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setAssigning((v) => !v)}
          >
            Assign
          </Button>
        ) : null}
      </div>

      {assigning && canAssign && (
        <div className="mt-2 flex items-center gap-2 pl-11">
          <div className="flex-1">
            <DriverPicker
              value={driverId}
              onChange={setDriverId}
              aria-label="Driver"
            />
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={handleAssign}
            loading={working}
            disabled={!driverId}
          >
            Confirm
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setAssigning(false)}>
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}
