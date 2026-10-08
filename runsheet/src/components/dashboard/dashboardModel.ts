/**
 * Pure derivations for the Dashboard (UI revamp §7.1, R7): deep links, the
 * severity ranking of "Needs attention", one run per truck from the board
 * lanes (or from active jobs when the board is off), and plan status per
 * service day. Kept free of React so each rule is unit-tested.
 */
import type { ApprovalEntry } from "../../services/agentApi";
import type { BoardSnapshot, LaneView } from "../../services/dispatchBoardApi";
import type { FuelAlert, PlanListItem } from "../../services/fuelApi";
import type { FuelOrder } from "../../services/ordersApi";
import type { StatusKey } from "../../styles/tokens";
import type { Job } from "../../types/api";
import { statusKeyFor } from "../ui/StatusBadge";

// ── Links ──────────────────────────────────────────────────────────────────

/**
 * The board's own deep link (`dispatch-board/viewState.ts`): the lane
 * scrolls into view and the order is selected (R7.4).
 */
export function boardLink({
  date,
  truck,
  order,
  status,
}: {
  date?: string | null;
  truck?: string | null;
  order?: string | null;
  status?: string | null;
}): string {
  const p = new URLSearchParams({ tab: "board" });
  if (date) p.set("date", date);
  if (truck) p.set("truck", truck);
  if (order) p.set("order", order);
  if (status) p.set("status", status);
  return `/dashboard/dispatch?${p.toString()}`;
}

export const LINKS = {
  jobs: (status?: string) =>
    `/dashboard/dispatch?tab=jobs${status ? `&status=${encodeURIComponent(status)}` : ""}`,
  job: (id: string) => `/dashboard/dispatch/jobs/${encodeURIComponent(id)}`,
  plans: "/dashboard/dispatch?tab=plans",
  live: "/dashboard/control",
  approvals: (id?: string) =>
    `/dashboard/control?tab=approvals${id ? `&id=${encodeURIComponent(id)}` : ""}`,
  order: (id: string) => `/dashboard/orders/${encodeURIComponent(id)}`,
  orders: (status?: string) =>
    `/dashboard/orders${status ? `?status=${encodeURIComponent(status)}` : ""}`,
  station: (id: string) =>
    `/dashboard/fuel-ops?tab=stations&station=${encodeURIComponent(id)}`,
} as const;

/** YYYY-MM-DD of a timestamp in a time zone (the board's service day). */
export function dayOf(
  iso: string | null | undefined,
  timeZone: string,
): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

// ── Needs attention ────────────────────────────────────────────────────────

export type AttentionKind =
  | "exception"
  | "delayed"
  | "approval"
  | "order"
  | "tank";

export interface BoardException {
  truckId: string;
  orderId: string;
  customerId: string | null;
  productCode: string | null;
  status: string | null;
}

export type AttentionItem =
  | {
      kind: "exception";
      key: string;
      severity: number;
      exception: BoardException;
    }
  | { kind: "delayed"; key: string; severity: number; job: Job }
  | { kind: "approval"; key: string; severity: number; approval: ApprovalEntry }
  | { kind: "order"; key: string; severity: number; order: FuelOrder }
  | { kind: "tank"; key: string; severity: number; alert: FuelAlert };

/*
 * Cross-category severity (kept from the Today cockpit, plus exceptions and
 * approvals): an empty or critical tank outranks a delay, an exception
 * outranks everything except an empty tank, approvals sit above fresh
 * orders, on-hold orders above placed ones.
 */
export function tankSeverity(a: FuelAlert): number {
  const base =
    a.status === "empty" ? 4000 : a.status === "critical" ? 3000 : 2000;
  return base - (a.stock_percentage ?? 0);
}
export function delaySeverity(j: Job): number {
  return 1000 + Math.min(j.delay_duration_minutes ?? 0, 900);
}
export const EXCEPTION_SEVERITY = 3500;
export const APPROVAL_SEVERITY = 800;
export function orderSeverity(o: FuelOrder): number {
  return o.status === "on_hold" ? 600 : 400;
}

/** Stops on the board whose order status is an exception (failed, rejected). */
export function boardExceptions(lanes: LaneView[]): BoardException[] {
  const out: BoardException[] = [];
  for (const lane of lanes) {
    for (const load of lane.loads) {
      for (const stop of load.stops) {
        if (statusKeyFor(stop.snapshot.status) === "exception") {
          out.push({
            truckId: lane.truck_id,
            orderId: stop.order_id,
            customerId: stop.snapshot.customer_id,
            productCode: stop.snapshot.product_code,
            status: stop.snapshot.status,
          });
        }
      }
    }
  }
  return out;
}

