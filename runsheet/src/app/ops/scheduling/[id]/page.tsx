"use client";

/**
 * Standalone route at ``/ops/scheduling/:id``. Reads the path param and renders
 * the shared {@link JobDetailPage} (the Job Board renders the same component
 * in-place when a job is selected). This route is the canonical owning-module
 * destination that {@link EntityLink} links a ``job`` reference to.
 */

import { useParams, useRouter } from "next/navigation";
import { useCallback } from "react";
import JobDetailPage from "../../../../components/ops/JobDetailPage";
import { transitionStatus } from "../../../../services/schedulingApi";
import type { JobStatus } from "../../../../types/api";

export default function JobPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const jobId = params?.id ?? "";

  const handleTransition = useCallback(
    async (id: string, targetStatus: JobStatus, failureReason?: string) => {
      try {
        await transitionStatus(id, {
          status: targetStatus,
          failure_reason: failureReason,
        });
      } catch (error) {
        console.error("Failed to transition job status:", error);
      }
    },
    [],
  );

  return (
    <JobDetailPage
      jobId={jobId}
      onBack={() => router.back()}
      onTransition={handleTransition}
    />
  );
}
