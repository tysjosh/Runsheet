"use client";

/**
 * Dashboard: the home page (`/dashboard`, UI revamp §7.1, R7,
 * `mockup-dashboard.html`).
 *
 * Title row: the service day (Today / Tomorrow) and inline counts (loads,
 * trucks out, delayed, exceptions, approvals), each linking to a filtered
 * destination. Three widgets, each with its own load and error state
 * (`Promise.allSettled`, R11.2):
 *
 * - Needs attention: exceptions, delays, approvals (inline Approve / Review),
 *   placed and on-hold orders, low tanks, ranked by severity.
 * - Today's runs: one row per truck with its identity stripe, driver, a
 *   progress bar by stop status and a status badge; each row opens its lane.
 * - Plan status: draft and published plans for today and tomorrow.
 *
 * Live updates come from the orders and scheduling sockets plus a slow
 * safety-net poll; background refreshes never put skeletons over data. The
 * board socket is not joined here, because joining it announces presence
 * ("Also viewing") to dispatchers on the board.
 */
import { CircleCheck, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type CreateOrderPrefill,
  useDashboardChrome,
} from "../../app/dashboard/shell-context";
import { useOrdersWebSocket } from "../../hooks/useOrdersWebSocket";
import { useSchedulingWebSocket } from "../../hooks/useSchedulingWebSocket";
import {
  date as formatDate,
  number as formatNumber,
  gallons,
  productName,
  relative,
  time,
} from "../../lib/format";
import { identityFor } from "../../lib/identity";
import {
  type ApprovalEntry,
  approveAction,
  getApprovals,
} from "../../services/agentApi";
import { ApiError } from "../../services/api";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import {
  type BoardMode,
  type BoardSnapshot,
  boardErrorStatus,
  getBoard,
  getBoardStatus,
} from "../../services/dispatchBoardApi";
import {
  type FuelAlert,
  getAlerts as getFuelAlerts,
  listPlans,
  NO_CONSUMPTION_DAYS,
  type PlanListItem,
} from "../../services/fuelApi";
import { type FuelOrder, listOrders } from "../../services/ordersApi";
import { getActiveJobs, getDelayedJobs } from "../../services/schedulingApi";
import { getCurrentTenantId } from "../../services/tenant";
import { STATUS } from "../../styles/tokens";
import type { Job } from "../../types/api";
import { addDays, todayIn } from "../dispatch-board/viewState";
import { useTenantSettings } from "../shell/TenantSettings";
import {
  Button,
  FilterChips,
  IconButton,
  LoadErrorState,
  PageHeader,
  ProductCap,
  Skeleton,
  StatusBadge,
} from "../ui";
import { notify } from "../ui/toast/notify";
import {
  type AttentionItem,
  type AttentionKind,
  approvalTitle,
  boardCounts,
  boardExceptions,
  boardLink,
  exceptionsLink,
  jobCounts,
  LINKS,
  type PlanLine,
  planLines,
  planLinesFromList,
  type Run,
  rankAttention,
  runsFromJobs,
  runsFromLanes,
} from "./dashboardModel";
import { OrderAttentionRow } from "./OrderAttentionRow";

/** Safety-net poll; sockets carry live changes. */
export const REFRESH_INTERVAL_MS = 120_000;
const WS_REFRESH_DEBOUNCE_MS = 500;
const ORDERS_PER_STATUS = 8;
/** Feed rows shown before "Show all". */
export const FEED_LIMIT = 10;
/** Runs shown before "All on the board". */
export const RUNS_LIMIT = 8;

type Day = "today" | "tomorrow";

interface Slot<T> {
  data: T | null;
  failure: LoadFailure | null;
}
const empty = <T,>(): Slot<T> => ({ data: null, failure: null });

function listOf<T>(v: unknown): T[] {
  const o = v as { data?: unknown; items?: unknown; entries?: unknown } | null;
  const list = o?.data ?? o?.items ?? o?.entries;
  return Array.isArray(list) ? (list as T[]) : [];
}
function totalOf(v: unknown): number {
  const o = v as { total?: unknown } | null;
  return typeof o?.total === "number" ? o.total : listOf(v).length;
}

interface DashboardData {
  boardMode: BoardMode | null;
  orders: Slot<{ rows: FuelOrder[]; total: number }>;
  delayed: Slot<Job[]>;
  tanks: Slot<FuelAlert[]>;
  approvals: Slot<ApprovalEntry[]>;
  board: Record<Day, Slot<BoardSnapshot>>;
  active: Slot<Job[]>;
  plans: Slot<PlanListItem[]>;
}

