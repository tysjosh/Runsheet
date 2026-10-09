"use client";
/**
 * Standalone route at ``/dashboard/dispatch/jobs/:id``. Reads the path param and renders
 * the shared {@link JobDetailPage}. This route is the canonical owning-module
 * destination that {@link EntityLink} links a ``job`` reference to, and where
 * a row of Dispatch → Jobs opens.
 *
 * `JobDetailPage` calls the transition API itself and shows failures inline;
 * `onTransition` is its after-success notice. This route used to call the API
 * a second time and swallow that call's error in `console.error` (task 2.4):
 * it now only confirms the change with a toast.
 */
import { useParams, useRouter } from "next/navigation";
import { useCallback } from "react";
import JobDetailPage from "../../../../../components/ops/JobDetailPage";
import { notify } from "../../../../../components/ui/toast/notify";
import type { JobStatus } from "../../../../../types/api";

const DONE: Partial<Record<JobStatus, string>> = {
  assigned: "assigned",
  in_progress: "started",
  completed: "completed",
  failed: "marked failed",
  cancelled: "cancelled",
};

export default function JobPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const jobId = params?.id ?? "";
  const handleTransition = useCallback(
    async (id: string, targetStatus: JobStatus) => {
      notify({
        type: "success",
        message: `Job ${id} ${DONE[targetStatus] ?? "updated"}`,
      });
    },
    [],
  );
  return (
    <JobDetailPage
      jobId={jobId}
      onBack={() => router.push("/dashboard/dispatch?tab=jobs")}
      onTransition={handleTransition}
    />
  );
}
