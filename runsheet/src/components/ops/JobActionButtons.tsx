"use client";

import { AlertTriangle, CheckCircle, Play, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { JobStatus } from "../../types/api";
import { Modal, ModalFooter } from "../ui";

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
    className: "text-warning-dark bg-warning-light hover:bg-warning-light",
  },
  in_progress: {
    targetStatus: "in_progress",
    label: "Start",
    icon: <Play className="w-3 h-3" />,
    className: "text-info-dark bg-info-light hover:bg-info-light",
  },
  completed: {
    targetStatus: "completed",
    label: "Complete",
    icon: <CheckCircle className="w-3 h-3" />,
    className: "text-success-dark bg-success-light hover:bg-success-light",
  },
  failed: {
    targetStatus: "failed",
    label: "Fail",
    icon: <AlertTriangle className="w-3 h-3" />,
    className: "text-error-dark bg-error-light hover:bg-error-light",
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
  onTransition: (
    jobId: string,
    targetStatus: JobStatus,
    failureReason?: string,
  ) => Promise<void>;
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
  // "Fail" requires a reason. Rather than a browser window.prompt (which is
  // unvalidated, can be suppressed by the browser, and breaks the visual
  // flow), capture it in a proper modal with a required field.
  const [showFailModal, setShowFailModal] = useState(false);
  const [failReason, setFailReason] = useState("");
  const failReasonRef = useRef<HTMLTextAreaElement>(null);

  // Focus the reason field when the fail modal opens (accessible alternative
  // to the autoFocus attribute).
  useEffect(() => {
    if (showFailModal) {
      const t = setTimeout(() => failReasonRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
  }, [showFailModal]);

  const validTargets = VALID_TRANSITIONS[currentStatus] ?? [];

  if (validTargets.length === 0) return null;

  const runTransition = async (
    targetStatus: JobStatus,
    failureReason?: string,
  ) => {
    setLoading(targetStatus);
    try {
      await onTransition(jobId, targetStatus, failureReason);
    } catch {
      // Error is handled by the parent component (displayed inline)
    } finally {
      setLoading(null);
    }
  };

  const handleClick = async (targetStatus: JobStatus) => {
    if (targetStatus === "failed") {
      // Defer the transition until the reason is confirmed in the modal.
      setFailReason("");
      setShowFailModal(true);
      return;
    }
    await runTransition(targetStatus);
  };

  const handleConfirmFail = async () => {
    const reason = failReason.trim();
    if (!reason) return;
    setShowFailModal(false);
    await runTransition("failed", reason);
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

      <Modal
        isOpen={showFailModal}
        onClose={() => setShowFailModal(false)}
        title="Mark job as failed"
        size="sm"
        footer={
          <ModalFooter
            onCancel={() => setShowFailModal(false)}
            onConfirm={handleConfirmFail}
            confirmText="Mark failed"
            confirmVariant="danger"
            loading={loading === "failed"}
          />
        }
      >
        <label
          htmlFor={`fail-reason-${jobId}`}
          className="block text-sm font-medium text-gray-700 mb-2"
        >
          Failure reason
        </label>
        <textarea
          id={`fail-reason-${jobId}`}
          ref={failReasonRef}
          value={failReason}
          onChange={(e) => setFailReason(e.target.value)}
          rows={3}
          placeholder="Describe why this job failed (e.g. customer site inaccessible, equipment breakdown)…"
          className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none resize-none"
        />
        <p className="mt-2 text-xs text-gray-500">
          A reason is required and will be recorded on the job's event timeline.
        </p>
      </Modal>
    </div>
  );
}
