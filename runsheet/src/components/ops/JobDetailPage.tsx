"use client";

/**
 * JobDetailPage — Full job detail view with event timeline, cargo manifest,
 * and status transition actions.
 *
 * Rendered as a sub-view within the existing Scheduling page when a job row
 * is clicked. Uses the same sub-navigation pattern as the fuel dashboard
 * for station detail.
 *
 * Validates:
 * - Requirement 3.1: Clicking a job row renders the detail sub-view
 * - Requirement 3.2: Displays all job fields
 * - Requirement 3.3: Event timeline in reverse chronological order
 * - Requirement 3.4: Fetches job details via GET /scheduling/jobs/{id}
 * - Requirement 3.5: Each event shows type, timestamp, actor, payload
 * - Requirement 3.6: "Back to Jobs" navigation
 * - Requirement 3.7: Status transition actions from detail view
 * - Requirement 4.1: Cargo manifest table
 * - Requirement 4.2: Cargo item status updates
 * - Requirement 4.3: Cargo manifest editing
 * - Requirement 4.7: Cargo error handling
 */

import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Calendar,
  Clock,
  FileText,
  Flag,
  Hash,
  MapPin,
  Package,
  Repeat,
  Timer,
  Truck,
  User,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { apiService } from "../../services/api";
import {
  getCargo,
  getJob,
  getJobEta,
  type JobLinks,
  type ResolvedLink,
  reassignAsset,
  transitionStatus,
} from "../../services/schedulingApi";
import type {
  Asset,
  AssetType,
  Job,
  JobEvent,
  JobStatus,
  JobType,
  SchedulingCargoItem,
} from "../../types/api";
import LoadingSpinner from "../LoadingSpinner";
import CargoManifestEditor from "./CargoManifestEditor";
import JobActionButtons from "./JobActionButtons";

// ─── Cross-Module Linkage Helpers (cross-module-entity-linkage Req 3.3, 13.1) ─

/**
 * Asset type required to service each job type. Mirrors the backend dispatch
 * compatibility map (``Agents/autonomous/delay_response_agent`` /
 * ``dispatch_optimizer``) so the asset picker only offers type-compatible
 * assets — enforcing logistics-scheduling Req 3.3 at the point of selection.
 */
const JOB_TYPE_TO_ASSET_TYPE: Record<JobType, AssetType> = {
  cargo_transport: "vehicle",
  passenger_transport: "vehicle",
  vessel_movement: "vessel",
  airport_transfer: "vehicle",
  crane_booking: "equipment",
};

/** Read an asset's type tolerating camelCase (`assetType`) or snake_case. */
function readAssetType(asset: Asset): AssetType | undefined {
  const camel = (asset as { assetType?: AssetType }).assetType;
  const snake = (asset as unknown as { asset_type?: AssetType }).asset_type;
  return camel ?? snake;
}

// ─── Props ───────────────────────────────────────────────────────────────────

interface JobDetailPageProps {
  /** The job to display */
  jobId: string;
  /** Navigation back to job board */
  onBack: () => void;
  /** Reuse parent's transition handler */
  onTransition: (
    jobId: string,
    targetStatus: JobStatus,
    failureReason?: string,
  ) => Promise<void>;
}

// ─── Helper Functions ────────────────────────────────────────────────────────

