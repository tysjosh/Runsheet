"use client";

/**
 * Dispatch → Jobs (UI revamp R8.6, §7.2; was the Scheduling job board).
 *
 * No header of its own: "Create job" goes to the Dispatch title row through
 * `usePageChrome()`. One toolbar row: search, status chips with counts (they
 * replace the KPI summary bar), a "Filters · n" popover (type, truck, dates),
 * Export CSV, and Cargo search in the overflow (it opens in a drawer). The
 * list is the shared `DataTable` with a per-row action menu and no row
 * tinting. `?status=` (e.g. `delayed` from the Dashboard) picks the chip.
 *
 * A row opens the job at `/dashboard/dispatch/jobs/:id` (R8.6).
 *
 * Live: `job_created`, `status_changed` and `delay_alert` patch rows in place.
 */
import { PackageSearch, Plus } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useSchedulingWebSocket } from "../../hooks/useSchedulingWebSocket";
import { classifyLoadError } from "../../services/apiErrors";
import {
  type JobFilters as ApiJobFilters,
  getJobs,
  transitionStatus,
} from "../../services/schedulingApi";
import type { Job, JobStatus } from "../../types/api";
import CreateJobModal from "../ops/CreateJobModal";
import JobBoard from "../ops/JobBoard";
import JobFilters, { type JobFilterValues } from "../ops/JobFilters";
import {
  Button,
  Drawer,
  FilterChips,
  FilterPopover,
  Toolbar,
  usePageChrome,
} from "../ui";
import { ExportCsvButton } from "../ui/ExportCsvButton";
import { NETWORK_COPY } from "../ui/LoadErrorState";
import { notify } from "../ui/toast/notify";

const CargoSearchSection = lazy(() => import("../ops/CargoSearchSection"));

const INITIAL_FILTERS: JobFilterValues = {
  job_type: "",
  status: "",
  start_date: "",
  end_date: "",
  asset_assigned: "",
};

/** The list's filter mapping, shared by the job fetch and the CSV export. */
export function toApiJobFilters(filters: JobFilterValues): ApiJobFilters {
  const apiFilters: ApiJobFilters = {};
  if (filters.job_type) apiFilters.job_type = filters.job_type;
  if (filters.status) apiFilters.status = filters.status;
  if (filters.asset_assigned)
    apiFilters.asset_assigned = filters.asset_assigned;
  if (filters.start_date) apiFilters.start_date = filters.start_date;
  if (filters.end_date) apiFilters.end_date = filters.end_date;
  return apiFilters;
}

export type JobChip =
  | "all"
  | "scheduled"
  | "assigned"
  | "in_progress"
  | "delayed"
  | "completed"
  | "failed"
  | "cancelled";

const CHIPS: {
  id: JobChip;
  label: string;
  status?: import("../../styles/tokens").StatusKey;
}[] = [
  { id: "all", label: "All" },
  { id: "scheduled", label: "Scheduled", status: "planned" },
  { id: "assigned", label: "Assigned", status: "dispatched" },
  { id: "in_progress", label: "In progress", status: "in_transit" },
  { id: "delayed", label: "Delayed", status: "delayed" },
  { id: "completed", label: "Completed", status: "delivered" },
  { id: "failed", label: "Failed", status: "exception" },
  { id: "cancelled", label: "Cancelled", status: "cancelled" },
];

export function matchesChip(job: Job, chip: JobChip): boolean {
  if (chip === "all") return true;
  if (chip === "delayed") return job.delayed;
  return job.status === chip;
}

function matchesSearch(job: Job, q: string): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  return [
    job.job_id,
    job.origin,
    job.destination,
    job.asset_assigned,
    job.order_id,
  ]
    .filter(Boolean)
    .some((v) => String(v).toLowerCase().includes(needle));
}