const INITIAL: DashboardData = {
  boardMode: null,
  orders: empty(),
  delayed: empty(),
  tanks: empty(),
  approvals: empty(),
  board: { today: empty(), tomorrow: empty() },
  active: empty(),
  plans: empty(),
};

function settle<T>(r: PromiseSettledResult<T>, fallback: string): Slot<T> {
  return r.status === "fulfilled"
    ? { data: r.value, failure: null }
    : { data: null, failure: classifyLoadError(r.reason, fallback) };
}

export interface DashboardProps {
  /** Opens the shell's Create order dialog (low-tank rows), prefilled. */
  onCreateOrder?: (prefill?: CreateOrderPrefill) => void;
}

export default function Dashboard({ onCreateOrder }: DashboardProps = {}) {
  const { timeZone } = useTenantSettings();
  const chrome = useDashboardChrome();
  const createOrder = onCreateOrder ?? chrome.openCreateOrder;
  const today = todayIn(timeZone);
  const tomorrow = addDays(today, 1);
  const [day, setDay] = useState<Day>("today");
  const [data, setData] = useState<DashboardData>(INITIAL);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [, setTick] = useState(0);
  const datesRef = useRef({ today, tomorrow });
  datesRef.current = { today, tomorrow };

  const loadData = useCallback(async (opts?: { background?: boolean }) => {
    if (!opts?.background) setRefreshing(true);
    const tenant = getCurrentTenantId();
    const { today: d0, tomorrow: d1 } = datesRef.current;
    const statusP = getBoardStatus()
      .then((r) => r.mode)
      .catch((err) => {
        const s = boardErrorStatus(err);
        if (s === 404 || s === 403) return null;
        throw err;
      });
    const [placedR, heldR, delayedR, tanksR, approvalsR, modeR] =
      await Promise.allSettled([
        listOrders({ status: "placed", size: ORDERS_PER_STATUS }),
        listOrders({ status: "on_hold", size: ORDERS_PER_STATUS }),
        getDelayedJobs(),
        getFuelAlerts(),
        getApprovals(tenant),
        statusP,
      ]);
    const mode = modeR.status === "fulfilled" ? modeR.value : null;
    let board: DashboardData["board"] = { today: empty(), tomorrow: empty() };
    let active: Slot<Job[]> = empty();
    let plans: Slot<PlanListItem[]> = empty();
    if (mode) {
      const [b0, b1] = await Promise.allSettled([getBoard(d0), getBoard(d1)]);
      board = {
        today: settle(b0, "Today's board couldn't be loaded."),
        tomorrow: settle(b1, "Tomorrow's board couldn't be loaded."),
      };
    } else {
      const [a, p] = await Promise.allSettled([
        getActiveJobs(),
        listPlans(tenant, 1, 50),
      ]);
      const aS = settle(a, "Active jobs couldn't be loaded.");
      active = {
        failure: aS.failure,
        data: aS.failure ? null : listOf<Job>(aS.data),
      };
      const pS = settle(p, "Plans couldn't be loaded.");
      plans = {
        failure: pS.failure,
        data: pS.failure ? null : listOf<PlanListItem>(pS.data),
      };
    }

    const placed = settle(placedR, "Orders couldn't be loaded.");
    const held = settle(heldR, "Orders couldn't be loaded.");
    const byId = new Map<string, FuelOrder>();
    for (const o of [
      ...listOf<FuelOrder>(held.data),
      ...listOf<FuelOrder>(placed.data),
    ])
      byId.set(o.order_id, o);
    const orders: DashboardData["orders"] =
      placed.failure && held.failure
        ? { data: null, failure: placed.failure }
        : {
            data: {
              rows: [...byId.values()],
              total: totalOf(placed.data) + totalOf(held.data),
            },
            failure: null,
          };
    const delayed = settle(delayedR, "Delayed jobs couldn't be loaded.");
    const tanks = settle(tanksR, "Tank alerts couldn't be loaded.");
    const approvals = settle(approvalsR, "Approvals couldn't be loaded.");
    setData({
      boardMode: mode,
      orders,
      delayed: {
        ...delayed,
        data: delayed.failure ? null : listOf<Job>(delayed.data),
      },
      tanks: {
        ...tanks,
        data: tanks.failure ? null : listOf<FuelAlert>(tanks.data),
      },
      approvals: {
        ...approvals,
        data: approvals.failure
          ? null
          : listOf<ApprovalEntry>(approvals.data).filter(
              (a) => !a.status || a.status === "pending",
            ),
      },
      board,
      active,
      plans,
    });
    setLastUpdated(Date.now());
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void loadData();
    const poll = setInterval(
      () => void loadData({ background: true }),
      REFRESH_INTERVAL_MS,
    );
    // Keeps "updated 12 s ago" current.
    const tick = setInterval(() => setTick((t) => t + 1), 15_000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [loadData]);

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleRefresh = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      timer.current = null;
      void loadData({ background: true });
    }, WS_REFRESH_DEBOUNCE_MS);
  }, [loadData]);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
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

  // ── Derived ──────────────────────────────────────────────────────────────
  const boardOn = data.boardMode !== null;
  const dayDate = day === "today" ? today : tomorrow;
  const daySnap = data.board[day].data;
  const todaySnap = data.board.today.data;
  const exceptions = useMemo(
    () => (todaySnap ? boardExceptions(todaySnap.lanes) : []),
    [todaySnap],
  );
  const counts = useMemo(() => {
    const base = boardOn
      ? boardCounts(daySnap?.lanes ?? [])
      : jobCounts(data.active.data ?? []);
    return {
      ...base,
      delayed: day === "today" ? (data.delayed.data?.length ?? 0) : 0,
      exceptions: day === "today" ? exceptions.length : 0,
      approvals: data.approvals.data?.length ?? 0,
    };
  }, [
    boardOn,
    daySnap,
    data.active.data,
    data.delayed.data,
    data.approvals.data,
    exceptions,
    day,
  ]);

  const removeOrder = (id: string) =>
    setData((d) =>
      d.orders.data
        ? {
            ...d,
            orders: {
              ...d.orders,
              data: {
                rows: d.orders.data.rows.filter((o) => o.order_id !== id),
                total: Math.max(0, d.orders.data.total - 1),
              },
            },
          }
        : d,
    );
  const removeApproval = (id: string) =>
    setData((d) => ({
      ...d,
      approvals: {
        ...d.approvals,
        data: (d.approvals.data ?? []).filter((a) => a.action_id !== id),
      },
    }));

  const failedAttention = [
    data.orders.failure,
    data.delayed.failure,
    data.tanks.failure,
    data.approvals.failure,
  ].filter((f): f is LoadFailure => f !== null);

  const countLinks = [
    {
      id: "loads",
      n: counts.loads,
      label: boardOn
        ? counts.loads === 1
          ? "load"
          : "loads"
        : counts.loads === 1
          ? "job"
          : "jobs",
      href: boardOn ? boardLink({ date: dayDate }) : LINKS.jobs(),
      color: "text-slate-900",
    },
    {
      id: "trucks",
      n: counts.trucksOut,
      label: counts.trucksOut === 1 ? "truck out" : "trucks out",
      href: LINKS.live,
      color: "text-slate-900",
    },
    {
      id: "delayed",
      n: counts.delayed,
      label: "delayed",
      // The count is `/scheduling/jobs/delayed`; the board has no "delayed"
      // status to filter on, so both modes open the Jobs Delayed chip.
      href: LINKS.jobs("delayed"),
      color: "text-amber-800",
    },
    {
      id: "exceptions",
      n: counts.exceptions,
      label: counts.exceptions === 1 ? "exception" : "exceptions",
      // The count is today's board stops in an exception status, so it opens
      // today's board filtered to exactly those statuses.
      href: exceptionsLink(exceptions, today),
      color: "text-red-800",
    },
    {
      id: "approvals",
      n: counts.approvals,
      label: counts.approvals === 1 ? "approval" : "approvals",
      href: LINKS.approvals(),
      color: "text-fuchsia-800",
    },
  ];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Dashboard"
        help="What needs attention now, today's runs and plan status. Every count and row opens where you act on it."
        context={
          <div
            role="group"
            aria-label="Service day"
            className="inline-flex h-7 shrink-0 overflow-hidden rounded-lg border border-slate-300 text-xs font-semibold"
          >
            {(["today", "tomorrow"] as const).map((d) => (
              <button
                key={d}
                type="button"
                aria-pressed={day === d}
                onClick={() => setDay(d)}
                className={`px-2.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus ${
                  day === d
                    ? "bg-primary-soft text-brand-800 shadow-[inset_0_-2px_0_var(--rs-primary)]"
                    : "bg-surface text-slate-700 hover:bg-slate-50"
                }`}
              >
                {d === "today" ? "Today" : "Tomorrow"}
                {day === d && (
                  <span className="font-medium">
                    {" "}
                    ·{" "}
                    {formatDate(
                      `${d === "today" ? today : tomorrow}T12:00:00Z`,
                      { timeZone: "UTC" },
                    )}
                  </span>
                )}
              </button>
            ))}
          </div>
        }
        counts={
          <ul aria-label="Counts" className="flex min-w-0 items-center gap-3.5">
            {countLinks.map((c) => (
              <li key={c.id} className="shrink-0">
                <Link
                  href={c.href}
                  className="rounded text-xs text-text-muted hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                >
                  <b
                    className={`text-sm font-bold tabular-nums ${c.n > 0 ? c.color : "text-slate-900"}`}
                  >
                    {loading ? "–" : formatNumber(c.n)}
                  </b>{" "}
                  {c.label}
                </Link>
              </li>
            ))}
          </ul>
        }
        actions={
          <>
            {lastUpdated && (
              <span className="hidden text-xs text-text-muted xl:inline">
                Live · updated {relative(lastUpdated)}
              </span>
            )}
            <IconButton
              label="Refresh"
              size="sm"
              onClick={() => void loadData()}
              icon={
                <RefreshCw
                  className={`h-3.5 w-3.5 ${refreshing ? "motion-safe:animate-spin" : ""}`}
                />
              }
            />
          </>
        }
      />
      <div className="min-h-0 flex-1 overflow-auto bg-canvas p-3">
        <div className="grid items-start gap-3 lg:grid-cols-[1.35fr_1fr]">
          <NeedsAttention
            loading={loading}
            failures={failedAttention}
            allFailed={failedAttention.length === 4}
            onRetry={() => void loadData()}
            boardOn={boardOn}
            today={today}
            timeZone={timeZone}
            sources={{
              exceptions,
              delayed: data.delayed.data ?? [],
              approvals: data.approvals.data ?? [],
              orders: data.orders.data?.rows ?? [],
              tanks: data.tanks.data ?? [],
            }}
            orderTotal={data.orders.data?.total ?? 0}
            onOrderActioned={removeOrder}
            onApprovalDone={removeApproval}
            onReload={() => void loadData({ background: true })}
            onCreateOrder={createOrder}
          />
          <div className="grid gap-3">
            <TodaysRuns
              loading={loading}
              boardOn={boardOn}
              day={day}
              runs={
                boardOn
                  ? daySnap
                    ? runsFromLanes(daySnap.lanes, dayDate)
                    : null
                  : data.active.data
                    ? runsFromJobs(data.active.data)
                    : null
              }
              failure={boardOn ? data.board[day].failure : data.active.failure}
              href={boardOn ? boardLink({ date: dayDate }) : LINKS.jobs()}
              onRetry={() => void loadData()}
            />
            <PlanStatus
              loading={loading}
              lines={
                boardOn
                  ? [
                      ...planLines(data.board.today.data, "Today"),
                      ...planLines(data.board.tomorrow.data, "Tomorrow"),
                    ]
                  : planLinesFromList(data.plans.data ?? [])
              }
              failure={
                boardOn
                  ? (data.board.today.failure ?? data.board.tomorrow.failure)
                  : data.plans.failure
              }
              href={boardOn ? boardLink({ date: tomorrow }) : LINKS.plans}
              onRetry={() => void loadData()}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Panel ──────────────────────────────────────────────────────────────────

