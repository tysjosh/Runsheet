"use client";

import { Clock } from "lucide-react";
import type { Job } from "../../types/api";

interface DelayedOperationsPanelProps {
  jobs: Job[];
}

function formatDelay(minutes?: number): string {
  if (!minutes || minutes <= 0) return "—";
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

/**
 * Delayed operations panel highlighting jobs that have exceeded
 * their estimated_arrival with delay duration.
 *
 * Validates: Requirement 10.5
 */
export default function DelayedOperationsPanel({
  jobs,
}: DelayedOperationsPanelProps) {
  const delayedJobs = jobs
    .filter((j) => j.delayed)
    .sort(
      (a, b) =>
        (b.delay_duration_minutes ?? 0) - (a.delay_duration_minutes ?? 0),
    );

  return (
    <div className="bg-white rounded-xl border border-gray-100">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100">
        <Clock className="w-4 h-4 text-red-600" />
        <h3 className="text-sm font-semibold text-[#232323]">
          Delayed Operations
        </h3>
        <span className="ml-auto text-xs text-red-500 font-medium">
          {delayedJobs.length}
        </span>
      </div>

      <div className="max-h-48 overflow-y-auto divide-y divide-gray-50">
        {delayedJobs.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-green-600">
            No delayed operations
          </div>
        ) : (
          delayedJobs.map((job) => (
            <div
              key={job.job_id}
              className="px-4 py-2.5 hover:bg-red-50/50"
            >
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-[#232323]">
                  {job.job_id}
                </span>
                <span className="px-2 py-0.5 rounded bg-red-100 text-red-700 text-xs font-medium">
                  +{formatDelay(job.delay_duration_minutes)}
                </span>
              </div>
              <p className="text-xs text-gray-500 mt-0.5 truncate">
                {job.origin} → {job.destination}
              </p>
              {job.asset_assigned && (
                <p className="text-xs text-gray-400 mt-0.5">
                  Asset: {job.asset_assigned}
                </p>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
