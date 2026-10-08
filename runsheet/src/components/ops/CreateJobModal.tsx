"use client";

import { useCallback, useState } from "react";
import {
  type AssetReadinessIndicator,
  getAssetReadiness,
  type ReadinessStatus,
} from "../../services/inventoryApi";
import { createJob } from "../../services/schedulingApi";
import type { AssetType, Job, JobType, Priority } from "../../types/api";
import { Field, FormDialog, INPUT_CLASS, Select } from "../ui";
import { notify } from "../ui/toast/notify";
import AssetPicker from "./AssetPicker";

const JOB_TYPES: { value: JobType; label: string }[] = [
  { value: "cargo_transport", label: "Cargo transport" },
  { value: "passenger_transport", label: "Passenger transport" },
  { value: "vessel_movement", label: "Vessel movement" },
  { value: "airport_transfer", label: "Airport transfer" },
  { value: "crane_booking", label: "Crane booking" },
];

const PRIORITIES: { value: Priority; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "normal", label: "Normal" },
  { value: "high", label: "High" },
  { value: "urgent", label: "Urgent" },
];

// Job type → required asset type. Mirrors the backend compatibility rule
// (see Agents/overlay/dispatch_optimizer.py and delay_response_agent.py) so
// the asset picker only offers assets the backend will accept. The backend
// still re-validates on submit.
const JOB_TYPE_TO_ASSET_TYPE: Record<JobType, AssetType> = {
  cargo_transport: "vehicle",
  passenger_transport: "vehicle",
  vessel_movement: "vessel",
  airport_transfer: "vehicle",
  crane_booking: "equipment",
};

// ─── Readiness Indicator Component ──────────────────────────────────────────

interface ReadinessIndicatorProps {
  status: ReadinessStatus;
  missingParts: { name: string }[];
  lowParts: { name: string }[];
}

function ReadinessIndicator({
  status,
  missingParts,
  lowParts,
}: ReadinessIndicatorProps) {
  const colorMap: Record<ReadinessStatus, string> = {
    ready: "bg-success",
    warning: "bg-warning",
    critical: "bg-error",
    blocked: "bg-error",
  };

  const labelMap: Record<ReadinessStatus, string> = {
    ready: "All parts in stock",
    warning: "Some parts low stock",
    critical: "Critical parts out of stock",
    blocked: "Assignment blocked — parts unavailable",
  };

  const tooltipParts: string[] = [];
  if (missingParts.length > 0) {
    tooltipParts.push(`Missing: ${missingParts.map((p) => p.name).join(", ")}`);
  }
  if (lowParts.length > 0) {
    tooltipParts.push(`Low: ${lowParts.map((p) => p.name).join(", ")}`);
  }
  const tooltipText =
    tooltipParts.length > 0 ? tooltipParts.join(" | ") : labelMap[status];

  return (
    <span
      className="relative inline-flex items-center group"
      aria-label={`Asset readiness: ${labelMap[status]}`}
    >
      <span
        className={`inline-block w-2.5 h-2.5 rounded-full ${colorMap[status]}`}
        aria-hidden="true"
      />
      {/* Tooltip */}
      <span
        className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 px-2 py-1 text-[10px] text-white bg-gray-800 rounded whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50"
        role="tooltip"
      >
        {tooltipText}
      </span>
    </span>
  );
}

// ─── Main Component ─────────────────────────────────────────────────────────

interface CreateJobModalProps {
  onClose: () => void;
  onCreated: (job: Job) => void;
}

type JobValues = {
  job_type: JobType;
  origin: string;
  destination: string;
  scheduled_time: string;
  asset_assigned: string;
  priority: Priority;
  notes: string;
};

const INITIAL: JobValues = {
  job_type: "cargo_transport",
  origin: "",
  destination: "",
  scheduled_time: "",
  asset_assigned: "",
  priority: "normal",
  notes: "",
};

export function validateJob(v: JobValues) {
  const e: Record<string, string | undefined> = {};
  if (!v.origin.trim()) e.origin = "Enter the origin.";
  if (!v.destination.trim()) e.destination = "Enter the destination.";
  if (!v.scheduled_time) e.scheduled_time = "Choose the scheduled time.";
  return e;
}

/** "Assignment risk at X: Out of stock: … | Low stock: …", or null. */
export function riskMessage(data: unknown): string | null {
  const flags = (data as any)?.readiness_flags;
  if (!flags) return null;
  const names = (xs: { name: string }[] | undefined) =>
    (xs ?? []).map((p) => p.name).join(", ");
  const parts: string[] = [];
  if (flags.missing_parts?.length)
    parts.push(`Out of stock: ${names(flags.missing_parts)}`);
  if (flags.low_parts?.length)
    parts.push(`Low stock: ${names(flags.low_parts)}`);
  if (parts.length === 0) return null;
  const location = flags.depot_location || flags.location || "";
  return `Assignment risk${location ? ` at ${location}` : ""}: ${parts.join(" | ")}`;
}

