"use client";

/**
 * Live (`/dashboard/control`, UI revamp R9, §7.3): the shift's monitoring
 * surface.
 *
 * Title row: "Live", the tabs Overview · Approvals · Agents (`?tab=`), inline
 * counts and the agent autonomy chip (links to Settings → Agents). Static
 * warnings take no space unless they need action (low inventory becomes a
 * count chip; the default-depot warning only shows when no depot is set).
 *
 * - Overview: the map of all trucks (identity colours, status badges) on the
 *   left; exceptions and delays, then active jobs, on the right.
 * - Approvals: the single approvals inbox (`ApprovalQueue`), `?id=` focuses
 *   one item (the Dashboard's Review link).
 * - Agents: activity feed and agent health.
 *
 * Sources load with `Promise.allSettled`; each panel shows its own error and
 * the view keeps the last good data on a failed background refresh. Live
 * updates arrive on the scheduling socket with a 60 s fallback poll.
 */
import { Boxes, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  DelayAlertEvent,
  JobCreatedEvent,
  StatusChangedEvent,
} from "../../hooks/useSchedulingWebSocket";
import { useSchedulingWebSocket } from "../../hooks/useSchedulingWebSocket";
import { number as formatNumber, relative, time } from "../../lib/format";
import { apiService } from "../../services/api";
import { classifyLoadError, type LoadFailure } from "../../services/apiErrors";
import {
  type FuelAlert,
  getAlerts as getFuelAlerts,
} from "../../services/fuelApi";
import { getAlerts as getInventoryAlerts } from "../../services/inventoryApi";
import { getActiveJobs, getDelayedJobs } from "../../services/schedulingApi";
import { STATUS } from "../../styles/tokens";
import type { Job, JobStatus } from "../../types/api";
import AgentActivityFeed from "../ops/AgentActivityFeed";
import AgentHealth from "../ops/AgentHealth";
import ApprovalQueue from "../ops/ApprovalQueue";
import DefaultDepotWarning from "../ops/DefaultDepotWarning";
import { JobStatusBadge } from "../ops/JobBoard";
import OperationsMap, { type AssetLocation } from "../ops/OperationsMap";
import StormModeBanner from "../ops/StormModeBanner";
import {
  type Column,
  DataTable,
  IconButton,
  IdentityAvatar,
  LoadErrorState,
  PageHeader,
  ProductCap,
  StatusBadge,
  TabPanel,
  useUrlTab,
} from "../ui";
import { NETWORK_COPY } from "../ui/LoadErrorState";
import { AutonomyChip } from "./AutonomyChip";

/** Fallback poll: a monitoring surface can't silently go stale. */
export const LIVE_REFRESH_MS = 60_000;

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "approvals", label: "Approvals" },
  { id: "agents", label: "Agents" },
];

interface LiveData {
  active: Job[];
  delayed: Job[];
  fuel: FuelAlert[];
  assets: AssetLocation[];
  inventoryAlerts: number;
  failures: { jobs: LoadFailure | null; assets: LoadFailure | null };
}

const listOf = <T,>(v: unknown): T[] => {
  const o = v as { data?: unknown } | null;
  return Array.isArray(o?.data) ? (o?.data as T[]) : [];
};

/** Assets with a usable position, joined to their active job. */
export function assetLocations(
  assets: unknown[],
  active: Job[],
  delayed: Job[],
): AssetLocation[] {
  const jobsByAsset = new Map<string, Job>();
  for (const job of active)
    if (job.asset_assigned) jobsByAsset.set(job.asset_assigned, job);
  const late = new Set(delayed.map((j) => j.asset_assigned).filter(Boolean));
  return (assets as Record<string, any>[])
    .filter((a) => {
      const lat = a.currentLocation?.coordinates?.lat;
      const lng = a.currentLocation?.coordinates?.lon;
      return (
        typeof lat === "number" &&
        typeof lng === "number" &&
        !Number.isNaN(lat) &&
        !Number.isNaN(lng)
      );
    })
    .map((a) => {
      const job = jobsByAsset.get(a.id);
      return {
        asset_id: a.id,
        name:
          a.name || a.plateNumber || a.vesselName || a.containerNumber || a.id,
        lat: a.currentLocation.coordinates.lat,
        lng: a.currentLocation.coordinates.lon,
        job_status: job?.status,
        job_id: job?.job_id,
        delayed: late.has(a.id),
      };
    });
}

