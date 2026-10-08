"use client";

import { useCallback, useEffect, useState } from "react";
import {
  type Column,
  DataTable,
  EntityLink,
  FilterChips,
  FormDialog,
  StatusBadge,
  Toolbar,
} from "@/components/ui";
import { number, pct } from "../../lib/format";
import {
  approveKFactorAdjustment,
  getKFactorDashboard,
  type KFactorEntry,
} from "../../services/complianceApi";
import type { StatusKey } from "../../styles/tokens";
import TankConsumptionDrillIn from "./TankConsumptionDrillIn";

// ─── Derived status ──────────────────────────────────────────────────────────

type KFactorStatus = "ok" | "review_needed" | "insufficient_data";

/**
 * Derive a display status from the backend entry. The backend exposes
 * ``read_only`` (insufficient deliveries) and a ``suggested_kfactor``
 * (present only when variance exceeds the recalibration threshold).
 */
function deriveStatus(entry: KFactorEntry): KFactorStatus {
  if (entry.read_only) return "insufficient_data";
  if (entry.suggested_kfactor !== null) return "review_needed";
  return "ok";
}

// ─── Status display ──────────────────────────────────────────────────────────

/** Display status (hue + icon + label) for each derived k-factor status. */
const STATUS_DISPLAY: Record<
  KFactorStatus,
  { status: StatusKey; label: string }
> = {
  ok: { status: "ok", label: "OK" },
  review_needed: { status: "warning", label: "Review Needed" },
  insufficient_data: { status: "draft", label: "Insufficient Data" },
};

function formatPercent(value: number | null): string {
  if (value === null) return "—";
  return `${value >= 0 ? "+" : ""}${pct(value, { decimals: 1 })}`;
}

function formatKFactor(value: number | null): string {
  if (value === null) return "—";
  return number(value, { decimals: 4 });
}

// ─── Table columns ───────────────────────────────────────────────────────────