/**
 * Create job (UI revamp, design.md §5 "Job create": md FormDialog, shared
 * toast). The private bottom-right toast is gone: a parts-risk warning now
 * goes through the app-wide toaster, so it outlives the closed dialog (the
 * old one unmounted with the modal).
 */
export default function CreateJobModal({
  onClose,
  onCreated,
}: CreateJobModalProps) {
  const [readinessMap, setReadinessMap] = useState<
    Record<string, AssetReadinessIndicator>
  >({});

  // Readiness for the assets the picker loaded for the current job type.
  const fetchReadiness = useCallback(async (assetIds: string[]) => {
    if (assetIds.length === 0) {
      setReadinessMap({});
      return;
    }
    const results: Record<string, AssetReadinessIndicator> = {};
    const settled = await Promise.allSettled(
      assetIds.map((assetId) => getAssetReadiness(assetId)),
    );
    settled.forEach((result, index) => {
      // Fail-open: a failed read shows no indicator for that asset.
      if (result.status === "fulfilled")
        results[assetIds[index]] = result.value.data;
    });
    setReadinessMap(results);
  }, []);

  const submit = async (v: JobValues): Promise<Job> => {
    const res = await createJob({
      job_type: v.job_type,
      origin: v.origin.trim(),
      destination: v.destination.trim(),
      scheduled_time: new Date(v.scheduled_time).toISOString(),
      asset_assigned: v.asset_assigned || undefined,
      priority: v.priority,
      notes: v.notes.trim() || undefined,
    });
    // Requirement 7.2: surface parts risk on the assigned asset.
    const risk = riskMessage(res.data);
    if (risk) notify({ type: "warning", message: risk, durationMs: 8000 });
    return res.data;
  };

  return (
    <FormDialog<JobValues, Job>
      open
      size="md"
      title="Create job"
      submitLabel="Create job"
      successMessage="Job created"
      initialValues={INITIAL}
      validate={validateJob}
      onSubmit={submit}
      onSaved={onCreated}
      onClose={onClose}
    >
      {({ values, set, setValues, errors }) => {
        const readiness = values.asset_assigned
          ? readinessMap[values.asset_assigned]
          : undefined;
        return (
          <>
            <Field label="Job type" span={1} id="job-type">
              <Select
                id="job-type"
                value={values.job_type}
                onChange={(t) =>
                  setValues((prev) => ({
                    ...prev,
                    job_type: t as JobType,
                    asset_assigned: "",
                  }))
                }
                options={JOB_TYPES}
              />
            </Field>
            <Field label="Priority" span={1} id="job-priority">
              <Select
                id="job-priority"
                value={values.priority}
                onChange={(p) => set("priority", p as Priority)}
                options={PRIORITIES}
              />
            </Field>
            <Field label="Origin" required error={errors.origin}>
              <input
                id="job-origin"
                type="text"
                value={values.origin}
                onChange={(e) => set("origin", e.target.value)}
                placeholder="e.g. Houston Terminal"
                className={INPUT_CLASS}
              />
            </Field>
            <Field label="Destination" required error={errors.destination}>
              <input
                id="job-destination"
                type="text"
                value={values.destination}
                onChange={(e) => set("destination", e.target.value)}
                placeholder="e.g. Dallas Depot"
                className={INPUT_CLASS}
              />
            </Field>
            <Field
              label="Scheduled time"
              required
              error={errors.scheduled_time}
              span={1}
            >
              <input
                id="job-scheduled"
                type="datetime-local"
                value={values.scheduled_time}
                onChange={(e) => set("scheduled_time", e.target.value)}
                className={INPUT_CLASS}
              />
            </Field>
            <div className="col-span-1">
              <p className="mb-1 text-xs font-medium text-slate-700">Asset</p>
              <AssetPicker
                assetType={JOB_TYPE_TO_ASSET_TYPE[values.job_type]}
                value={values.asset_assigned || null}
                onChange={(assetId) => set("asset_assigned", assetId)}
                readinessByAsset={Object.fromEntries(
                  Object.entries(readinessMap).map(([id, r]) => [id, r.status]),
                )}
                onAssetsLoaded={fetchReadiness}
                aria-label="Asset"
              />
              {readiness && (
                <div className="mt-1.5 flex items-center gap-1.5">
                  <ReadinessIndicator
                    status={readiness.status}
                    missingParts={readiness.missing_parts}
                    lowParts={readiness.low_parts}
                  />
                  <span className="text-xs text-text-muted">
                    {readiness.status === "ready"
                      ? "Parts available"
                      : readiness.status === "warning"
                        ? "Low stock warning"
                        : "Critical shortage"}
                  </span>
                </div>
              )}
            </div>
            <Field label="Notes">
              <textarea
                id="job-notes"
                value={values.notes}
                onChange={(e) => set("notes", e.target.value)}
                placeholder="Any additional details"
                rows={2}
                className={`${INPUT_CLASS} resize-none`}
              />
            </Field>
          </>
        );
      }}
    </FormDialog>
  );
}