export interface AttentionSources {
  exceptions: BoardException[];
  delayed: Job[];
  approvals: ApprovalEntry[];
  orders: FuelOrder[];
  tanks: FuelAlert[];
}

/** One severity-ranked feed (highest first). */
export function rankAttention(s: AttentionSources): AttentionItem[] {
  const items: AttentionItem[] = [
    ...s.exceptions.map(
      (exception): AttentionItem => ({
        kind: "exception",
        key: `exception-${exception.truckId}-${exception.orderId}`,
        severity: EXCEPTION_SEVERITY,
        exception,
      }),
    ),
    ...s.delayed.map(
      (job): AttentionItem => ({
        kind: "delayed",
        key: `delayed-${job.job_id}`,
        severity: delaySeverity(job),
        job,
      }),
    ),
    ...s.approvals.map(
      (approval): AttentionItem => ({
        kind: "approval",
        key: `approval-${approval.action_id}`,
        severity:
          APPROVAL_SEVERITY +
          (approval.risk_level === "high"
            ? 100
            : approval.risk_level === "medium"
              ? 50
              : 0),
        approval,
      }),
    ),
    ...s.orders.map(
      (order): AttentionItem => ({
        kind: "order",
        key: `order-${order.order_id}`,
        severity: orderSeverity(order),
        order,
      }),
    ),
    ...s.tanks.map(
      (alert): AttentionItem => ({
        kind: "tank",
        key: `tank-${alert.station_id}`,
        severity: tankSeverity(alert),
        alert,
      }),
    ),
  ];
  // Stable: equal severities keep source order.
  return items
    .map((it, i) => ({ it, i }))
    .sort((a, b) => b.it.severity - a.it.severity || a.i - b.i)
    .map(({ it }) => it);
}

// ── Today's runs ───────────────────────────────────────────────────────────

export interface RunSegment {
  status: StatusKey;
  count: number;
}

export interface Run {
  truckId: string;
  driverName: string | null;
  /** Stop counts by display status, in progress order. */
  segments: RunSegment[];
  total: number;
  done: number;
  status: StatusKey;
  /** A label for the badge, e.g. "In transit". */
  label: string;
  href: string;
}

const SEGMENT_ORDER: StatusKey[] = [
  "delivered",
  "in_transit",
  "dispatched",
  "delayed",
  "exception",
  "planned",
  "draft",
  "cancelled",
];

const RUN_LABEL: Partial<Record<StatusKey, string>> = {
  exception: "Exception",
  delayed: "Delayed",
  in_transit: "In transit",
  dispatched: "Dispatched",
  delivered: "Delivered",
  planned: "Planned",
  draft: "Draft",
};

/** A run's overall status: the most urgent active state of its stops. */
export function runStatus(
  counts: Map<StatusKey, number>,
  total: number,
): StatusKey {
  const has = (s: StatusKey) => (counts.get(s) ?? 0) > 0;
  if (has("exception")) return "exception";
  if (has("delayed")) return "delayed";
  if (has("in_transit")) return "in_transit";
  if (total > 0 && (counts.get("delivered") ?? 0) === total) return "delivered";
  if (has("dispatched")) return "dispatched";
  if (has("planned")) return "planned";
  return "draft";
}

function toRun(
  truckId: string,
  driverName: string | null,
  statuses: StatusKey[],
  href: string,
): Run {
  const counts = new Map<StatusKey, number>();
  for (const s of statuses) counts.set(s, (counts.get(s) ?? 0) + 1);
  const total = statuses.length;
  const status = runStatus(counts, total);
  return {
    truckId,
    driverName,
    segments: SEGMENT_ORDER.filter((s) => counts.has(s)).map((s) => ({
      status: s,
      count: counts.get(s) ?? 0,
    })),
    total,
    done: counts.get("delivered") ?? 0,
    status,
    label: RUN_LABEL[status] ?? status,
    href,
  };
}

/** Board lanes with work, one run each (a published lane's stops default to Dispatched). */
export function runsFromLanes(lanes: LaneView[], date: string): Run[] {
  return lanes
    .filter((l) => l.loads.some((ld) => ld.stops.length > 0))
    .map((lane) => {
      const published = lane.state === "published" || lane.ever_published;
      const statuses = lane.loads.flatMap((ld) =>
        ld.stops.map((s): StatusKey => {
          const k = statusKeyFor(s.snapshot.status);
          if (k && k !== "draft" && k !== "planned") return k;
          return published ? "dispatched" : "planned";
        }),
      );
      return toRun(
        lane.truck_id,
        lane.driver?.name ?? lane.driver_id ?? null,
        statuses,
        boardLink({ date, truck: lane.truck_id }),
      );
    });
}

