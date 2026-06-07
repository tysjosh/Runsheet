"use client";

import { ChevronDown, ChevronUp, ExternalLink } from "lucide-react";
import Link from "next/link";
import { useCallback, useState } from "react";
import { type Column, Table } from "@/components/ui";
import type { Job, JobStatus } from "../../types/api";
import JobActionButtons from "./JobActionButtons";

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
}

/**
 * Row background color based on job status.
 * Delayed jobs get an orange overlay regardless of status.
 *
 * Validates: Requirement 11.2
 */
function getRowColor(job: Job): string {
  if (job.delayed) return "bg-warning-light";
  switch (job.status) {
    case "scheduled":
      return "bg-info-light";
    case "assigned":
      return "bg-warning-light";
    case "in_progress":
      return "bg-success-light";
    case "completed":
      return "bg-gray-50";
    case "failed":
      return "bg-error-light";
    case "cancelled":
      return "bg-gray-50";
    default:
      return "";
  }
}

function getStatusBadge(status: JobStatus, delayed: boolean): string {
  if (delayed) return "text-warning-dark bg-warning-light";
  switch (status) {
    case "scheduled":
      return "text-info-dark bg-info-light";
    case "assigned":
      return "text-warning-dark bg-warning-light";
    case "in_progress":
      return "text-success-dark bg-success-light";
    case "completed":
      return "text-gray-600 bg-gray-100";
    case "failed":
      return "text-error-dark bg-error-light";
    case "cancelled":
      return "text-gray-500 bg-gray-100";
    default:
      return "text-gray-700 bg-gray-100";
  }
}

function formatDate(dateStr?: string): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatJobType(jobType: string): string {
  return jobType
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
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

/**
 * Sortable job board with color-coded rows and action buttons.
 *
 * Validates: Requirements 11.1, 11.2, 11.4, 11.7
 */
export default function JobBoard({
  jobs,
  onTransition,
  onSelectJob,
}: JobBoardProps) {
  const [sortField, setSortField] = useState<SortField>("scheduled_time");
  const [sortOrder, setSortOrder] = useState<SortOrder>("asc");

  const handleSort = useCallback(
    (field: SortField) => {
      if (sortField === field) {
        setSortOrder((prev) => (prev === "asc" ? "desc" : "asc"));
      } else {
        setSortField(field);
        setSortOrder("asc");
      }
    },
    [sortField],
  );

  const sorted = [...jobs].sort((a, b) => {
    const aVal = a[sortField] as string | undefined;
    const bVal = b[sortField] as string | undefined;
    return compareValues(aVal, bVal, sortOrder);
  });

  const sortableHeader = (field: SortField, label: string) => (
    <button
      type="button"
      onClick={() => handleSort(field)}
      aria-sort={
        sortField === field
          ? sortOrder === "asc"
            ? "ascending"
            : "descending"
          : "none"
      }
      className="flex items-center text-xs font-medium text-gray-600 uppercase tracking-wider"
    >
      {label}
      {sortField === field &&
        (sortOrder === "asc" ? (
          <ChevronUp className="w-3 h-3 inline ml-1" />
        ) : (
          <ChevronDown className="w-3 h-3 inline ml-1" />
        ))}
    </button>
  );

  const columns: Column<Job>[] = [
    {
      key: "job_id",
      label: sortableHeader("job_id", "Job ID"),
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm font-medium text-primary",
      render: (job) =>
        job.job_type === "cargo_transport" ? (
          <Link
            href={`/ops/scheduling/${encodeURIComponent(job.job_id)}/cargo`}
            className="hover:underline flex items-center gap-1"
            onClick={(e) => e.stopPropagation()}
          >
            {job.job_id}
            <ExternalLink className="w-3 h-3 text-gray-500" />
          </Link>
        ) : (
          job.job_id
        ),
    },
    {
      key: "job_type",
      label: sortableHeader("job_type", "Type"),
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm text-gray-700",
      render: (job) => formatJobType(job.job_type),
    },
    {
      key: "status",
      label: sortableHeader("status", "Status"),
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      render: (job) => (
        <span
          className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(job.status, job.delayed)}`}
        >
          {job.delayed ? "Delayed" : job.status.replace(/_/g, " ")}
        </span>
      ),
    },
    {
      key: "origin",
      label: sortableHeader("origin", "Origin"),
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm text-gray-700",
      render: (job) => job.origin,
    },
    {
      key: "destination",
      label: sortableHeader("destination", "Destination"),
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm text-gray-700",
      render: (job) => job.destination,
    },
    {
      key: "asset_assigned",
      label: sortableHeader("asset_assigned", "Asset"),
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm text-gray-700",
      render: (job) => job.asset_assigned ?? "—",
    },
    {
      key: "scheduled_time",
      label: sortableHeader("scheduled_time", "Scheduled"),
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm text-gray-600",
      render: (job) => formatDate(job.scheduled_time),
    },
    {
      key: "estimated_arrival",
      label: sortableHeader("estimated_arrival", "Est. Arrival"),
      headerClassName: "cursor-pointer select-none hover:bg-gray-100",
      className: "text-sm text-gray-600",
      render: (job) => formatDate(job.estimated_arrival),
    },
    {
      key: "actions",
      label: "Actions",
      // Stop row-click propagation so action buttons don't trigger navigation.
      render: (job) => (
        <div onClick={(e) => e.stopPropagation()}>
          <JobActionButtons
            jobId={job.job_id}
            currentStatus={job.status}
            onTransition={onTransition}
          />
        </div>
      ),
    },
  ];

  return (
    <Table<Job>
      ariaLabel="Job board"
      variant="standard"
      columns={columns}
      data={sorted}
      getRowId={(job) => job.job_id}
      onRowClick={onSelectJob ? (job) => onSelectJob(job.job_id) : undefined}
      rowClassName={(job) => getRowColor(job)}
      emptyState={
        <div className="text-gray-500">
          <p className="text-lg font-medium text-gray-500">No jobs found</p>
          <p className="text-sm text-gray-500 mt-1">
            Try adjusting your filters
          </p>
        </div>
      }
    />
  );
}
