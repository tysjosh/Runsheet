"use client";

import { CalendarClock } from "lucide-react";
import type { Job, JobStatus } from "../../types/api";

interface JobQueuePanelProps {
  jobs: Job[];
}

const STATUS_BADGE: Record<JobStatus, { bg: string; text: string }> = {
  scheduled: { bg: "bg-blue-100", text: "text-blue-700" },
  assigned: { bg: "bg-orange-100", text: "text-orange-700" },
  in_progress: { bg: "bg-green-100", text: "text-green-700" },
  completed: { bg: "bg-gray-100", text: "text-gray-600" },
  cancelled: { bg: "bg-gray-100", text: "text-gray-500" },
  failed: { bg: "bg-red-100", text: "text-red-700" },
};

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/**
 * Job queue panel showing upcoming scheduled and assigned jobs
 * sorted by scheduled_time.
 *
 * Validates: Requirement 10.4
 */
export default function JobQueuePanel({ jobs }: JobQueuePanelProps) {
  const queuedJobs = jobs
    .filter((j) => j.status === "scheduled" || j.status === "assigned")
    .sort(
      (a, b) =>
        new Date(a.scheduled_time).getTime() -
        new Date(b.scheduled_time).getTime(),
    );

  return (
    <div className="bg-white rounded-xl border border-gray-100">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100">
        <CalendarClock className="w-4 h-4 text-[#232323]" />
        <h3 className="text-sm font-semibold text-[#232323]">Job Queue</h3>
        <span className="ml-auto text-xs text-gray-400">
          {queuedJobs.length} upcoming
        </span>
      </div>

      <div className="max-h-64 overflow-y-auto divide-y divide-gray-50">
        {queuedJobs.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-gray-400">
            No upcoming jobs
          </div>
        ) : (
          queuedJobs.map((job) => {
            const badge = STATUS_BADGE[job.status] ?? STATUS_BADGE.scheduled;
            return (
              <div key={job.job_id} className="px-4 py-2.5 hover:bg-gray-50">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-[#232323]">
                    {job.job_id}
                  </span>
                  <span
                    className={`px-2 py-0.5 rounded text-xs font-medium ${badge.bg} ${badge.text}`}
                  >
                    {job.status.replace("_", " ")}
                  </span>
                </div>
                <p className="text-xs text-gray-500 mt-0.5 truncate">
                  {job.origin} → {job.destination}
                </p>
                <p className="text-xs text-gray-400 mt-0.5">
                  {formatTime(job.scheduled_time)}
                  {job.asset_assigned && ` · ${job.asset_assigned}`}
                </p>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
