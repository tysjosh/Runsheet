"use client";

/**
 * Job list for Dispatch → Jobs (UI revamp R8.6, §7.2).
 *
 * The shared `DataTable`: single-line sticky 36 px header, 40 px rows, status
 * as a `StatusBadge` (hue + icon + label) with no full-row tinting, dates
 * through `lib/format`, and the status transitions (Assign, Start, Complete,
 * Fail, Cancel, from `JobActionButtons`' state machine) in a per-row `⋯`
 * menu. "Fail" asks for a reason in a small `FormDialog`. Sorting keeps the
 * board's field/order logic.
 */
import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { useCallback, useState } from "react";
import {
  type Column,
  DataTable,
  Field,
  FormDialog,
  INPUT_CLASS,
  type MenuItem,
  StatusBadge,
  statusKeyFor,
  type TableSort,
} from "@/components/ui";
import { dateTime } from "../../lib/format";
import type { Job, JobStatus } from "../../types/api";
import { TRANSITION_BUTTONS, VALID_TRANSITIONS } from "./JobActionButtons";

type SortField =
  | "job_id"
  | "job_type"
  | "status"
  | "origin"
  | "destination"
  | "asset_assigned"
  | "scheduled_time"
  | "estimated_arrival";

type SortOrder = "asc" | "desc";

interface JobBoardProps {
  jobs: Job[];
  onTransition: (
    jobId: string,
    targetStatus: JobStatus,
    failureReason?: string,
  ) => Promise<void>;
  /** Optional callback when a job row is clicked — navigates to job detail */
  onSelectJob?: (jobId: string) => void;
  loading?: boolean;
  error?: { message: string; onRetry?: () => void } | null;
}