/** Without the board: active and delayed jobs grouped by truck. */
export function runsFromJobs(jobs: Job[]): Run[] {
  const byTruck = new Map<string, Job[]>();
  for (const j of jobs) {
    const t = j.asset_assigned;
    if (!t) continue;
    byTruck.set(t, [...(byTruck.get(t) ?? []), j]);
  }
  return [...byTruck.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([truck, list]) =>
      toRun(
        truck,
        null,
        list.map((j): StatusKey => {
          if (j.delayed) return "delayed";
          return statusKeyFor(j.status) ?? "planned";
        }),
        LINKS.jobs(),
      ),
    );
}

// ── Plan status ────────────────────────────────────────────────────────────

export interface PlanLine {
  key: string;
  status: StatusKey;
  label: string;
  day: "Today" | "Tomorrow";
  loads: number;
  trucks: number;
  warnings: number;
  href: string | null;
  action: string | null;
}

/** Draft (unpublished changes) and published lanes for one board day. */
export function planLines(
  snap: BoardSnapshot | null,
  day: "Today" | "Tomorrow",
): PlanLine[] {
  if (!snap) return [];
  const withLoads = snap.lanes.filter((l) => l.loads.length > 0);
  const draft = withLoads.filter(
    (l) =>
      l.state === "draft" || l.state === "modified" || l.state === "failed",
  );
  const published = withLoads.filter(
    (l) => l.state === "published" || l.state === "publishing",
  );
  const loads = (ls: LaneView[]) => ls.reduce((n, l) => n + l.loads.length, 0);
  const warnings = (ls: LaneView[]) =>
    ls.reduce(
      (n, l) =>
        n +
        l.checks.filter(
          (c) =>
            (c.outcome === "warn" || c.outcome === "block") &&
            !(c.warning_id && snap.acknowledged[c.warning_id]),
        ).length,
      0,
    );
  const out: PlanLine[] = [];
  const href = boardLink({ date: snap.service_date });
  if (draft.length > 0 || snap.suggestions.length > 0) {
    out.push({
      key: `${day}-draft`,
      status: "planned",
      label: draft.length > 0 ? "Draft plan" : "Suggestions",
      day,
      loads: loads(draft),
      trucks: draft.length,
      warnings: warnings(draft),
      href,
      action: "Review on board",
    });
  }
  if (published.length > 0) {
    out.push({
      key: `${day}-published`,
      status: "dispatched",
      label: "Published",
      day,
      loads: loads(published),
      trucks: published.length,
      warnings: 0,
      href,
      action: null,
    });
  }
  return out;
}

/** Without the board: the legacy plan list summarised by status. */
export function planLinesFromList(plans: PlanListItem[]): PlanLine[] {
  const by = new Map<string, PlanListItem[]>();
  for (const p of plans) by.set(p.status, [...(by.get(p.status) ?? []), p]);
  return [...by.entries()].map(([status, list]) => ({
    key: `list-${status}`,
    status: statusKeyFor(status) ?? "planned",
    label: status.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()),
    day: "Today",
    loads: list.length,
    trucks: new Set(list.map((p) => p.truck_id)).size,
    warnings: 0,
    href: LINKS.plans,
    action: status === "draft" ? "Review" : null,
  }));
}

// ── Title counts ───────────────────────────────────────────────────────────

export interface TitleCounts {
  loads: number;
  trucksOut: number;
  delayed: number;
  exceptions: number;
  approvals: number;
}

export function boardCounts(
  lanes: LaneView[],
): Pick<TitleCounts, "loads" | "trucksOut"> {
  let loads = 0;
  let trucksOut = 0;
  for (const lane of lanes) {
    loads += lane.loads.length;
    const out = lane.loads.some((ld) =>
      ld.stops.some((s) => statusKeyFor(s.snapshot.status) === "in_transit"),
    );
    if (out) trucksOut += 1;
  }
  return { loads, trucksOut };
}

export function jobCounts(
  active: Job[],
): Pick<TitleCounts, "loads" | "trucksOut"> {
  const out = new Set(
    active
      .filter(
        (j) => statusKeyFor(j.status) === "in_transit" && j.asset_assigned,
      )
      .map((j) => j.asset_assigned as string),
  );
  return { loads: active.length, trucksOut: out.size };
}

/** "Route plan for 4 loads" from an approval entry. */
export function approvalTitle(a: ApprovalEntry): string {
  if (a.impact_summary) return a.impact_summary;
  const words = (a.action_type || a.tool_name || "Agent action").replace(
    /_/g,
    " ",
  );
  return words.charAt(0).toUpperCase() + words.slice(1);
}