export default function JobsView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const initialChip = (searchParams?.get("status") ?? "all") as JobChip;
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<JobFilterValues>(INITIAL_FILTERS);
  const [chip, setChip] = useState<JobChip>(
    CHIPS.some((c) => c.id === initialChip) ? initialChip : "all",
  );
  const [query, setQuery] = useState("");
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showCargoSearch, setShowCargoSearch] = useState(false);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await getJobs(toApiJobFilters(filters));
      setJobs(Array.isArray(res.data) ? res.data : []);
    } catch (err) {
      const f = classifyLoadError(err, "Jobs couldn't be loaded.");
      setError(f.kind === "network" ? NETWORK_COPY : f.message);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const handleJobCreated = useCallback((event: { job: Job }) => {
    setJobs((prev) => [event.job, ...prev]);
  }, []);
  const handleStatusChanged = useCallback(
    (event: {
      job_id: string;
      new_status: string;
      old_status: string;
      asset_assigned?: string;
      estimated_arrival?: string;
    }) => {
      setJobs((prev) =>
        prev.map((j) =>
          j.job_id === event.job_id
            ? {
                ...j,
                status: event.new_status as JobStatus,
                asset_assigned: event.asset_assigned ?? j.asset_assigned,
                estimated_arrival:
                  event.estimated_arrival ?? j.estimated_arrival,
                updated_at: new Date().toISOString(),
              }
            : j,
        ),
      );
    },
    [],
  );
  const handleDelayAlert = useCallback(
    (event: { job_id: string; delay_duration_minutes: number }) => {
      setJobs((prev) =>
        prev.map((j) =>
          j.job_id === event.job_id
            ? {
                ...j,
                delayed: true,
                delay_duration_minutes: event.delay_duration_minutes,
              }
            : j,
        ),
      );
    },
    [],
  );
  useSchedulingWebSocket({
    subscriptions: ["job_created", "status_changed", "delay_alert"],
    onJobCreated: handleJobCreated,
    onStatusChanged: handleStatusChanged,
    onDelayAlert: handleDelayAlert,
  });

  /** Row menu and Fail dialog: the API call, then the row; errors toast. */
  const handleTransition = useCallback(
    async (jobId: string, targetStatus: JobStatus, failureReason?: string) => {
      try {
        const res = await transitionStatus(jobId, {
          status: targetStatus,
          failure_reason: failureReason,
        });
        setJobs((prev) => prev.map((j) => (j.job_id === jobId ? res.data : j)));
      } catch (err) {
        notify({
          type: "error",
          message:
            err instanceof Error && err.message
              ? `Job ${jobId}: ${err.message}`
              : `Job ${jobId} couldn't be updated.`,
        });
        throw err;
      }
    },
    [],
  );

  const counts = useMemo(() => {
    const out: Record<JobChip, number> = {
      all: 0,
      scheduled: 0,
      assigned: 0,
      in_progress: 0,
      delayed: 0,
      completed: 0,
      failed: 0,
      cancelled: 0,
    };
    for (const j of jobs) {
      if (!matchesSearch(j, query)) continue;
      for (const c of CHIPS) if (matchesChip(j, c.id)) out[c.id] += 1;
    }
    return out;
  }, [jobs, query]);
  const visible = useMemo(
    () => jobs.filter((j) => matchesChip(j, chip) && matchesSearch(j, query)),
    [jobs, chip, query],
  );
  const secondary =
    (filters.job_type ? 1 : 0) +
    (filters.asset_assigned ? 1 : 0) +
    (filters.start_date || filters.end_date ? 1 : 0);

  usePageChrome({
    actions: (
      <Button
        variant="primary"
        size="sm"
        icon={<Plus className="h-3.5 w-3.5" />}
        onClick={() => setShowCreateModal(true)}
      >
        Create job
      </Button>
    ),
  });

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface">
      <Toolbar
        label="Jobs"
        search={
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search jobs"
            placeholder="Search jobs, places, trucks"
            className="h-7 w-full rounded-lg border border-slate-300 bg-surface px-2.5 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          />
        }
        filters={
          <>
            <FilterChips
              label="Job status"
              options={CHIPS.map((c) => ({ ...c, count: counts[c.id] }))}
              value={chip}
              onChange={(v) => setChip(v as JobChip)}
              className="overflow-x-auto"
            />
            <FilterPopover
              count={secondary}
              label="Job filters"
              onClear={() => setFilters(INITIAL_FILTERS)}
            >
              <JobFilters filters={filters} onChange={setFilters} hideStatus />
            </FilterPopover>
          </>
        }
        end={
          <ExportCsvButton
            type="jobs"
            params={{ ...toApiJobFilters(filters) }}
            subject="jobs"
            allowedRoles={["admin", "dispatcher"]}
          />
        }
        overflow={[
          {
            id: "cargo",
            label: "Cargo search",
            icon: <PackageSearch />,
            onSelect: () => setShowCargoSearch(true),
            inline: false,
          },
        ]}
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <JobBoard
          jobs={visible}
          loading={loading && jobs.length === 0}
          error={
            error ? { message: error, onRetry: () => void loadData() } : null
          }
          onTransition={handleTransition}
          onSelectJob={(id) =>
            router.push(`/dashboard/dispatch/jobs/${encodeURIComponent(id)}`)
          }
        />
      </div>
      <Drawer
        open={showCargoSearch}
        onClose={() => setShowCargoSearch(false)}
        title="Cargo search"
        width={640}
      >
        <Suspense
          fallback={
            <p className="py-8 text-center text-sm text-text-muted">
              Loading cargo search…
            </p>
          }
        >
          {showCargoSearch && <CargoSearchSection />}
        </Suspense>
      </Drawer>
      {showCreateModal && (
        <CreateJobModal
          onClose={() => setShowCreateModal(false)}
          onCreated={(job) => setJobs((prev) => [job, ...prev])}
        />
      )}
    </div>
  );
}