function Panel({
  title,
  accent,
  link,
  extra,
  children,
  id,
}: {
  title: string;
  accent: string;
  link?: { href: string; label: string };
  extra?: React.ReactNode;
  children: React.ReactNode;
  id: string;
}) {
  return (
    <section
      aria-labelledby={id}
      className="overflow-hidden rounded-[10px] border border-slate-200 bg-surface"
    >
      <div className="flex h-10 items-center gap-2 border-b border-slate-200 px-3">
        <span
          aria-hidden="true"
          className="h-4 w-1 shrink-0 rounded-sm"
          style={{ backgroundColor: accent }}
        />
        <h2
          id={id}
          className="shrink-0 text-[13px] font-semibold text-slate-900"
        >
          {link ? (
            <Link
              href={link.href}
              className="hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              {title}
            </Link>
          ) : (
            title
          )}
        </h2>
        {extra}
        {link && (
          <Link
            href={link.href}
            className="ml-auto shrink-0 rounded text-xs font-semibold text-link hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            {link.label} →
          </Link>
        )}
      </div>
      {children}
    </section>
  );
}

function WidgetError({
  failure,
  label,
  onRetry,
}: {
  failure: LoadFailure;
  label: string;
  onRetry: () => void;
}) {
  return (
    <div className="p-3 text-sm">
      <LoadErrorState
        failure={failure}
        entityLabel={label}
        onRetry={onRetry}
        embedded
      />
    </div>
  );
}

