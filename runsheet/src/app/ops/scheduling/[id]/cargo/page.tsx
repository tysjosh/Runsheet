"use client";

import { ArrowLeft, Package } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import LoadingSpinner from "../../../../../components/LoadingSpinner";
import CargoManifestEditor from "../../../../../components/ops/CargoManifestEditor";
import { useSchedulingWebSocket } from "../../../../../hooks/useSchedulingWebSocket";
import type {
  CargoItemStatus,
  Job,
  JobStatus,
  SchedulingCargoItem,
} from "../../../../../types/api";
import {
  getCargo,
  getJob,
} from "../../../../../services/schedulingApi";

/**
 * Job status badge color-coding (matches Job Board pattern).
 */
function getJobStatusBadge(status: JobStatus): string {
  switch (status) {
    case "scheduled":
      return "text-blue-700 bg-blue-100";
    case "assigned":
      return "text-orange-700 bg-orange-100";
    case "in_progress":
      return "text-green-700 bg-green-100";
    case "completed":
      return "text-gray-700 bg-gray-100";
    case "failed":
      return "text-red-700 bg-red-100";
    case "cancelled":
      return "text-gray-500 bg-gray-100";
    default:
      return "text-gray-700 bg-gray-100";
  }
}

function formatStatus(status: string): string {
  return status
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/**
 * Cargo Tracking page — displays cargo manifest for a cargo_transport job.
 *
 * Shows job header with job_id, origin, destination, asset_assigned, and
 * overall status. Lists cargo items with status color-coding and action
 * buttons. Updates in real time via WebSocket within 5 seconds.
 *
 * Validates: Requirements 12.1-12.5
 */
export default function CargoTrackingPage() {
  const params = useParams<{ id: string }>();
  const jobId = params.id;

  const [job, setJob] = useState<Job | null>(null);
  const [cargoItems, setCargoItems] = useState<SchedulingCargoItem[]>([]);
  const [loading, setLoading] = useState(true);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const [jobRes, cargoRes] = await Promise.all([
        getJob(jobId),
        getCargo(jobId),
      ]);
      setJob(jobRes.data);
      setCargoItems(cargoRes.data);
    } catch (error) {
      console.error("Failed to load cargo data:", error);
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  /**
   * Handle real-time cargo updates via WebSocket.
   * Updates the affected item row within 5 seconds.
   *
   * Validates: Requirement 12.5
   */
  const handleCargoUpdate = useCallback(
    (event: {
      job_id: string;
      item_id: string;
      new_status: string;
      item?: SchedulingCargoItem;
    }) => {
      if (event.job_id !== jobId) return;

      setCargoItems((prev) =>
        prev.map((item) =>
          item.item_id === event.item_id
            ? event.item ?? {
                ...item,
                item_status: event.new_status as CargoItemStatus,
              }
            : item,
        ),
      );
    },
    [jobId],
  );

  /**
   * Also listen for status_changed events to update the job header.
   */
  const handleStatusChanged = useCallback(
    (event: { job_id: string; new_status: string }) => {
      if (event.job_id !== jobId) return;
      setJob((prev) =>
        prev
          ? { ...prev, status: event.new_status as JobStatus }
          : prev,
      );
    },
    [jobId],
  );

  useSchedulingWebSocket({
    subscriptions: ["cargo_update", "status_changed"],
    onCargoUpdate: handleCargoUpdate,
    onStatusChanged: handleStatusChanged,
  });

  /**
   * Handle cargo items change from CargoManifestEditor (edit saves or status updates).
   */
  const handleItemsChange = useCallback(
    (updatedItems: SchedulingCargoItem[]) => {
      setCargoItems(updatedItems);
    },
    [],
  );

  if (loading) {
    return <LoadingSpinner message="Loading cargo manifest..." />;
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center gap-3 mb-4">
          <Link
            href="/ops/scheduling"
            className="text-gray-400 hover:text-[#232323] transition-colors"
            aria-label="Back to Job Board"
          >
            <ArrowLeft className="w-5 h-5" />
          </Link>
          <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
            <Package className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-[#232323]">
              Cargo Tracking
            </h1>
            <p className="text-gray-500">
              Manifest and item-level status for {jobId}
            </p>
          </div>
        </div>

        {/* Job header info */}
        {job && (
          <div className="flex flex-wrap items-center gap-4 mt-2 text-sm">
            <div>
              <span className="text-gray-500">Job ID: </span>
              <span className="font-medium text-[#232323]">{job.job_id}</span>
            </div>
            <div>
              <span className="text-gray-500">Origin: </span>
              <span className="text-gray-700">{job.origin}</span>
            </div>
            <div>
              <span className="text-gray-500">Destination: </span>
              <span className="text-gray-700">{job.destination}</span>
            </div>
            <div>
              <span className="text-gray-500">Asset: </span>
              <span className="text-gray-700">
                {job.asset_assigned ?? "Unassigned"}
              </span>
            </div>
            <div>
              <span className="text-gray-500">Status: </span>
              <span
                className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getJobStatusBadge(job.status)}`}
              >
                {formatStatus(job.status)}
              </span>
            </div>
          </div>
        )}
      </div>

      {/* Cargo Manifest */}
      <div className="flex-1 overflow-y-auto">
        <CargoManifestEditor
          jobId={jobId}
          items={cargoItems}
          onItemsChange={handleItemsChange}
        />
      </div>
    </div>
  );
}