interface DecoratedKFactorEntry {
  entry: KFactorEntry;
  status: KFactorStatus;
}
function getKFactorColumns(
  onApprove: (entry: KFactorEntry) => void,
  onViewConsumption: (entry: KFactorEntry) => void,
): Column<DecoratedKFactorEntry>[] {
  return [
    {
      key: "tank_id",
      label: "Tank ID",
      className: "text-sm",
      // The k-factor subject is its tank, navigable to the Fuel Ops tank
      // surface (Req 11.3, 13.1).
      render: ({ entry }) => (
        <EntityLink type="tank" id={entry.tank_id} className="font-medium" />
      ),
    },
    {
      key: "customer_id",
      label: "Customer",
      className: "text-sm",
      // The owning customer is also navigable to the Commerce module.
      render: ({ entry }) => (
        <EntityLink type="customer" id={entry.customer_id} />
      ),
    },
    {
      key: "current_kfactor",
      label: "Current K-Factor",
      className: "font-mono text-sm",
      render: ({ entry }) => formatKFactor(entry.current_kfactor),
    },
    {
      key: "suggested_kfactor",
      label: "Suggested K-Factor",
      className: "font-mono text-sm",
      render: ({ entry }) => formatKFactor(entry.suggested_kfactor),
    },
    {
      key: "variance_percent",
      label: "Cumulative Variance",
      className: "text-sm",
      render: ({ entry }) => {
        const variance = entry.variance_percent;
        return (
          <span
            className={
              variance !== null && Math.abs(variance) > 15
                ? "font-semibold text-red-800"
                : "text-slate-700"
            }
          >
            {formatPercent(variance)}
          </span>
        );
      },
    },
    {
      key: "status",
      label: "Status",
      render: ({ status }) => (
        <StatusBadge
          status={STATUS_DISPLAY[status].status}
          label={STATUS_DISPLAY[status].label}
        />
      ),
    },
    {
      key: "actions",
      label: "Actions",
      render: ({ entry, status }) => (
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onViewConsumption(entry)}
            className="text-sm font-medium text-link underline hover:opacity-80"
          >
            Consumption
          </button>
          {status === "review_needed" && entry.suggested_kfactor !== null ? (
            <button
              type="button"
              onClick={() => onApprove(entry)}
              className="h-7 rounded-lg bg-primary px-3 text-xs font-semibold text-white hover:bg-primary-hover"
            >
              Approve
            </button>
          ) : null}
        </div>
      ),
    },
  ];
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function KFactorCalibrationPage() {
  const [entries, setEntries] = useState<KFactorEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Approval dialog state
  const [approvalTarget, setApprovalTarget] = useState<KFactorEntry | null>(
    null,
  );
  const [chip, setChip] = useState<"all" | KFactorStatus>("all");

  // Per-tank consumption drill-in state
  const [consumptionTarget, setConsumptionTarget] =
    useState<KFactorEntry | null>(null);

  // ─── Fetch dashboard ───────────────────────────────────────────────────────

  const fetchDashboard = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getKFactorDashboard();
      setEntries(response.data ?? []);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to load K-factor dashboard",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDashboard();
  }, [fetchDashboard]);

  // ─── Approval workflow ─────────────────────────────────────────────────────

  async function confirmApproval(target: KFactorEntry) {
    if (target.suggested_kfactor === null) return;
    await approveKFactorAdjustment(target.tank_id, {
      new_kfactor: target.suggested_kfactor,
      operator_id: "current_user", // In production, this would come from auth context
    });
  }

  // ─── Derive view model from the flat entry list ────────────────────────────

  // Annotate each entry with its derived status, then sort by absolute
  // variance (highest first; null variance sorts last).
  const decoratedEntries = entries.map((entry) => ({
    entry,
    status: deriveStatus(entry),
  }));

  const sortedEntries = [...decoratedEntries].sort(
    (a, b) =>
      Math.abs(b.entry.variance_percent ?? 0) -
      Math.abs(a.entry.variance_percent ?? 0),
  );

  const totalReviewNeeded = decoratedEntries.filter(
    (d) => d.status === "review_needed",
  ).length;
  const totalInsufficientData = decoratedEntries.filter(
    (d) => d.status === "insufficient_data",
  ).length;

  const visible =
    chip === "all"
      ? sortedEntries
      : sortedEntries.filter((d) => d.status === chip);

  // ─── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex h-full flex-col bg-surface">
      <Toolbar
        label="K-factor"
        filters={
          <FilterChips
            label="K-factor status"
            options={[
              {
                id: "all",
                label: "All",
                count: loading ? undefined : entries.length,
              },
              {
                id: "review_needed",
                label: "Review Needed",
                count: loading ? undefined : totalReviewNeeded,
                status: "warning",
              },
              {
                id: "insufficient_data",
                label: "Insufficient Data",
                count: loading ? undefined : totalInsufficientData,
                status: "draft",
              },
              {
                id: "ok",
                label: "OK",
                count: loading
                  ? undefined
                  : entries.length - totalReviewNeeded - totalInsufficientData,
                status: "ok",
              },
            ]}
            value={chip}
            onChange={(v) => setChip(v as "all" | KFactorStatus)}
            collapse
          />
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<DecoratedKFactorEntry>
          ariaLabel="K-factor calibration dashboard"
          columns={getKFactorColumns(setApprovalTarget, setConsumptionTarget)}
          data={visible}
          loading={loading}
          error={error ? { message: error, onRetry: fetchDashboard } : null}
          getRowId={({ entry }) => entry.tank_id}
          emptyState={
            <span className="text-sm text-text-muted">
              No K-factor calibration data available.
            </span>
          }
        />
      </div>

      {/* Approval confirmation (design.md §5 "K-factor calibration": md) */}
      {approvalTarget && (
        <FormDialog
          open
          size="md"
          title="Confirm K-factor adjustment"
          help="This updates the tank's K-factor and notifies the tank forecasting agent. The change is logged for audit."
          submitLabel="Confirm approval"
          successMessage="K-factor updated"
          initialValues={{}}
          onSubmit={() => confirmApproval(approvalTarget)}
          onSaved={() => void fetchDashboard()}
          onClose={() => setApprovalTarget(null)}
        >
          {() => (
            <dl className="col-span-2 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <dt className="text-text-muted">Tank</dt>
              <dd className="font-medium">
                <EntityLink type="tank" id={approvalTarget.tank_id} />
              </dd>
              <dt className="text-text-muted">Customer</dt>
              <dd className="font-medium">
                <EntityLink type="customer" id={approvalTarget.customer_id} />
              </dd>
              <dt className="text-text-muted">Current K-factor</dt>
              <dd className="tabular-nums">
                {formatKFactor(approvalTarget.current_kfactor)}
              </dd>
              <dt className="text-text-muted">New K-factor</dt>
              <dd className="font-semibold tabular-nums text-brand-800">
                {formatKFactor(approvalTarget.suggested_kfactor)}
              </dd>
              <dt className="text-text-muted">Cumulative variance</dt>
              <dd className="tabular-nums">
                {formatPercent(approvalTarget.variance_percent)}
              </dd>
            </dl>
          )}
        </FormDialog>
      )}
      {/* Per-tank consumption / forecast drill-in */}
      {consumptionTarget && (
        <TankConsumptionDrillIn
          entry={consumptionTarget}
          onClose={() => setConsumptionTarget(null)}
        />
      )}
    </div>
  );
}
