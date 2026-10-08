"use client";

import {
  CalendarClock,
  ChevronDown,
  ChevronUp,
  Plus,
  Search,
} from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { useSchedulingWebSocket } from "../../hooks/useSchedulingWebSocket";
import {
  type JobFilters as ApiJobFilters,
  getJobs,
  transitionStatus,
} from "../../services/schedulingApi";
import type { Job, JobStatus } from "../../types/api";
import LoadingSpinner from "../LoadingSpinner";
import CreateJobModal from "../ops/CreateJobModal";
import JobBoard from "../ops/JobBoard";
import JobDetailPage from "../ops/JobDetailPage";
import JobFilters, { type JobFilterValues } from "../ops/JobFilters";
import JobSummaryBar from "../ops/JobSummaryBar";
import { ExportCsvButton } from "../ui/ExportCsvButton";
import { PageTitle } from "../ui/PageHeader";

const CargoSearchSection = lazy(() => import("../ops/CargoSearchSection"));

const INITIAL_FILTERS: JobFilterValues = {
  job_type: "",
  status: "",
  start_date: "",
  end_date: "",
  asset_assigned: "",
};

/** The list's filter mapping, shared by the job fetch and the CSV export. */
function toApiJobFilters(filters: JobFilterValues): ApiJobFilters {
  const apiFilters: ApiJobFilters = {};
  if (filters.job_type) apiFilters.job_type = filters.job_type;
  if (filters.status) apiFilters.status = filters.status;
  if (filters.asset_assigned)
    apiFilters.asset_assigned = filters.asset_assigned;
  if (filters.start_date) apiFilters.start_date = filters.start_date;
  if (filters.end_date) apiFilters.end_date = filters.end_date;
  return apiFilters;
}

/**
 * Job Board page — scheduling dashboard.
 *
 * Displays a live, sortable, color-coded job board with summary bar,
 * filters, and real-time WebSocket updates within 5 seconds.
 *
 * Validates: Requirements 11.1-11.7
 */
export default function SchedulingJobBoardPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<JobFilterValues>(INITIAL_FILTERS);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showCargoSearch, setShowCargoSearch] = useState(false);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);

      const res = await getJobs(toApiJobFilters(filters));
      setJobs(res.data);
    } catch (error) {
      console.error("Failed to load job data:", error);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  /**
   * Handle real-time job updates via WebSocket.
   * Updates the affected row within 5 seconds.
   *
   * Validates: Requirement 11.6
   */
  const handleJobCreated = useCallback((event: { job: Job }) => {
    setJobs((prev) => [event.job, ...prev]);
  }, []);

  const handleStatusChanged = useCallback(
    (event: {
      job_id: string;
      new_status: string;
      old_status: string;
      asset_assigned?: string;
      estimated_arrival?: string;
    }) => {
      setJobs((prev) =>
        prev.map((j) =>
          j.job_id === event.job_id
            ? {
                ...j,
                status: event.new_status as JobStatus,
                asset_assigned: event.asset_assigned ?? j.asset_assigned,
                estimated_arrival:
                  event.estimated_arrival ?? j.estimated_arrival,
                updated_at: new Date().toISOString(),
              }
            : j,
        ),
      );
    },
    [],
  );

  const handleDelayAlert = useCallback(
    (event: { job_id: string; delay_duration_minutes: number }) => {
      setJobs((prev) =>
        prev.map((j) =>
          j.job_id === event.job_id
            ? {
                ...j,
                delayed: true,
                delay_duration_minutes: event.delay_duration_minutes,
              }
            : j,
        ),
      );
    },
    [],
  );

  useSchedulingWebSocket({
    subscriptions: ["job_created", "status_changed", "delay_alert"],
    onJobCreated: handleJobCreated,
    onStatusChanged: handleStatusChanged,
    onDelayAlert: handleDelayAlert,
  });

  /**
   * Handle status transition from action buttons.
   *
   * Validates: Requirement 11.7
   */
  const handleTransition = useCallback(
    async (jobId: string, targetStatus: JobStatus, failureReason?: string) => {
      try {
        const res = await transitionStatus(jobId, {
          status: targetStatus,
          failure_reason: failureReason,
        });
        setJobs((prev) => prev.map((j) => (j.job_id === jobId ? res.data : j)));
      } catch (error) {
        console.error("Failed to transition job status:", error);
      }
    },
    [],
  );

  if (loading) {
    return <LoadingSpinner message="Loading jobs..." />;
  }

  // Sub-navigation: show job detail when a job is selected
  if (selectedJobId) {
    return (
      <JobDetailPage
        jobId={selectedJobId}
        onBack={() => setSelectedJobId(null)}
        onTransition={handleTransition}
      />
    );
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        {/* flex-wrap: in the narrower dashboard slot the actions drop below
            the title instead of overflowing to the right (R-4). */}
        <div className="flex flex-wrap items-center justify-between gap-4 mb-6">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-primary rounded-xl flex items-center justify-center">
              <CalendarClock className="w-5 h-5 text-white" />
            </div>
            <div>
              <PageTitle className="text-2xl font-semibold text-primary">
                Job Board
              </PageTitle>
              <p className="text-gray-500">
                Manage and track all logistics jobs in real time
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => setShowCargoSearch((prev) => !prev)}
              className="flex items-center gap-2 px-4 py-2 text-sm border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50 transition-colors"
              aria-expanded={showCargoSearch}
              aria-controls="cargo-search-section"
            >
              <Search className="w-4 h-4" />
              Cargo Search
              {showCargoSearch ? (
                <ChevronUp className="w-4 h-4" />
              ) : (
                <ChevronDown className="w-4 h-4" />
              )}
            </button>
            <ExportCsvButton
              type="jobs"
              params={{ ...toApiJobFilters(filters) }}
              subject="jobs"
              allowedRoles={["admin", "dispatcher"]}
            />
            <button
              onClick={() => setShowCreateModal(true)}
              className="flex items-center gap-2 px-4 py-2 text-sm text-white rounded-lg transition-colors bg-primary hover:bg-primary-hover"
            >
              <Plus className="w-4 h-4" />
              Create Job
            </button>
          </div>
        </div>

        <JobFilters filters={filters} onChange={setFilters} />
      </div>

      {/* Summary Bar */}
      <div className="border-b border-gray-100 px-8 py-4">
        <JobSummaryBar jobs={jobs} />
      </div>

      {/* Board */}
      <div className="flex-1 overflow-y-auto">
        <JobBoard
          jobs={jobs}
          onTransition={handleTransition}
          onSelectJob={setSelectedJobId}
        />
      </div>

      {/* Cargo Search — collapsible section */}
      {showCargoSearch && (
        <div
          id="cargo-search-section"
          className="border-t border-gray-200 px-8 py-6"
        >
          <Suspense
            fallback={
              <div className="flex items-center justify-center py-8 text-gray-500 text-sm">
                Loading cargo search...
              </div>
            }
          >
            <CargoSearchSection />
          </Suspense>
        </div>
      )}

      {/* Create Job Modal */}
      {showCreateModal && (
        <CreateJobModal
          onClose={() => setShowCreateModal(false)}
          onCreated={(job) => setJobs((prev) => [job, ...prev])}
        />
      )}
    </div>
  );
}
