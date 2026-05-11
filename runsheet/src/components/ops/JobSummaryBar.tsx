"use client";

import type { Job } from "../../types/api";

interface JobSummaryBarProps {
  jobs: Job[];
}

/**
 * Summary bar showing counts of jobs by status.
 * Displays: total, scheduled, assigned, in_progress, completed, failed, delayed.
 *
 * Validates: Requirement 11.5
 */
export default function JobSummaryBar({ jobs }: JobSummaryBarProps) {
  const total = jobs.length;
  const scheduled = jobs.filter((j) => j.status === "scheduled").length;
  const assigned = jobs.filter((j) => j.status === "assigned").length;
  const inProgress = jobs.filter((j) => j.status === "in_progress").length;
  const completed = jobs.filter((j) => j.status === "completed").length;
  const failed = jobs.filter((j) => j.status === "failed").length;
  const delayed = jobs.filter((j) => j.delayed).length;

  const stats = [
    { label: "Total", value: total, color: "text-primary" },
    { label: "Scheduled", value: scheduled, color: "text-info" },
    { label: "Assigned", value: assigned, color: "text-warning" },
    { label: "In Progress", value: inProgress, color: "text-success" },
    { label: "Completed", value: completed, color: "text-gray-600" },
    { label: "Failed", value: failed, color: "text-error" },
    { label: "Delayed", value: delayed, color: "text-warning" },
  ];

  return (
    <div className="grid grid-cols-7 gap-4">
      {stats.map((stat) => (
        <div key={stat.label} className="text-center">
          <div className={`text-2xl font-semibold ${stat.color}`}>
            {stat.value}
          </div>
          <div className="text-sm text-gray-500">{stat.label}</div>
        </div>
      ))}
    </div>
  );
}
