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
    { label: "Total", value: total, color: "text-[#232323]" },
    { label: "Scheduled", value: scheduled, color: "text-blue-600" },
    { label: "Assigned", value: assigned, color: "text-orange-600" },
    { label: "In Progress", value: inProgress, color: "text-green-600" },
    { label: "Completed", value: completed, color: "text-gray-600" },
    { label: "Failed", value: failed, color: "text-red-600" },
    { label: "Delayed", value: delayed, color: "text-orange-600" },
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