function formatDateTime(dateStr?: string): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatJobType(jobType: string): string {
  return jobType
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function formatEventType(eventType: string): string {
  return eventType
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function getStatusBadge(status: string, delayed?: boolean): string {
  if (delayed) return "text-warning-dark bg-warning-light";
  switch (status) {
    case "scheduled":
      return "text-info-dark bg-info-light";
    case "assigned":
      return "text-warning-dark bg-warning-light";
    case "in_progress":
      return "text-success-dark bg-success-light";
    case "completed":
      return "text-gray-600 bg-gray-100";
    case "failed":
      return "text-error-dark bg-error-light";
    case "cancelled":
      return "text-gray-500 bg-gray-100";
    default:
      return "text-gray-700 bg-gray-100";
  }
}

function getPriorityBadge(priority: string): string {
  switch (priority) {
    case "urgent":
      return "text-error-dark bg-error-light";
    case "high":
      return "text-warning-dark bg-warning-light";
    case "normal":
      return "text-info-dark bg-info-light";
    case "low":
      return "text-gray-600 bg-gray-100";
    default:
      return "text-gray-700 bg-gray-100";
  }
}

/**
 * Sort events in reverse chronological order (most recent first).
 * Exported for property-based testing.
 */
export function sortEventsDescending(events: JobEvent[]): JobEvent[] {
  return [...events].sort(
    (a, b) =>
      new Date(b.event_timestamp).getTime() -
      new Date(a.event_timestamp).getTime(),
  );
}

// ─── EventTimeline Sub-component ─────────────────────────────────────────────

interface EventTimelineProps {
  events: JobEvent[];
}

function EventTimeline({ events }: EventTimelineProps) {
  const sorted = sortEventsDescending(events);

  if (sorted.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400">
        <Activity className="w-8 h-8 mx-auto mb-2 opacity-50" />
        <p className="text-sm">No events recorded</p>
      </div>
    );
  }

  // Separate parts_consumed events for special rendering
  const partsConsumedEvents = sorted.filter(
    (e) => e.event_type === "parts_consumed",
  );
  const otherEvents = sorted.filter((e) => e.event_type !== "parts_consumed");

  return (
    <div className="space-y-6">
      {/* Parts Consumed Section (Requirement 7.8) */}
      {partsConsumedEvents.length > 0 && (
        <div className="border border-info rounded-lg bg-info-light/50 p-4">
          <div className="flex items-center gap-2 mb-3">
            <Package className="w-4 h-4 text-info" />
            <h4 className="text-sm font-medium text-primary">Parts Consumed</h4>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-info-light text-info-dark font-medium">
              {partsConsumedEvents.length}{" "}
              {partsConsumedEvents.length === 1 ? "event" : "events"}
            </span>
          </div>
          <div className="space-y-2">
            {partsConsumedEvents.map((event) => {
              const payload = event.event_payload || {};
              const items =
                (payload.items as Array<{
                  name?: string;
                  item_name?: string;
                  quantity?: number;
                }>) || [];
              const itemName = payload.item_name as string | undefined;
              const quantity = payload.quantity_change as number | undefined;

              return (
                <div
                  key={event.event_id}
                  className="bg-white rounded-lg px-3 py-2 border border-info"
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs text-gray-500">
                      {formatDateTime(event.event_timestamp)}
                    </span>
                    {event.actor_id && (
                      <span className="text-[10px] text-gray-400">
                        by {event.actor_id}
                      </span>
                    )}
                  </div>
                  {items.length > 0 ? (
                    <div className="space-y-1">
                      {items.map((item, idx) => (
                        <div
                          key={idx}
                          className="flex items-center justify-between text-xs"
                        >
                          <span className="text-gray-700">
                            {item.name || item.item_name || "Unknown item"}
                          </span>
                          <span className="font-medium text-info-dark">
                            -{Math.abs(item.quantity || 0)} units
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : itemName ? (
                    <div className="flex items-center justify-between text-xs">
                      <span className="text-gray-700">{itemName}</span>
                      <span className="font-medium text-info-dark">
                        -{Math.abs(quantity || 0)} units
                      </span>
                    </div>
                  ) : (
                    <div className="text-xs text-gray-500">
                      {Object.entries(payload)
                        .filter(([key]) => key !== "items")
                        .map(([key, value]) => (
                          <span key={key} className="mr-3">
                            <span className="text-gray-400">
                              {key.replace(/_/g, " ")}:
                            </span>{" "}
                            {String(value)}
                          </span>
                        ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Regular Event Timeline */}
      <div className="space-y-0">
        {otherEvents.map((event, index) => (
          <div
            key={event.event_id}
            className="relative flex gap-4 pb-6 last:pb-0"
          >
            {/* Timeline line */}
            {index < otherEvents.length - 1 && (
              <div className="absolute left-[15px] top-8 bottom-0 w-px bg-gray-200" />
            )}

            {/* Timeline dot */}
            <div className="relative z-10 flex-shrink-0 w-8 h-8 rounded-full bg-gray-100 flex items-center justify-center">
              <Activity className="w-4 h-4 text-gray-500" />
            </div>

            {/* Event content */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-medium text-primary">
                  {formatEventType(event.event_type)}
                </span>
                <span className="text-xs text-gray-400">
                  {formatDateTime(event.event_timestamp)}
                </span>
              </div>

              {event.actor_id && (
                <div className="flex items-center gap-1 mt-1">
                  <User className="w-3 h-3 text-gray-400" />
                  <span className="text-xs text-gray-500">
                    {event.actor_id}
                  </span>
                </div>
              )}

              {/* Payload fields */}
              {event.event_payload &&
                Object.keys(event.event_payload).length > 0 && (
                  <div className="mt-2 bg-gray-50 rounded-lg px-3 py-2">
                    {Object.entries(event.event_payload).map(([key, value]) => (
                      <div
                        key={key}
                        className="flex items-center gap-2 text-xs text-gray-600"
                      >
                        <span className="font-medium text-gray-500">
                          {key.replace(/_/g, " ")}:
                        </span>
                        <span>{String(value)}</span>
                      </div>
                    ))}
                  </div>
                )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Detail Field Component ──────────────────────────────────────────────────

interface DetailFieldProps {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
}

function DetailField({ icon, label, value }: DetailFieldProps) {
  return (
    <div className="flex items-start gap-3 py-2">
      <div className="flex-shrink-0 mt-0.5 text-gray-400">{icon}</div>
      <div className="min-w-0">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wider">
          {label}
        </p>
        <p className="text-sm text-primary mt-0.5">{value ?? "—"}</p>
      </div>
    </div>
  );
}

// ─── Linked Reference Field (cross-module-entity-linkage Req 13.1, 13.3) ──────

/** Pull a human label out of a resolved reference summary. */
function summaryLabel(summary: Record<string, unknown>): string | undefined {
  for (const key of [
    "display_name",
    "legal_name",
    "name",
    "driver_name",
    "customer_name",
  ]) {
    const value = summary[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

interface LinkedRefFieldProps {
  icon: React.ReactNode;
  label: string;
  /** The resolver link for this reference (may be undefined when not expanded). */
  link: ResolvedLink | undefined;
  /** Fallback id from the job document when the link was not expanded. */
  fallbackId?: string;
  /** Builds the destination href for a resolvable id. */
  href: (id: string) => string;
}

/**
 * Renders a cross-module reference as navigation to the owning module when it
 * resolves, or an explicit "Unlinked" affordance when it does not — never an
 * inert id string for a dangling reference (Req 13.1, 13.3).
 */
function LinkedRefField({
  icon,
  label,
  link,
  fallbackId,
  href,
}: LinkedRefFieldProps) {
  let content: React.ReactNode;

  if (link?.status === "resolved") {
    const display = summaryLabel(link.summary) ?? link.id;
    content = (
      <Link
        href={href(link.id)}
        className="text-info hover:text-info-dark underline underline-offset-2"
      >
        {display}
        {display !== link.id && (
          <span className="text-gray-400"> ({link.id})</span>
        )}
      </Link>
    );
  } else if (link?.status === "unresolved") {
    content = (
      <span className="inline-flex items-center gap-1.5">
        <span className="text-gray-500">{link.id}</span>
        <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium text-warning-dark bg-warning-light">
          Unlinked
        </span>
      </span>
    );
  } else if (fallbackId) {
    // Not expanded but the job document carries the id — link optimistically.
    content = (
      <Link
        href={href(fallbackId)}
        className="text-info hover:text-info-dark underline underline-offset-2"
      >
        {fallbackId}
      </Link>
    );
  } else {
    content = <span className="text-gray-400">—</span>;
  }

  return (
    <div className="flex items-start gap-3 py-2">
      <div className="flex-shrink-0 mt-0.5 text-gray-400">{icon}</div>
      <div className="min-w-0">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wider">
          {label}
        </p>
        <p className="text-sm mt-0.5">{content}</p>
      </div>
    </div>
  );
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function JobDetailPage({
  jobId,
  onBack,
  onTransition,
}: JobDetailPageProps) {
  const [job, setJob] = useState<(Job & { events?: JobEvent[] }) | null>(null);
  const [links, setLinks] = useState<JobLinks>({});
  const [cargo, setCargo] = useState<SchedulingCargoItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [transitionError, setTransitionError] = useState("");
  const [eta, setEta] = useState<{
    eta_minutes: number;
    estimated_arrival: string;
  } | null>(null);
  const [showReassign, setShowReassign] = useState(false);
  const [reassignAssetId, setReassignAssetId] = useState("");
  const [reassigning, setReassigning] = useState(false);
  // Asset picker state (Req 3.3): type-compatible assets backed by /fleet/assets.
  const [assetOptions, setAssetOptions] = useState<Asset[]>([]);
  const [assetsLoading, setAssetsLoading] = useState(false);
  const [assetsError, setAssetsError] = useState("");

  /**
   * Apply a job resolver read response, normalizing the two envelope shapes
   * (``{ job, events, links }`` vs a flat job) and capturing the ``links``
   * object so linked order/customer can be rendered (Req 5.2/5.4).
   */
  const applyJobResponse = useCallback((jobData: any) => {
    if (jobData?.job) {
      setJob({ ...jobData.job, events: jobData.events ?? [] });
    } else {
      setJob(jobData);
    }
    const resolved: JobLinks | undefined =
      jobData?.links ?? jobData?.job?.links;
    if (resolved) setLinks(resolved);
  }, []);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [jobRes, cargoRes, etaRes] = await Promise.allSettled([
        getJob(jobId, { expand: ["order", "customer", "asset", "driver"] }),
        getCargo(jobId),
        getJobEta(jobId),
      ]);

      if (jobRes.status === "fulfilled") {
        // Backend returns { job: {...}, events: [...], links: {...} } or flat
        applyJobResponse(jobRes.value.data as any);
      } else {
        throw jobRes.reason;
      }

      if (cargoRes.status === "fulfilled") {
        setCargo(cargoRes.value.data);
      }
      // Cargo may not exist for non-cargo jobs — that's fine

      if (etaRes.status === "fulfilled") {
        setEta(etaRes.value.data);
      }
      // ETA is non-critical — ignore failures
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load job details",
      );
    } finally {
      setLoading(false);
    }
  }, [jobId, applyJobResponse]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  /**
   * Load type-compatible assets for the reassign picker when it opens
   * (Req 3.3). Filtering by the job's required asset type means the picker can
   * only offer assets that exist and are type-compatible — invalid free-text
   * ids are no longer possible.
   */
  useEffect(() => {
    if (!showReassign || !job) return;
    let cancelled = false;
    const compatibleType = JOB_TYPE_TO_ASSET_TYPE[job.job_type] ?? "vehicle";
    setAssetsLoading(true);
    setAssetsError("");
    apiService
      .getAssets({ asset_type: compatibleType })
      .then((res) => {
        if (cancelled) return;
        // Defensive client-side filter in case the backend ignores the param.
        const compatible = (res.data ?? []).filter((a) => {
          const t = readAssetType(a);
          return t === undefined || t === compatibleType;
        });
        setAssetOptions(compatible);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setAssetsError(
          err instanceof Error ? err.message : "Failed to load assets",
        );
        setAssetOptions([]);
      })
      .finally(() => {
        if (!cancelled) setAssetsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [showReassign, job]);

  /**
   * Handle status transition and update local job state.
   */
  const handleTransition = useCallback(
    async (id: string, targetStatus: JobStatus, failureReason?: string) => {
      setTransitionError("");
      try {
        // Call API directly so we can catch errors for inline display
        const _res = await transitionStatus(id, {
          status: targetStatus,
          failure_reason: failureReason,
        });
        // Also notify parent to update its job list
        onTransition(id, targetStatus, failureReason).catch(() => {});
        // Re-fetch to get updated job data and events
        try {
          const jobRes = await getJob(jobId, {
            expand: ["order", "customer", "asset", "driver"],
          });
          const jobData = jobRes.data as any;
          if (jobData?.job || jobData?.status) {
            applyJobResponse(jobData);
          }
        } catch {
          // Re-fetch failed — use the transition response as fallback
        }
      } catch (err) {
        setTransitionError(
          err instanceof Error
            ? err.message
            : "Failed to transition job status",
        );
      }
    },
    [onTransition, jobId, applyJobResponse],
  );

  const handleReassign = useCallback(async () => {
    if (!reassignAssetId.trim()) return;
    setReassigning(true);
    try {
      await reassignAsset(jobId, reassignAssetId.trim());
      setShowReassign(false);
      setReassignAssetId("");
      loadData(); // Refresh job data
    } catch (err) {
      setTransitionError(
        err instanceof Error ? err.message : "Failed to reassign asset",
      );
    } finally {
      setReassigning(false);
    }
  }, [reassignAssetId, jobId, loadData]);

  // ── Loading state ──────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="h-full flex flex-col bg-white">
        <div className="border-b border-gray-100 px-8 py-4">
          <button
            onClick={onBack}
            className="flex items-center gap-2 text-sm text-gray-600 hover:text-primary transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            Back to Jobs
          </button>
        </div>
        <LoadingSpinner message="Loading job details..." />
      </div>
    );
  }

  // ── Error state ────────────────────────────────────────────────────────

  if (error || !job) {
    return (
      <div className="h-full flex flex-col bg-white">
        <div className="border-b border-gray-100 px-8 py-4">
          <button
            onClick={onBack}
            className="flex items-center gap-2 text-sm text-gray-600 hover:text-primary transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            Back to Jobs
          </button>
        </div>
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center">
            <AlertTriangle className="w-10 h-10 text-error mx-auto mb-3" />
            <p className="text-sm text-error mb-4">
              {error || "Job not found"}
            </p>
            <button
              onClick={loadData}
              className="px-4 py-2 text-sm text-white rounded-lg transition-colors bg-primary hover:bg-primary-hover"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-100 px-8 py-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <button
              onClick={onBack}
              className="flex items-center gap-2 text-sm text-gray-600 hover:text-primary transition-colors"
              aria-label="Back to Jobs"
            >
              <ArrowLeft className="w-4 h-4" />
            </button>
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-semibold text-primary">
                  {job.job_id}
                </h1>
                <span
                  className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(job.status, job.delayed)}`}
                >
                  {job.delayed
                    ? "Delayed"
                    : (job.status ?? "unknown").replace(/_/g, " ")}
                </span>
                <span
                  className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getPriorityBadge(job.priority)}`}
                >
                  {job.priority ?? "normal"}
                </span>
              </div>
              <p className="text-sm text-gray-500 mt-1">
                {formatJobType(job.job_type)} · {job.origin} → {job.destination}
              </p>
            </div>
          </div>

          {/* Status transition actions */}
          <div className="flex items-center gap-2">
            {(job.status === "assigned" || job.status === "in_progress") && (
              <button
                onClick={() => setShowReassign(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50 transition-colors"
              >
                <Repeat className="w-3.5 h-3.5" />
                Reassign
              </button>
            )}
            <JobActionButtons
              jobId={job.job_id}
              currentStatus={job.status}
              onTransition={handleTransition}
            />
          </div>
        </div>
      </div>

      {/* Reassign form — asset picker backed by /fleet/assets (Req 3.3) */}
      {showReassign && (
        <div className="mx-8 mt-2 bg-gray-50 px-4 py-3 rounded-lg border border-gray-200">
          <div className="flex items-center gap-3">
            <select
              value={reassignAssetId}
              onChange={(e) => setReassignAssetId(e.target.value)}
              disabled={assetsLoading}
              aria-label="Select replacement asset"
              className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg bg-white focus:ring-2 focus:ring-gray-200 disabled:opacity-50"
            >
              <option value="">
                {assetsLoading
                  ? "Loading assets…"
                  : assetOptions.length === 0
                    ? "No compatible assets available"
                    : "Select a compatible asset…"}
              </option>
              {assetOptions.map((asset) => (
                <option key={asset.id} value={asset.id}>
                  {asset.name ? `${asset.name} (${asset.id})` : asset.id}
                  {asset.status ? ` — ${asset.status}` : ""}
                </option>
              ))}
            </select>
            <button
              onClick={handleReassign}
              disabled={reassigning || !reassignAssetId.trim()}
              className="px-3 py-1.5 text-xs font-medium text-white rounded-lg disabled:opacity-50 bg-primary hover:bg-primary-hover"
            >
              {reassigning ? "Reassigning..." : "Confirm"}
            </button>
            <button
              onClick={() => {
                setShowReassign(false);
                setReassignAssetId("");
              }}
              className="px-3 py-1.5 text-xs text-gray-500 hover:text-gray-700"
            >
              Cancel
            </button>
          </div>
          <p className="mt-2 text-[11px] text-gray-500">
            Only {JOB_TYPE_TO_ASSET_TYPE[job.job_type] ?? "vehicle"}-type assets
            are shown — they are validated to exist and be compatible with this{" "}
            {formatJobType(job.job_type)} job.
          </p>
          {assetsError && (
            <p className="mt-1 text-[11px] text-error">{assetsError}</p>
          )}
        </div>
      )}

      {/* Transition error */}
      {transitionError && (
        <div className="mx-8 mt-2 mb-0">
          <p className="text-sm text-error bg-error-light px-4 py-3 rounded-lg">
            {transitionError}
          </p>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        <div className="px-8 py-6 space-y-6">
          {/* Job Details Card */}
          <div className="bg-white border border-gray-100 rounded-xl overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-100">
              <h2 className="text-sm font-medium text-gray-600 uppercase tracking-wider">
                Job Details
              </h2>
            </div>
            <div className="px-6 py-4 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-1">
              <DetailField
                icon={<Hash className="w-4 h-4" />}
                label="Job ID"
                value={job.job_id}
              />
              <DetailField
                icon={<Package className="w-4 h-4" />}
                label="Type"
                value={formatJobType(job.job_type)}
              />
              <DetailField
                icon={<Activity className="w-4 h-4" />}
                label="Status"
                value={
                  <span
                    className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(job.status, job.delayed)}`}
                  >
                    {job.delayed
                      ? "Delayed"
                      : (job.status ?? "unknown").replace(/_/g, " ")}
                  </span>
                }
              />
              <DetailField
                icon={<User className="w-4 h-4" />}
                label="Tenant"
                value={job.tenant_id}
              />
              <DetailField
                icon={<Truck className="w-4 h-4" />}
                label="Asset Assigned"
                value={job.asset_assigned}
              />
              <DetailField
                icon={<Flag className="w-4 h-4" />}
                label="Priority"
                value={
                  <span
                    className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getPriorityBadge(job.priority)}`}
                  >
                    {job.priority}
                  </span>
                }
              />
              <DetailField
                icon={<MapPin className="w-4 h-4" />}
                label="Origin"
                value={job.origin}
              />
              <DetailField
                icon={<MapPin className="w-4 h-4" />}
                label="Destination"
                value={job.destination}
              />
              <DetailField
                icon={<Calendar className="w-4 h-4" />}
                label="Scheduled Time"
                value={formatDateTime(job.scheduled_time)}
              />
              <DetailField
                icon={<Clock className="w-4 h-4" />}
                label="Estimated Arrival"
                value={formatDateTime(job.estimated_arrival)}
              />
              {eta && (
                <DetailField
                  icon={<Clock className="w-4 h-4" />}
                  label="Live ETA"
                  value={`${eta.eta_minutes} min (${new Date(eta.estimated_arrival).toLocaleTimeString()})`}
                />
              )}
              <DetailField
                icon={<Clock className="w-4 h-4" />}
                label="Started At"
                value={formatDateTime(job.started_at)}
              />
              <DetailField
                icon={<Clock className="w-4 h-4" />}
                label="Completed At"
                value={formatDateTime(job.completed_at)}
              />
              <DetailField
                icon={<Calendar className="w-4 h-4" />}
                label="Created At"
                value={formatDateTime(job.created_at)}
              />
              <DetailField
                icon={<Calendar className="w-4 h-4" />}
                label="Updated At"
                value={formatDateTime(job.updated_at)}
              />
              {job.delayed && (
                <DetailField
                  icon={<Timer className="w-4 h-4" />}
                  label="Delay Duration"
                  value={
                    job.delay_duration_minutes
                      ? `${job.delay_duration_minutes} minutes`
                      : "Delayed"
                  }
                />
              )}
              {job.failure_reason && (
                <DetailField
                  icon={<AlertTriangle className="w-4 h-4" />}
                  label="Failure Reason"
                  value={
                    <span className="text-error">{job.failure_reason}</span>
                  }
                />
              )}
              {job.notes && (
                <DetailField
                  icon={<FileText className="w-4 h-4" />}
                  label="Notes"
                  value={job.notes}
                />
              )}
            </div>
          </div>

          {/* Linked Records Card (cross-module-entity-linkage Req 3.3, 13.1) */}
          <div className="bg-white border border-gray-100 rounded-xl overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-100">
              <h2 className="text-sm font-medium text-gray-600 uppercase tracking-wider">
                Linked Records
              </h2>
            </div>
            <div className="px-6 py-4 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-1">
              <LinkedRefField
                icon={<FileText className="w-4 h-4" />}
                label="Order"
                link={links.order}
                fallbackId={job.order_id}
                href={(id) => `/orders/${encodeURIComponent(id)}`}
              />
              <LinkedRefField
                icon={<User className="w-4 h-4" />}
                label="Customer"
                link={links.customer}
                fallbackId={job.customer_id}
                href={(id) => `/commerce/customers/${encodeURIComponent(id)}`}
              />
              <LinkedRefField
                icon={<Truck className="w-4 h-4" />}
                label="Asset"
                link={links.asset}
                fallbackId={job.asset_assigned}
                href={(id) => `/ops/tracking/${encodeURIComponent(id)}`}
              />
              <LinkedRefField
                icon={<User className="w-4 h-4" />}
                label="Driver"
                link={links.driver}
                fallbackId={job.driver_id}
                href={(id) => `/ops/drivers?driver=${encodeURIComponent(id)}`}
              />
            </div>
          </div>

          {/* Event Timeline Card */}
          <div className="bg-white border border-gray-100 rounded-xl overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-100">
              <h2 className="text-sm font-medium text-gray-600 uppercase tracking-wider">
                Event Timeline
              </h2>
            </div>
            <div className="px-6 py-4">
              <EventTimeline events={job.events ?? []} />
            </div>
          </div>

          {/* Cargo Manifest Card */}
          {(job.job_type === "cargo_transport" || cargo.length > 0) && (
            <div className="bg-white border border-gray-100 rounded-xl overflow-hidden">
              <CargoManifestEditor
                jobId={job.job_id}
                items={cargo}
                onItemsChange={setCargo}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