// ── Needs attention ────────────────────────────────────────────────────────

const KIND_CHIPS: { id: "all" | AttentionKind; label: string }[] = [
  { id: "all", label: "All" },
  { id: "order", label: "Orders" },
  { id: "exception", label: "Exceptions" },
  { id: "delayed", label: "Delayed" },
  { id: "tank", label: "Tanks" },
  { id: "approval", label: "Approvals" },
];

function NeedsAttention({
  loading,
  failures,
  allFailed,
  onRetry,
  boardOn,
  today,
  timeZone,
  sources,
  orderTotal,
  onOrderActioned,
  onApprovalDone,
  onReload,
  onCreateOrder,
}: {
  loading: boolean;
  failures: LoadFailure[];
  allFailed: boolean;
  onRetry: () => void;
  boardOn: boolean;
  today: string;
  timeZone: string;
  sources: Parameters<typeof rankAttention>[0];
  orderTotal: number;
  onOrderActioned: (id: string) => void;
  onApprovalDone: (id: string) => void;
  onReload: () => void;
  onCreateOrder?: (prefill?: CreateOrderPrefill) => void;
}) {
  const [filter, setFilter] = useState<"all" | AttentionKind>("all");
  const [showAll, setShowAll] = useState(false);
  const all = useMemo(() => rankAttention(sources), [sources]);
  const countOf = (k: AttentionKind) =>
    k === "order"
      ? Math.max(orderTotal, sources.orders.length)
      : all.filter((i) => i.kind === k).length;
  const total =
    all.length -
    sources.orders.length +
    Math.max(orderTotal, sources.orders.length);
  const chips = KIND_CHIPS.map((c) => ({
    id: c.id,
    label: c.label,
    count: c.id === "all" ? total : countOf(c.id),
  })).filter((c) => c.id === "all" || c.count > 0 || c.id === filter);
  const items = filter === "all" ? all : all.filter((i) => i.kind === filter);
  const shown = showAll ? items : items.slice(0, FEED_LIMIT);

  return (
    <Panel
      id="dash-attention"
      title="Needs attention"
      accent={STATUS.exception.dot}
      extra={
        <FilterChips
          label="Filter attention items"
          options={chips}
          value={filter}
          onChange={(v) => {
            setFilter(v as "all" | AttentionKind);
            setShowAll(false);
          }}
          className="ml-1 overflow-x-auto [&>button]:h-6 [&>button]:px-2"
        />
      }
    >
      {loading ? (
        <Skeleton
          rows={6}
          height={28}
          label="Loading attention items"
          className="p-3"
        />
      ) : allFailed ? (
        <WidgetError
          failure={failures[0]}
          label="Attention items"
          onRetry={onRetry}
        />
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center gap-1.5 px-4 py-10 text-center">
          <CircleCheck aria-hidden="true" className="h-7 w-7 text-brand-600" />
          <p className="text-sm font-semibold text-slate-800">
            You&apos;re all caught up
          </p>
          <p className="text-xs text-text-muted">
            {filter === "all"
              ? "No exceptions, delays, approvals, waiting orders or low tanks."
              : "Nothing needs attention in this category."}
          </p>
        </div>
      ) : (
        <ul aria-label="Attention items">
          {shown.map((item) => (
            <AttentionRow
              key={item.key}
              item={item}
              boardOn={boardOn}
              today={today}
              timeZone={timeZone}
              onOrderActioned={onOrderActioned}
              onApprovalDone={onApprovalDone}
              onReload={onReload}
              onCreateOrder={onCreateOrder}
            />
          ))}
        </ul>
      )}
      {!loading && !allFailed && (items.length > 0 || failures.length > 0) && (
        <div className="flex h-10 items-center gap-2 border-t border-slate-100 px-3 text-xs text-text-muted">
          {items.length > 0 && (
            <span>
              Showing {formatNumber(shown.length)} of{" "}
              {formatNumber(items.length)} · ranked by severity
            </span>
          )}
          {failures.length > 0 && (
            <span role="status" className="font-medium text-amber-800">
              {failures.length === 1
                ? "One source"
                : `${failures.length} sources`}{" "}
              couldn&apos;t load.
              <button
                type="button"
                onClick={onRetry}
                className="ml-1 font-semibold text-link underline"
              >
                Retry
              </button>
            </span>
          )}
          {items.length > FEED_LIMIT && (
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              className="ml-auto rounded font-semibold text-link hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              {showAll ? "Show fewer" : "View all"}
            </button>
          )}
        </div>
      )}
    </Panel>
  );
}

