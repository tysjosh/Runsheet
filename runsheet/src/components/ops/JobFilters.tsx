"use client";

import { Filter } from "lucide-react";
import type { JobType, JobStatus } from "../../types/api";

export interface JobFilterValues {
  job_type: JobType | "";
  status: JobStatus | "";
  start_date: string;
  end_date: string;
  asset_assigned: string;
}

interface JobFiltersProps {
  filters: JobFilterValues;
  onChange: (filters: JobFilterValues) => void;
}

const JOB_TYPE_OPTIONS: { value: JobType | ""; label: string }[] = [
  { value: "", label: "All Job Types" },
  { value: "cargo_transport", label: "Cargo Transport" },
  { value: "passenger_transport", label: "Passenger Transport" },
  { value: "vessel_movement", label: "Vessel Movement" },
  { value: "airport_transfer", label: "Airport Transfer" },
  { value: "crane_booking", label: "Crane Booking" },
];

const STATUS_OPTIONS: { value: JobStatus | ""; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "scheduled", label: "Scheduled" },
  { value: "assigned", label: "Assigned" },
  { value: "in_progress", label: "In Progress" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
];

/**
 * Filter controls for the job board.
 * Supports filtering by job_type, status, date range, and asset_assigned.
 *
 * Validates: Requirement 11.3
 */
export default function JobFilters({ filters, onChange }: JobFiltersProps) {
  const update = (patch: Partial<JobFilterValues>) => {
    onChange({ ...filters, ...patch });
  };

  return (
    <div className="flex flex-wrap items-center gap-3">
      <Filter className="w-4 h-4 text-gray-400" aria-hidden="true" />

      <select
        value={filters.job_type}
        onChange={(e) => update({ job_type: e.target.value as JobType | "" })}
        className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
        aria-label="Filter by job type"
      >
        {JOB_TYPE_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>

      <select
        value={filters.status}
        onChange={(e) => update({ status: e.target.value as JobStatus | "" })}
        className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
        aria-label="Filter by status"
      >
        {STATUS_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>

      <input
        type="text"
        placeholder="Asset ID"
        value={filters.asset_assigned}
        onChange={(e) => update({ asset_assigned: e.target.value })}
        className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 w-36"
        aria-label="Filter by asset assigned"
      />

      <input
        type="date"
        value={filters.start_date}
        onChange={(e) => update({ start_date: e.target.value })}
        className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
        aria-label="Start date"
      />

      <span className="text-gray-400 text-sm">to</span>

      <input
        type="date"
        value={filters.end_date}
        onChange={(e) => update({ end_date: e.target.value })}
        className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
        aria-label="End date"
      />
    </div>
  );
}
