"use client";

/**
 * DispatchCockpit — the dispatcher's "Today" landing.
 *
 * A single "what needs my attention now" surface that aggregates the work
 * scattered across Orders, Dispatch, Fuel Ops, and the Control Center, and
 * deep-links each item to the place it's acted on. Read paths reuse existing
 * endpoints (orders list, delayed jobs, fuel alerts, agent approvals); each
 * source loads independently and fails open so one outage can't blank the
 * whole cockpit.
 */

import {
  AlertTriangle,
  ArrowRight,
  Bot,
  ChevronRight,
  ClipboardList,
  Clock,
  Fuel,
  RefreshCw,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
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

const REFRESH_INTERVAL_MS = 60_000;
const MAX_ROWS = 6;

interface DispatchCockpitProps {
  /** Navigate to a top-level dashboard module (e.g. "dispatch"). */
  onNavigate?: (item: string) => void;
}

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

export default function DispatchCockpit({ onNavigate }: DispatchCockpitProps) {
  const router = useRouter();
  const { toasts, addToast, dismissToast } = useToasts();
  const [orders, setOrders] = useState<FuelOrder[]>([]);
  const [delayedJobs, setDelayedJobs] = useState<Job[]>([]);
  const [fuelAlerts, setFuelAlerts] = useState<FuelAlert[]>([]);
  const [approvalCount, setApprovalCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    // "Needs attention" orders: newly placed (await scheduling/assignment)
    // plus anything parked on hold (awaits release).
    const orderStatuses: OrderStatus[] = ["placed", "on_hold"];
    const [placedRes, holdRes, delayedRes, fuelRes, approvalsRes] =
      await Promise.allSettled([
        listOrders({ status: orderStatuses[0], size: MAX_ROWS }),
        listOrders({ status: orderStatuses[1], size: MAX_ROWS }),
        getDelayedJobs(),
        getFuelAlerts(),
        getApprovals(getCurrentTenantId()),
      ]);

    const placed = placedRes.status === "fulfilled" ? placedRes.value.data : [];
    const held = holdRes.status === "fulfilled" ? holdRes.value.data : [];
    // De-dupe by order_id (a status can't be both, but guard anyway) and sort
    // newest-first so the freshest work surfaces at the top.
    const byId = new Map<string, FuelOrder>();
    for (const o of [...placed, ...held]) byId.set(o.order_id, o);
    setOrders(
      [...byId.values()].sort(
        (a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      ),
    );

    setDelayedJobs(
      delayedRes.status === "fulfilled" ? delayedRes.value.data : [],
    );
    setFuelAlerts(fuelRes.status === "fulfilled" ? fuelRes.value.data : []);

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
  }, []);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadData]);

  const openOrder = (orderId: string) =>
    router.push(`/orders/${encodeURIComponent(orderId)}`);

  // Drop an order from the attention list once it's been actioned inline. The
  // next refresh reconciles authoritative state from the server.
  const removeOrder = (orderId: string) =>
    setOrders((prev) => prev.filter((o) => o.order_id !== orderId));

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="Today"
        subtitle="What needs your attention right now"
        icon={<ClipboardList className="w-5 h-5" />}
        actions={
          <div className="flex items-center gap-3">
            {lastUpdated && (
              <span className="text-xs text-gray-400">
                Updated {formatRelative(lastUpdated.toISOString())}
              </span>
            )}
            <Button
              variant="secondary"
              size="sm"
              onClick={loadData}
              icon={
                <RefreshCw
                  className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`}
                />
              }
            >
              Refresh
            </Button>
          </div>
        }
      />

      <div className="flex-1 overflow-auto bg-gray-50 p-6 space-y-6">
        {/* KPI strip */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <KpiCard
            label="Orders to action"
            value={orders.length}
            icon={<ClipboardList className="w-5 h-5" />}
            tone="info"
            onClick={() => router.push("/ops")}
          />
          <KpiCard
            label="Delayed jobs"
            value={delayedJobs.length}
            icon={<Clock className="w-5 h-5" />}
            tone="error"
            onClick={() => onNavigate?.("dispatch")}
          />
          <KpiCard
            label="Fuel alerts"
            value={fuelAlerts.length}
            icon={<Fuel className="w-5 h-5" />}
            tone="warning"
            onClick={() => onNavigate?.("fuel-ops")}
          />
          <KpiCard
            label="Agent proposals"
            value={approvalCount}
            icon={<Bot className="w-5 h-5" />}
            tone="default"
            onClick={() => onNavigate?.("control")}
          />
        </div>

        {/* Attention queues */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
          <AttentionCard
            title="Orders needing attention"
            icon={<ClipboardList className="w-4 h-4 text-info" />}
            count={orders.length}
            loading={loading}
            emptyMessage="No orders waiting"
            footer={
              <button
                type="button"
                onClick={() => router.push("/ops")}
                className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
              >
                View all orders <ArrowRight className="w-3 h-3" />
              </button>
            }
          >
            {orders.slice(0, MAX_ROWS).map((order) => (
              <OrderAttentionRow
                key={order.order_id}
                order={order}
                onOpen={() => openOrder(order.order_id)}
                onActioned={removeOrder}
                onReload={loadData}
                addToast={addToast}
              />
            ))}
          </AttentionCard>

          <AttentionCard
            title="Delayed operations"
            icon={<Clock className="w-4 h-4 text-error" />}
            count={delayedJobs.length}
            loading={loading}
            emptyMessage="No delayed operations"
            footer={
              <button
                type="button"
                onClick={() => onNavigate?.("dispatch")}
                className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
              >
                Go to Dispatch <ArrowRight className="w-3 h-3" />
              </button>
            }
          >
            {[...delayedJobs]
              .sort(
                (a, b) =>
                  (b.delay_duration_minutes ?? 0) -
                  (a.delay_duration_minutes ?? 0),
              )
              .slice(0, MAX_ROWS)
              .map((job) => (
                <AttentionRow
                  key={job.job_id}
                  onClick={() => onNavigate?.("dispatch")}
                  title={job.job_id}
                  subtitle={`${job.origin} → ${job.destination}`}
                  badge={
                    <Badge variant="error" size="sm">
                      +{formatDelay(job.delay_duration_minutes)}
                    </Badge>
                  }
                />
              ))}
          </AttentionCard>

          <AttentionCard
            title="Fuel alerts"
            icon={<AlertTriangle className="w-4 h-4 text-warning" />}
            count={fuelAlerts.length}
            loading={loading}
            emptyMessage="All stations healthy"
            footer={
              <button
                type="button"
                onClick={() => onNavigate?.("fuel-ops")}
                className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
              >
                Go to Fuel Ops <ArrowRight className="w-3 h-3" />
              </button>
            }
          >
            {fuelAlerts.slice(0, MAX_ROWS).map((alert) => (
              <AttentionRow
                key={alert.station_id}
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
            ))}
          </AttentionCard>

          <AttentionCard
            title="Agent proposals"
            icon={<Bot className="w-4 h-4 text-gray-500" />}
            count={approvalCount}
            loading={loading}
            emptyMessage="No pending proposals"
            footer={
              <button
                type="button"
                onClick={() => onNavigate?.("control")}
                className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
              >
                Review in Control Center <ArrowRight className="w-3 h-3" />
              </button>
            }
          >
            {approvalCount > 0 && (
              <button
                type="button"
                onClick={() => onNavigate?.("control")}
                className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-gray-50"
              >
                <span className="text-sm text-gray-700">
                  {approvalCount} proposal{approvalCount === 1 ? "" : "s"}{" "}
                  awaiting review
                </span>
                <ChevronRight className="w-4 h-4 text-gray-400" />
              </button>
            )}
          </AttentionCard>
        </div>
      </div>

      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}

// ─── KPI Card ────────────────────────────────────────────────────────────────

const KPI_TONE: Record<string, string> = {
  info: "text-info",
  error: "text-error",
  warning: "text-warning",
  default: "text-primary",
};

function KpiCard({
  label,
  value,
  icon,
  tone,
  onClick,
}: {
  label: string;
  value: number;
  icon: React.ReactNode;
  tone: "info" | "error" | "warning" | "default";
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center justify-between rounded-xl border border-gray-200 bg-white p-5 text-left shadow-sm transition-shadow hover:shadow-md focus:outline-none focus:ring-2 focus:ring-primary/40"
    >
      <div>
        <p className="text-sm text-gray-500">{label}</p>
        <p className={`mt-1 text-3xl font-semibold ${KPI_TONE[tone]}`}>
          {value}
        </p>
      </div>
      <span className={`${KPI_TONE[tone]} opacity-80`}>{icon}</span>
    </button>
  );
}

// ─── Attention Card + Row ──────────────────────────────────────────────────

function AttentionCard({
  title,
  icon,
  count,
  loading,
  emptyMessage,
  footer,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  count: number;
  loading: boolean;
  emptyMessage: string;
  footer?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col rounded-xl border border-gray-200 bg-white shadow-sm">
      <div className="flex items-center gap-2 border-b border-gray-100 px-4 py-3">
        {icon}
        <h3 className="text-sm font-semibold text-primary">{title}</h3>
        <span className="ml-auto text-xs font-medium text-gray-400">
          {count}
        </span>
      </div>
      <div className="divide-y divide-gray-50">
        {loading ? (
          <div className="px-4 py-8 text-center text-sm text-gray-400">
            Loading…
          </div>
        ) : count === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-gray-400">
            {emptyMessage}
          </div>
        ) : (
          children
        )}
      </div>
      {footer && count > 0 && (
        <div className="mt-auto border-t border-gray-100 px-4 py-2.5">
          {footer}
        </div>
      )}
    </div>
  );
}

function AttentionRow({
  onClick,
  title,
  subtitle,
  meta,
  badge,
}: {
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
      className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-gray-50"
    >
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-gray-900">{title}</p>
        {subtitle && (
          <p className="truncate text-xs text-gray-500">{subtitle}</p>
        )}
      </div>
      {meta && <span className="text-xs text-gray-400">{meta}</span>}
      {badge}
      <ChevronRight className="w-4 h-4 flex-shrink-0 text-gray-300" />
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
      // The backend may keep the order on hold if a re-run intake hook fails.
      if (res.data?.status === "on_hold") {
        addToast(
          `Still on hold: ${res.data.hold_reason ?? "intake check failed"}`,
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
    <div className="px-4 py-2.5 hover:bg-gray-50">
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onOpen}
          className="min-w-0 flex-1 text-left"
        >
          <p className="truncate text-sm font-medium text-gray-900">
            {order.customer_name || order.customer_id}
          </p>
          <p className="truncate text-xs text-gray-500">
            {order.ship_to_address}
          </p>
        </button>
        <span className="text-xs text-gray-400">
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
        <div className="mt-2 flex items-center gap-2 pl-1">
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