const rowBtn =
  "inline-flex h-7 shrink-0 items-center rounded-lg border border-slate-300 bg-surface px-2.5 text-xs font-semibold text-slate-800 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus";

function Row({ children }: { children: React.ReactNode }) {
  return (
    <li
      data-feed-row
      className="flex min-h-11 items-center gap-2.5 border-b border-slate-100 px-3 last:border-b-0"
    >
      {children}
    </li>
  );
}

function delayText(minutes?: number): string {
  if (!minutes || minutes <= 0) return "Delayed";
  if (minutes < 60) return `+${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m > 0 ? `+${h} h ${m} min` : `+${h} h`;
}

/** Product and the gallons that refill the tank, for Create order. */
export function tankPrefill(a: FuelAlert): CreateOrderPrefill {
  const gap =
    typeof a.capacity_gallons === "number" &&
    typeof a.current_stock_gallons === "number"
      ? Math.max(0, Math.round(a.capacity_gallons - a.current_stock_gallons))
      : null;
  return {
    product_code: a.fuel_type ? String(a.fuel_type) : "",
    gallons_requested: gap ? String(gap) : "",
    special_instructions: `Refill ${a.name}`,
  };
}

function runout(days: number | undefined): string | null {
  if (typeof days !== "number" || !Number.isFinite(days)) return null;
  // 99999 is the backend's "no consumption" sentinel, not a runout (F5).
  if (days >= NO_CONSUMPTION_DAYS) return null;
  const hours = Math.round(days * 24);
  return hours < 48
    ? `runout in ~${hours} h`
    : `runout in ~${Math.round(days)} days`;
}

function AttentionRow({
  item,
  boardOn,
  today,
  timeZone,
  onOrderActioned,
  onApprovalDone,
  onReload,
  onCreateOrder,
}: {
  item: AttentionItem;
  boardOn: boolean;
  today: string;
  timeZone: string;
  onOrderActioned: (id: string) => void;
  onApprovalDone: (id: string) => void;
  onReload: () => void;
  onCreateOrder?: (prefill?: CreateOrderPrefill) => void;
}) {
  switch (item.kind) {
    case "exception": {
      const e = item.exception;
      const href = boardLink({
        date: today,
        truck: e.truckId,
        order: e.orderId,
      });
      return (
        <Row>
          <StatusBadge status="exception" />
          <span className="min-w-0 flex-1 truncate text-sm">
            <Link
              href={LINKS.order(e.orderId)}
              className="font-semibold text-slate-900 hover:underline"
            >
              Order {e.orderId}
            </Link>
            <span className="text-xs text-text-muted">
              {" "}
              · {e.customerId ?? "Customer"} · Truck {e.truckId}
              {e.productCode ? ` · ${productName(e.productCode)}` : ""}
            </span>
          </span>
          <Link href={href} className={rowBtn}>
            Open on board
          </Link>
        </Row>
      );
    }
    case "delayed": {
      const j = item.job;
      const href =
        boardOn && j.asset_assigned
          ? boardLink({
              date: today,
              truck: j.asset_assigned,
              order: j.order_id,
            })
          : LINKS.job(j.job_id);
      return (
        <Row>
          <StatusBadge
            status="delayed"
            label={delayText(j.delay_duration_minutes)}
          />
          <span className="min-w-0 flex-1 truncate text-sm">
            <Link
              href={LINKS.job(j.job_id)}
              className="font-semibold text-slate-900 hover:underline"
            >
              Job {j.job_id}
            </Link>
            <span className="text-xs text-text-muted">
              {" "}
              · {j.destination}
              {j.asset_assigned ? ` · Truck ${j.asset_assigned}` : ""}
              {j.estimated_arrival
                ? ` · ETA ${time(j.estimated_arrival, { timeZone })}`
                : ""}
            </span>
          </span>
          <Link href={href} className={rowBtn}>
            {boardOn && j.asset_assigned ? "Open on board" : "Open job"}
          </Link>
        </Row>
      );
    }
    case "approval":
      return <ApprovalRow approval={item.approval} onDone={onApprovalDone} />;
    case "order":
      return (
        <OrderAttentionRow
          order={item.order}
          boardHref={
            boardOn
              ? boardLink({
                  date:
                    item.order.delivery_window_start?.slice(0, 10) &&
                    item.order.delivery_window_start.slice(0, 10) >= today
                      ? item.order.delivery_window_start.slice(0, 10)
                      : today,
                  order: item.order.order_id,
                })
              : null
          }
          onActioned={onOrderActioned}
          onReload={onReload}
        />
      );
    case "tank": {
      const a = item.alert;
      const pct = Math.round(a.stock_percentage);
      const severe = a.status === "critical" || a.status === "empty";
      return (
        <Row>
          <ProductCap code={String(a.fuel_type ?? "")} />
          <StatusBadge
            status={severe ? "exception" : "delayed"}
            label={`${pct}% tank`}
          />
          <span className="min-w-0 flex-1 truncate text-sm">
            <Link
              href={LINKS.station(a.station_id)}
              className="font-semibold text-slate-900 hover:underline"
            >
              {a.name}
            </Link>
            <span className="text-xs text-text-muted">
              {a.location_name ? ` · ${a.location_name}` : ""}
              {runout(a.days_until_empty)
                ? ` · ${runout(a.days_until_empty)}`
                : ""}
              {typeof a.capacity_gallons === "number" &&
              typeof a.current_stock_gallons === "number"
                ? ` · ${gallons(a.capacity_gallons - a.current_stock_gallons)} to fill`
                : ""}
            </span>
          </span>
          {onCreateOrder ? (
            <button
              type="button"
              className={rowBtn}
              onClick={() => onCreateOrder(tankPrefill(a))}
            >
              Create order
            </button>
          ) : (
            <Link href={LINKS.station(a.station_id)} className={rowBtn}>
              Open station
            </Link>
          )}
        </Row>
      );
    }
  }
}

function ApprovalRow({
  approval,
  onDone,
}: {
  approval: ApprovalEntry;
  onDone: (id: string) => void;
}) {
  const [working, setWorking] = useState(false);
  const approve = async () => {
    setWorking(true);
    try {
      // Same call as the approvals inbox (ApprovalQueue).
      await approveAction(approval.action_id);
      notify({
        type: "success",
        message: `Approved: ${approvalTitle(approval)}`,
      });
      onDone(approval.action_id);
    } catch (err) {
      notify({
        type: "error",
        message:
          err instanceof ApiError
            ? err.message
            : "Couldn't approve. Try again.",
      });
    } finally {
      setWorking(false);
    }
  };
  const title = approvalTitle(approval);
  return (
    <Row>
      <StatusBadge status="planned" label="Approval" />
      <span className="min-w-0 flex-1 truncate text-sm" title={title}>
        <span className="font-semibold text-slate-900">{title}</span>
        <span className="text-xs text-text-muted">
          {" "}
          · {approval.proposed_by}
          {approval.proposed_at ? ` · ${relative(approval.proposed_at)}` : ""}
          {approval.risk_level !== "low"
            ? ` · ${approval.risk_level} risk`
            : ""}
        </span>
      </span>
      <Link
        href={LINKS.approvals(approval.action_id)}
        className={rowBtn}
        aria-label={`Review ${title}`}
      >
        Review
      </Link>
      <Button
        variant="primary"
        size="sm"
        loading={working}
        onClick={approve}
        aria-label={`Approve ${title}`}
      >
        Approve
      </Button>
    </Row>
  );
}

// ── Today's runs ───────────────────────────────────────────────────────────

function TodaysRuns({
  loading,
  boardOn,
  day,
  runs,
  failure,
  href,
  onRetry,
}: {
  loading: boolean;
  boardOn: boolean;
  day: Day;
  runs: Run[] | null;
  failure: LoadFailure | null;
  href: string;
  onRetry: () => void;
}) {
  const title = day === "today" ? "Today's runs" : "Tomorrow's runs";
  return (
    <Panel
      id="dash-runs"
      title={title}
      accent={STATUS.dispatched.dot}
      link={{ href, label: boardOn ? "Open Dispatch Board" : "Open jobs" }}
    >
      {loading ? (
        <Skeleton
          rows={4}
          height={30}
          label={`Loading ${title.toLowerCase()}`}
          className="p-3"
        />
      ) : failure ? (
        <WidgetError failure={failure} label={title} onRetry={onRetry} />
      ) : !runs || runs.length === 0 ? (
        <p className="px-3 py-6 text-center text-sm text-text-muted">
          No trucks have work {day === "today" ? "today" : "tomorrow"} yet.{" "}
          <Link href={href} className="font-semibold text-link underline">
            Plan on the board
          </Link>
        </p>
      ) : (
        <>
          <ul aria-label={title}>
            {runs.slice(0, RUNS_LIMIT).map((r) => (
              <RunRow key={r.truckId} run={r} />
            ))}
          </ul>
          {runs.length > RUNS_LIMIT && (
            <div className="flex h-9 items-center border-t border-slate-100 px-3 text-xs text-text-muted">
              Showing {RUNS_LIMIT} of {formatNumber(runs.length)} trucks
              <Link
                href={href}
                className="ml-auto font-semibold text-link hover:underline"
              >
                All on the board
              </Link>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}

function RunRow({ run }: { run: Run }) {
  const stripe = identityFor(run.truckId).hex;
  const summary = run.segments
    .map((s) => `${s.count} ${STATUS[s.status].label.toLowerCase()}`)
    .join(", ");
  return (
    <li className="border-b border-slate-100 last:border-b-0">
      <Link
        href={run.href}
        className="grid h-[46px] grid-cols-[6px_minmax(0,9.5rem)_1fr_auto] items-center gap-2.5 px-3 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus"
        aria-label={`Truck ${run.truckId}${run.driverName ? `, ${run.driverName}` : ", no driver"}: ${run.label}, ${run.done} of ${run.total} stops delivered (${summary}). Open on the board`}
      >
        <span
          aria-hidden="true"
          className="h-[30px] w-1.5 rounded"
          style={{ backgroundColor: stripe }}
        />
        <span className="truncate text-sm" aria-hidden="true">
          <b className="font-bold text-slate-900">{run.truckId}</b>
          <span className="text-text-muted">
            {" "}
            · {run.driverName ?? "unassigned"}
          </span>
        </span>
        <span
          aria-hidden="true"
          className="flex h-2.5 overflow-hidden rounded-full bg-slate-100"
        >
          {run.segments.map((s) => (
            <i
              key={s.status}
              className="block h-full"
              style={{
                width: `${(s.count / Math.max(1, run.total)) * 100}%`,
                backgroundColor: STATUS[s.status].dot,
              }}
            />
          ))}
        </span>
        <span aria-hidden="true">
          <StatusBadge
            status={run.status}
            label={`${run.label} ${run.done}/${run.total}`}
          />
        </span>
      </Link>
    </li>
  );
}

// ── Plan status ────────────────────────────────────────────────────────────

function PlanStatus({
  loading,
  lines,
  failure,
  href,
  onRetry,
}: {
  loading: boolean;
  lines: PlanLine[];
  failure: LoadFailure | null;
  href: string;
  onRetry: () => void;
}) {
  return (
    <Panel
      id="dash-plans"
      title="Plan status"
      accent={STATUS.planned.dot}
      link={{ href, label: "Open plans" }}
    >
      {loading ? (
        <Skeleton
          rows={2}
          height={28}
          label="Loading plan status"
          className="p-3"
        />
      ) : failure && lines.length === 0 ? (
        <WidgetError failure={failure} label="Plan status" onRetry={onRetry} />
      ) : lines.length === 0 ? (
        <p className="px-3 py-6 text-center text-sm text-text-muted">
          No plans for today or tomorrow yet.{" "}
          <Link href={href} className="font-semibold text-link underline">
            Generate a plan
          </Link>
        </p>
      ) : (
        <ul aria-label="Plans">
          {lines.map((l) => (
            <li
              key={l.key}
              className="flex min-h-11 items-center gap-2.5 border-b border-slate-100 px-3 last:border-b-0"
            >
              <StatusBadge status={l.status} label={l.label} />
              <span className="min-w-0 flex-1 truncate text-sm text-slate-800">
                {l.day} · {formatNumber(l.loads)}{" "}
                {l.loads === 1 ? "load" : "loads"} on {formatNumber(l.trucks)}{" "}
                {l.trucks === 1 ? "truck" : "trucks"}
                {l.warnings > 0
                  ? ` · ${l.warnings} ${l.warnings === 1 ? "warning" : "warnings"}`
                  : ""}
              </span>
              {l.action && l.href && (
                <Link href={l.href} className={rowBtn}>
                  {l.action}
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
