"use client";

import { Play, CheckCircle, XCircle, AlertTriangle } from "lucide-react";
import { useState } from "react";
import type { JobStatus } from "../../types/api";

/**
 * Valid status transitions per the design spec state machine.
 * scheduled → [assigned, cancelled]
 * assigned → [in_progress, cancelled]
 * in_progress → [completed, failed, cancelled]
 * completed → []
 * cancelled → []
 * failed → []
 */
const VALID_TRANSITIONS: Record<JobStatus, JobStatus[]> = {
  scheduled: ["assigned", "cancelled"],
  assigned: ["in_progress", "cancelled"],
  in_progress: ["completed", "failed", "cancelled"],
  completed: [],
  cancelled: [],
  failed: [],
};

interface TransitionButton {
  targetStatus: JobStatus;
  label: string;
  icon: React.ReactNode;
  className: string;
}

const TRANSITION_BUTTONS: Record<string, TransitionButton> = {
  assigned: {
    targetStatus: "assigned",
    label: "Assign",
    icon: <Play className="w-3 h-3" />,
    className: "text-orange-700 bg-orange-100 hover:bg-orange-200",
  },
  in_progress: {
    targetStatus: "in_progress",
    label: "Start",
    icon: <Play className="w-3 h-3" />,
    className: "text-blue-700 bg-blue-100 hover:bg-blue-200",
  },
  completed: {
    targetStatus: "completed",
    label: "Complete",
    icon: <CheckCircle className="w-3 h-3" />,
    className: "text-green-700 bg-green-100 hover:bg-green-200",
  },
  failed: {
    targetStatus: "failed",
    label: "Fail",
    icon: <AlertTriangle className="w-3 h-3" />,
    className: "text-red-700 bg-red-100 hover:bg-red-200",
  },
  cancelled: {
    targetStatus: "cancelled",
    label: "Cancel",
    icon: <XCircle className="w-3 h-3" />,
    className: "text-gray-700 bg-gray-100 hover:bg-gray-200",
  },
};

interface JobActionButtonsProps {
  jobId: string;
  currentStatus: JobStatus;
  onTransition: (jobId: string, targetStatus: JobStatus, failureReason?: string) => Promise<void>;
}

/**
 * Status transition action buttons for a job row.
 * Shows only valid transitions based on the current status.
 *
 * Validates: Requirement 11.7
 */
export default function JobActionButtons({
  jobId,
  currentStatus,
  onTransition,
}: JobActionButtonsProps) {
  const [loading, setLoading] = useState<JobStatus | null>(null);

  const validTargets = VALID_TRANSITIONS[currentStatus] ?? [];

  if (validTargets.length === 0) return null;

  const handleClick = async (targetStatus: JobStatus) => {
    setLoading(targetStatus);
    try {
      let failureReason: string | undefined;
      if (targetStatus === "failed") {
        const reason = window.prompt("Enter failure reason:");
        if (!reason) {
          setLoading(null);
          return;
        }
        failureReason = reason;
      }
      await onTransition(jobId, targetStatus, failureReason);
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="flex items-center gap-1">
      {validTargets.map((target) => {
        const btn = TRANSITION_BUTTONS[target];
        if (!btn) return null;
        const isLoading = loading === target;
        return (
          <button
            key={target}
            onClick={() => handleClick(target)}
            disabled={loading !== null}
            className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium transition-colors ${btn.className} disabled:opacity-50`}
            aria-label={`${btn.label} job ${jobId}`}
          >
            {isLoading ? (
              <div className="w-3 h-3 animate-spin rounded-full border border-current border-t-transparent" />
            ) : (
              btn.icon
            )}
            {btn.label}
          </button>
        );
      })}
    </div>
  );
}