const STATUS_LABEL: Record<string, string> = {
  scheduled: "Scheduled",
  assigned: "Assigned",
  in_progress: "In progress",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

/** Status badge for a job: Delayed wins over the job's own status. */
export function JobStatusBadge({
  job,
}: {
  job: Pick<Job, "status" | "delayed" | "delay_duration_minutes">;
}) {
  if (job.delayed) {
    return (
      <StatusBadge
        status="delayed"
        label={
          job.delay_duration_minutes
            ? `Delayed +${job.delay_duration_minutes} min`
            : "Delayed"
        }
      />
    );
  }
  const key =
    job.status === "scheduled"
      ? "planned"
      : (statusKeyFor(job.status) ?? "draft");
  return (
    <StatusBadge
      status={key}
      label={STATUS_LABEL[job.status] ?? job.status.replace(/_/g, " ")}
    />
  );
}

function formatJobType(jobType: string): string {
  const s = jobType.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function compareValues(
  a: string | undefined,
  b: string | undefined,
  order: SortOrder,
): number {
  const aVal = a ?? "";
  const bVal = b ?? "";
  const cmp = aVal.localeCompare(bVal);
  return order === "asc" ? cmp : -cmp;
}

export default function JobBoard({
  jobs,
  onTransition,
  onSelectJob,
  loading = false,
  error = null,
}: JobBoardProps) {
  const [sortField, setSortField] = useState<SortField>("scheduled_time");
  const [sortOrder, setSortOrder] = useState<SortOrder>("asc");
  const [failing, setFailing] = useState<string | null>(null);

  const handleSort = useCallback((s: TableSort) => {
    setSortField(s.key as SortField);
    setSortOrder(s.direction);
  }, []);

  const sorted = [...jobs].sort((a, b) => {
    const aVal = a[sortField] as string | undefined;
    const bVal = b[sortField] as string | undefined;
    return compareValues(aVal, bVal, sortOrder);
  });

  const columns: Column<Job>[] = [
    {
      key: "job_id",
      header: "Job",
      sortable: true,
      width: 150,
      truncate: true,
      title: (job) => job.job_id,
      className: "font-semibold text-slate-900",
      cell: (job) =>
        job.job_type === "cargo_transport" ? (
          <Link
            href={`/dashboard/dispatch/jobs/${encodeURIComponent(job.job_id)}/cargo`}
            className="inline-flex items-center gap-1 text-link hover:underline"
            onClick={(e) => e.stopPropagation()}
            aria-label={`${job.job_id} cargo manifest`}
          >
            {job.job_id}
            <ExternalLink aria-hidden="true" className="h-3 w-3" />
          </Link>
        ) : (
          job.job_id
        ),
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      width: 170,
      cell: (job) => <JobStatusBadge job={job} />,
    },
    {
      key: "job_type",
      header: "Type",
      sortable: true,
      truncate: true,
      className: "text-slate-700",
      cell: (job) => formatJobType(job.job_type),
    },
    {
      key: "origin",
      header: "Origin",
      sortable: true,
      truncate: true,
      className: "text-slate-700",
      cell: (job) => job.origin,
    },
    {
      key: "destination",
      header: "Destination",
      sortable: true,
      truncate: true,
      className: "text-slate-700",
      cell: (job) => job.destination,
    },
    {
      key: "asset_assigned",
      header: "Truck",
      sortable: true,
      width: 110,
      truncate: true,
      className: "text-slate-700",
      cell: (job) => job.asset_assigned ?? "—",
    },
    {
      key: "scheduled_time",
      header: "Scheduled",
      sortable: true,
      width: 140,
      className: "whitespace-nowrap text-slate-700 tabular-nums",
      cell: (job) => dateTime(job.scheduled_time),
    },
    {
      key: "estimated_arrival",
      header: "ETA",
      sortable: true,
      width: 140,
      className: "whitespace-nowrap text-slate-700 tabular-nums",
      cell: (job) => dateTime(job.estimated_arrival),
    },
  ];

  const rowMenu = (job: Job): MenuItem[] => {
    const targets = VALID_TRANSITIONS[job.status] ?? [];
    const items: MenuItem[] = [];
    if (onSelectJob)
      items.push({
        id: "open",
        label: "Open job",
        onSelect: () => onSelectJob(job.job_id),
      });
    for (const t of targets) {
      const btn = TRANSITION_BUTTONS[t];
      if (!btn) continue;
      items.push({
        id: t,
        label: t === "failed" ? `${btn.label}…` : btn.label,
        icon: btn.icon,
        danger: t === "failed" || t === "cancelled",
        onSelect: () =>
          t === "failed"
            ? setFailing(job.job_id)
            : void onTransition(job.job_id, t).catch(() => {}),
      });
    }
    return items;
  };

  return (
    <>
      <DataTable<Job>
        ariaLabel="Job board"
        columns={columns}
        data={sorted}
        getRowId={(job) => job.job_id}
        rowLabel={(job) => `job ${job.job_id}`}
        onRowClick={onSelectJob ? (job) => onSelectJob(job.job_id) : undefined}
        sort={{ key: sortField, direction: sortOrder }}
        onSortChange={handleSort}
        rowMenu={rowMenu}
        loading={loading}
        error={error}
        emptyState={
          <div>
            <p className="text-sm font-semibold text-slate-800">
              No jobs found
            </p>
            <p className="mt-1 text-xs text-text-muted">
              Try another status or clear the filters.
            </p>
          </div>
        }
      />
      {failing && (
        <FormDialog<{ reason: string }>
          open
          size="sm"
          title={`Mark job ${failing} as failed`}
          help="A reason is required and is recorded on the job's event timeline."
          submitLabel="Mark failed"
          initialValues={{ reason: "" }}
          validate={(v) =>
            v.reason.trim() ? {} : { reason: "Enter a reason." }
          }
          onSubmit={async (v) => {
            await onTransition(failing, "failed", v.reason.trim());
          }}
          successMessage={`Job ${failing} marked failed`}
          onClose={() => setFailing(null)}
        >
          {({ values, set, errors }) => (
            <Field
              label="Failure reason"
              required
              error={errors.reason}
              span={2}
            >
              <textarea
                rows={3}
                value={values.reason}
                onChange={(e) => set("reason", e.target.value)}
                placeholder="Customer site inaccessible, equipment breakdown…"
                className={`${INPUT_CLASS} h-auto py-2`}
              />
            </Field>
          )}
        </FormDialog>
      )}
    </>
  );
}
