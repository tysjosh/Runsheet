"use client";

import { ChevronDown, ChevronUp, ExternalLink } from "lucide-react";
import Link from "next/link";
import { useCallback, useState } from "react";
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
  onTransition: (jobId: string, targetStatus: JobStatus, failureReason?: string) => Promise<void>;
}

const COLUMNS: { key: SortField; label: string }[] = [
  { key: "job_id", label: "Job ID" },
  { key: "job_type", label: "Type" },
  { key: "status", label: "Status" },
  { key: "origin", label: "Origin" },
  { key: "destination", label: "Destination" },
  { key: "asset_assigned", label: "Asset" },
  { key: "scheduled_time", label: "Scheduled" },
  { key: "estimated_arrival", label: "Est. Arrival" },
];

/**
 * Row background color based on job status.
 * Delayed jobs get an orange overlay regardless of status.
 *
 * Validates: Requirement 11.2
 */
function getRowColor(job: Job): string {
  if (job.delayed) return "bg-yellow-50";
  switch (job.status) {
    case "scheduled":
      return "bg-blue-50";
    case "assigned":
      return "bg-orange-50";
    case "in_progress":
      return "bg-green-50";
    case "completed":
      return "bg-gray-50";
    case "failed":
      return "bg-red-50";
    case "cancelled":
      return "bg-gray-50";
    default:
      return "";
  }
}

function getStatusBadge(status: JobStatus, delayed: boolean): string {
  if (delayed) return "text-yellow-700 bg-yellow-100";
  switch (status) {
    case "scheduled":
      return "text-blue-700 bg-blue-100";
    case "assigned":
      return "text-orange-700 bg-orange-100";
    case "in_progress":
      return "text-green-700 bg-green-100";
    case "completed":
      return "text-gray-600 bg-gray-100";
    case "failed":
      return "text-red-700 bg-red-100";
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
export default function JobBoard({ jobs, onTransition }: JobBoardProps) {
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

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null;
    return sortOrder === "asc" ? (
      <ChevronUp className="w-3 h-3 inline ml-1" />
    ) : (
      <ChevronDown className="w-3 h-3 inline ml-1" />
    );
  };

  if (jobs.length === 0) {
    return (
      <div className="text-center py-16 text-gray-500">
        <p className="text-lg font-medium text-gray-400">No jobs found</p>
        <p className="text-sm text-gray-400 mt-1">Try adjusting your filters</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full" aria-label="Job board">
        <thead className="bg-gray-50 sticky top-0 border-b border-gray-100">
          <tr>
            {COLUMNS.map((col) => (
              <th
                key={col.key}
                className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer select-none hover:bg-gray-100"
                onClick={() => handleSort(col.key)}
                aria-sort={
                  sortField === col.key
                    ? sortOrder === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
              >
                {col.label}
                <SortIcon field={col.key} />
              </th>
            ))}
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-600 uppercase tracking-wider">
              Actions
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {sorted.map((job) => (
            <tr
              key={job.job_id}
              className={`${getRowColor(job)} transition-colors`}
            >
              <td className="px-6 py-3 text-sm font-medium text-[#232323]">
                {job.job_type === "cargo_transport" ? (
                  <Link
                    href={`/ops/scheduling/${encodeURIComponent(job.job_id)}/cargo`}
                    className="hover:underline flex items-center gap-1"
                  >
                    {job.job_id}
                    <ExternalLink className="w-3 h-3 text-gray-400" />
                  </Link>
                ) : (
                  job.job_id
                )}
              </td>
              <td className="px-6 py-3 text-sm text-gray-700">
                {formatJobType(job.job_type)}
              </td>
              <td className="px-6 py-3">
                <span
                  className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(job.status, job.delayed)}`}
                >
                  {job.delayed ? "Delayed" : job.status.replace(/_/g, " ")}
                </span>
              </td>
              <td className="px-6 py-3 text-sm text-gray-700">{job.origin}</td>
              <td className="px-6 py-3 text-sm text-gray-700">{job.destination}</td>
              <td className="px-6 py-3 text-sm text-gray-700">
                {job.asset_assigned ?? "—"}
              </td>
              <td className="px-6 py-3 text-sm text-gray-600">
                {formatDate(job.scheduled_time)}
              </td>
              <td className="px-6 py-3 text-sm text-gray-600">
                {formatDate(job.estimated_arrival)}
              </td>
              <td className="px-6 py-3">
                <JobActionButtons
                  jobId={job.job_id}
                  currentStatus={job.status}
                  onTransition={onTransition}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