export default function LiveView() {
  const [tab, setTab] = useUrlTab(
    TABS.map((t) => t.id),
    { fallback: "overview" },
  );
  const focusId = useSearchParams()?.get("id") ?? undefined;
  const [data, setData] = useState<LiveData | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const dataRef = useRef(data);
  dataRef.current = data;

  const loadData = useCallback(async (opts?: { background?: boolean }) => {
    if (!opts?.background) setRefreshing(true);
    const [activeR, delayedR, fuelR, assetsR, invR] = await Promise.allSettled([
      getActiveJobs(),
      getDelayedJobs(),
      getFuelAlerts(),
      apiService.getAssets(),
      getInventoryAlerts(),
    ]);
    const prev = dataRef.current;
    const active =
      activeR.status === "fulfilled"
        ? listOf<Job>(activeR.value)
        : (prev?.active ?? []);
    const delayed =
      delayedR.status === "fulfilled"
        ? listOf<Job>(delayedR.value)
        : (prev?.delayed ?? []);
    const jobsFailure =
      activeR.status === "rejected"
        ? classifyLoadError(activeR.reason, "Active jobs couldn't be loaded.")
        : delayedR.status === "rejected"
          ? classifyLoadError(
              delayedR.reason,
              "Delayed jobs couldn't be loaded.",
            )
          : null;
    const assets =
      assetsR.status === "fulfilled"
        ? assetLocations(listOf(assetsR.value), active, delayed)
        : (prev?.assets ?? []);
    setData({
      active,
      delayed,
      fuel:
        fuelR.status === "fulfilled"
          ? listOf<FuelAlert>(fuelR.value)
          : (prev?.fuel ?? []),
      assets,
      inventoryAlerts:
        invR.status === "fulfilled"
          ? (invR.value.count ?? invR.value.data?.length ?? 0)
          : (prev?.inventoryAlerts ?? 0),
      failures: {
        jobs: jobsFailure,
        assets:
          assetsR.status === "rejected"
            ? classifyLoadError(
                assetsR.reason,
                "Truck positions couldn't be loaded.",
              )
            : null,
      },
    });
    setLastUpdated(Date.now());
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void loadData();
    const id = setInterval(
      () => void loadData({ background: true }),
      LIVE_REFRESH_MS,
    );
    return () => clearInterval(id);
  }, [loadData]);

  // ── Live updates (scheduling socket) ────────────────────────────────────
  const patch = (fn: (d: LiveData) => LiveData) =>
    setData((d) => (d ? fn(d) : d));
  const onJobCreated = useCallback((e: JobCreatedEvent) => {
    patch((d) => ({ ...d, active: [e.job, ...d.active] }));
  }, []);
  const onStatusChanged = useCallback((e: StatusChangedEvent) => {
    patch((d) => ({
      ...d,
      active: d.active.map((j) =>
        j.job_id === e.job_id
          ? {
              ...j,
              status: e.new_status as JobStatus,
              asset_assigned: e.asset_assigned ?? j.asset_assigned,
              estimated_arrival: e.estimated_arrival ?? j.estimated_arrival,
            }
          : j,
      ),
    }));
  }, []);
  const onDelayAlert = useCallback((e: DelayAlertEvent) => {
    patch((d) => {
      const known = d.delayed.some((j) => j.job_id === e.job_id);
      const fromActive = d.active.find((j) => j.job_id === e.job_id);
      const entry: Job = fromActive
        ? {
            ...fromActive,
            delayed: true,
            delay_duration_minutes: e.delay_duration_minutes,
          }
        : {
            job_id: e.job_id,
            job_type: e.job_type as Job["job_type"],
            status: "in_progress",
            tenant_id: "",
            origin: e.origin,
            destination: e.destination,
            scheduled_time: "",
            created_at: "",
            updated_at: new Date().toISOString(),
            priority: "normal",
            delayed: true,
            delay_duration_minutes: e.delay_duration_minutes,
            asset_assigned: e.asset_assigned,
          };
      return {
        ...d,
        active: d.active.map((j) => (j.job_id === e.job_id ? entry : j)),
        delayed: known
          ? d.delayed.map((j) =>
              j.job_id === e.job_id
                ? { ...j, delay_duration_minutes: e.delay_duration_minutes }
                : j,
            )
          : [...d.delayed, entry],
      };
    });
  }, []);
  useSchedulingWebSocket({
    subscriptions: [
      "job_created",
      "status_changed",
      "delay_alert",
      "cargo_update",
    ],
    onJobCreated,
    onStatusChanged,
    onDelayAlert,
  });

  const exceptions = useMemo(
    () => (data?.active ?? []).filter((j) => j.status === "failed"),
    [data],
  );
  const lowTanks = useMemo(
    () =>
      (data?.fuel ?? []).filter(
        (a) => a.status === "critical" || a.status === "empty",
      ),
    [data],
  );
  const trucksOut = useMemo(
    () =>
      new Set(
        (data?.active ?? [])
          .filter((j) => j.status === "in_progress" && j.asset_assigned)
          .map((j) => j.asset_assigned),
      ).size,
    [data],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Live"
        help="The shift as it happens: where every truck is, what is late or failed, the approvals inbox and what the agents are doing."
        tabs={TABS}
        tab={tab}
        onTabChange={setTab}
        tabIdBase="live"
        counts={
          data && (
            <ul aria-label="Live counts" className="flex items-center gap-3">
              <li>
                <b className="text-sm text-slate-900 tabular-nums">
                  {formatNumber(trucksOut)}
                </b>{" "}
                {trucksOut === 1 ? "truck out" : "trucks out"}
              </li>
              <li>
                <b
                  className={`text-sm tabular-nums ${data.delayed.length ? "text-amber-800" : "text-slate-900"}`}
                >
                  {formatNumber(data.delayed.length)}
                </b>{" "}
                delayed
              </li>
              <li>
                <b
                  className={`text-sm tabular-nums ${exceptions.length ? "text-red-800" : "text-slate-900"}`}
                >
                  {formatNumber(exceptions.length)}
                </b>{" "}
                {exceptions.length === 1 ? "exception" : "exceptions"}
              </li>
              <li>
                <DefaultDepotWarning variant="chip" />
              </li>
              {data.inventoryAlerts > 0 && (
                <li>
                  <Link
                    href="/dashboard/fleet?tab=inventory"
                    className="inline-flex h-6 items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2 font-semibold text-amber-800 hover:bg-amber-100"
                  >
                    <Boxes aria-hidden="true" className="h-3.5 w-3.5" />
                    {data.inventoryAlerts} inventory{" "}
                    {data.inventoryAlerts === 1 ? "alert" : "alerts"}
                  </Link>
                </li>
              )}
            </ul>
          )
        }
        actions={
          <>
            <AutonomyChip />
            {lastUpdated && (
              <span className="hidden text-xs text-text-muted xl:inline">
                Updated {relative(lastUpdated)}
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
      <StormModeBanner />
      <TabPanel
        idBase="live"
        value={tab}
        className="min-h-0 flex-1 overflow-auto bg-canvas"
      >
        {tab === "overview" && (
          <Overview
            data={data}
            exceptions={exceptions}
            lowTanks={lowTanks}
            onRetry={() => void loadData()}
          />
        )}
        {tab === "approvals" && (
          <div className="h-full p-3">
            <ApprovalQueue focusId={focusId} />
          </div>
        )}
        {tab === "agents" && (
          <div className="grid gap-3 p-3 lg:grid-cols-2">
            <div className="h-[28rem]">
              <AgentActivityFeed />
            </div>
            <div className="h-[28rem]">
              <AgentHealth />
            </div>
          </div>
        )}
      </TabPanel>
    </div>
  );
}

function Panel({
  id,
  title,
  accent,
  count,
  children,
}: {
  id: string;
  title: string;
  accent: string;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <section
      aria-labelledby={id}
      className="overflow-hidden rounded-[10px] border border-slate-200 bg-surface"
    >
      <div className="flex h-10 items-center gap-2 border-b border-slate-200 px-3">
        <span
          aria-hidden="true"
          className="h-4 w-1 rounded-sm"
          style={{ backgroundColor: accent }}
        />
        <h2 id={id} className="text-[13px] font-semibold text-slate-900">
          {title}
        </h2>
        {count !== undefined && (
          <span className="text-xs text-text-muted">{formatNumber(count)}</span>
        )}
      </div>
      {children}
    </section>
  );
}

const rowBtn =
  "inline-flex h-7 shrink-0 items-center rounded-lg border border-slate-300 bg-surface px-2.5 text-xs font-semibold text-slate-800 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus";

function Overview({
  data,
  exceptions,
  lowTanks,
  onRetry,
}: {
  data: LiveData | null;
  exceptions: Job[];
  lowTanks: FuelAlert[];
  onRetry: () => void;
}) {
  if (!data) {
    return (
      <div role="status" className="p-6 text-sm text-text-muted">
        Loading the shift…
      </div>
    );
  }
  const issues = [
    ...exceptions.map((j) => ({ kind: "exception" as const, job: j })),
    ...[...data.delayed]
      .sort(
        (a, b) =>
          (b.delay_duration_minutes ?? 0) - (a.delay_duration_minutes ?? 0),
      )
      .map((j) => ({ kind: "delayed" as const, job: j })),
  ];
  const columns: Column<Job>[] = [
    {
      key: "job_id",
      header: "Job",
      width: 130,
      className: "whitespace-nowrap",
      cell: (j) => (
        <Link
          href={`/dashboard/dispatch/jobs/${encodeURIComponent(j.job_id)}`}
          className="inline-flex min-h-6 items-center font-semibold text-link hover:underline"
        >
          {j.job_id}
        </Link>
      ),
    },
    {
      key: "truck",
      header: "Truck",
      width: 140,
      className: "whitespace-nowrap",
      cell: (j) =>
        j.asset_assigned ? (
          <span className="inline-flex min-w-0 items-center gap-1.5">
            <IdentityAvatar
              id={j.asset_assigned}
              label={j.asset_assigned}
              size="xs"
            />
            <span className="truncate">{j.asset_assigned}</span>
          </span>
        ) : (
          <span className="text-text-muted">Unassigned</span>
        ),
    },
    {
      key: "status",
      header: "Status",
      width: 160,
      cell: (j) => <JobStatusBadge job={j} />,
    },
    {
      key: "destination",
      header: "Destination",
      truncate: true,
      cell: (j) => j.destination,
    },
    {
      key: "eta",
      header: "ETA",
      width: 70,
      className: "tabular-nums",
      cell: (j) => (j.estimated_arrival ? time(j.estimated_arrival) : "—"),
    },
  ];
  return (
    <div className="grid h-full min-h-0 gap-3 p-3 lg:grid-cols-[3fr_2fr]">
      <OperationsMap
        assets={data.assets}
        className="min-h-[420px] lg:min-h-0"
      />
      <div className="flex min-h-0 flex-col gap-3 lg:overflow-y-auto">
        <Panel
          id="live-issues"
          title="Exceptions and delays"
          accent={STATUS.exception.dot}
          count={issues.length + lowTanks.length}
        >
          {data.failures.jobs && issues.length === 0 ? (
            <div className="p-3">
              <LoadErrorState
                failure={data.failures.jobs}
                entityLabel="Jobs"
                onRetry={onRetry}
                embedded
              />
            </div>
          ) : issues.length + lowTanks.length === 0 ? (
            <p className="px-3 py-6 text-center text-sm text-text-muted">
              No exceptions or delays right now.
            </p>
          ) : (
            <ul aria-label="Exceptions and delays">
              {issues.map(({ kind, job }) => (
                <li
                  key={`${kind}-${job.job_id}`}
                  data-feed-row
                  className="flex min-h-11 items-center gap-2.5 border-b border-slate-100 px-3 last:border-b-0"
                >
                  {kind === "exception" ? (
                    <StatusBadge status="exception" label="Failed" />
                  ) : (
                    <StatusBadge
                      status="delayed"
                      label={
                        job.delay_duration_minutes
                          ? `+${job.delay_duration_minutes} min`
                          : "Delayed"
                      }
                    />
                  )}
                  <span className="min-w-0 flex-1 truncate text-sm">
                    <span className="font-semibold text-slate-900">
                      Job {job.job_id}
                    </span>
                    <span className="text-xs text-text-muted">
                      {" "}
                      · {job.destination}
                      {job.asset_assigned
                        ? ` · Truck ${job.asset_assigned}`
                        : ""}
                      {kind === "exception" && job.failure_reason
                        ? ` · ${job.failure_reason}`
                        : ""}
                    </span>
                  </span>
                  <Link
                    href={`/dashboard/dispatch/jobs/${encodeURIComponent(job.job_id)}`}
                    className={rowBtn}
                  >
                    Open job
                  </Link>
                </li>
              ))}
              {lowTanks.map((a) => (
                <li
                  key={`tank-${a.station_id}`}
                  data-feed-row
                  className="flex min-h-11 items-center gap-2.5 border-b border-slate-100 px-3 last:border-b-0"
                >
                  <ProductCap code={String(a.fuel_type ?? "")} />
                  <StatusBadge
                    status="exception"
                    label={`${Math.round(a.stock_percentage)}% tank`}
                  />
                  <span className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-900">
                    {a.name}
                  </span>
                  <Link
                    href={`/dashboard/fuel-ops?tab=stations&station=${encodeURIComponent(a.station_id)}`}
                    className={rowBtn}
                  >
                    Open station
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel
          id="live-jobs"
          title="Active jobs"
          accent={STATUS.in_transit.dot}
          count={data.active.length}
        >
          <DataTable<Job>
            ariaLabel="Active jobs"
            rowHeight="compact"
            columns={columns}
            data={data.active}
            getRowId={(j) => j.job_id}
            error={
              data.failures.jobs && data.active.length === 0
                ? {
                    message:
                      data.failures.jobs.kind === "network"
                        ? NETWORK_COPY
                        : data.failures.jobs.message,
                    onRetry,
                  }
                : null
            }
            emptyState={
              <p className="text-sm text-text-muted">No active jobs.</p>
            }
          />
        </Panel>
      </div>
    </div>
  );
}
