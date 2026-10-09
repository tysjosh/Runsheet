"use client";

/**
 * Compliance → IFTA (UI revamp task 3.5): the summary cards are title-row
 * counts, the quarter picker is the toolbar, incomplete trucks are one
 * actionable banner, and the manual mileage adjustment is an md FormDialog
 * (design.md §5) instead of an always-open form below the report.
 */
import { Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  EntityLink,
  ExportCsvButton,
  Field,
  FormDialog,
  INPUT_CLASS,
  InlineBanner,
  NumberField,
  Select,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { money, number } from "../../lib/format";
import {
  type CreateMileageAdjustmentPayload,
  createMileageAdjustment,
  getIFTAReport,
  type IFTAJurisdictionEntry,
  type IFTAReport,
  type IFTAReportFilters,
  type IFTATruckSummary,
} from "../../services/complianceApi";
import AssetPicker from "../ops/AssetPicker";
import { PageTitle } from "../ui/PageHeader";
import { notify } from "../ui/toast/notify";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getCurrentQuarter(): string {
  const now = new Date();
  const q = Math.ceil((now.getMonth() + 1) / 3);
  return `${now.getFullYear()}-Q${q}`;
}

function isValidQuarter(value: string): boolean {
  return /^\d{4}-Q[1-4]$/.test(value);
}

function formatNumber(value: number | null | undefined, decimals = 1): string {
  return number(value, { decimals });
}

function formatCurrency(cents: number | null | undefined): string {
  if (cents == null || Number.isNaN(cents)) return "—";
  return money(cents / 100);
}

/** Quarters from last year to next year ("2026-Q4"). */
function getQuarterOptions(): string[] {
  const currentYear = new Date().getFullYear();
  const options: string[] = [];
  for (let year = currentYear - 1; year <= currentYear + 1; year++) {
    for (let q = 1; q <= 4; q++) options.push(`${year}-Q${q}`);
  }
  return options;
}

type AdjustmentValues = {
  truck_id: string;
  jurisdiction: string;
  miles: number | null;
  quarter: string;
  reason: string;
};

export function validateAdjustment(v: AdjustmentValues) {
  const errors: Record<string, string | undefined> = {};
  if (!v.truck_id.trim()) errors.truck_id = "Pick a truck.";
  if (!/^[A-Za-z]{2}$/.test(v.jurisdiction.trim()))
    errors.jurisdiction = "Enter a two-letter state, e.g. TX.";
  if (v.miles == null || v.miles === 0)
    errors.miles = "Miles must be non-zero.";
  if (!isValidQuarter(v.quarter))
    errors.quarter = "Quarter must be in YYYY-Q[1-4] format.";
  if (!v.reason.trim()) errors.reason = "Reason is required for audit trail.";
  return errors;
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function IFTAReportPage() {
  const [quarter, setQuarter] = useState(getCurrentQuarter());
  const [report, setReport] = useState<IFTAReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedTruck, setExpandedTruck] = useState<string | null>(null);
  const [adjusting, setAdjusting] = useState<string | null>(null);

  const fetchReport = useCallback(async (q: string) => {
    if (!isValidQuarter(q)) return;
    setLoading(true);
    setError(null);
    try {
      const filters: IFTAReportFilters = { quarter: q };
      const response = await getIFTAReport(filters);
      setReport(response.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load IFTA report",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchReport(quarter);
  }, [fetchReport, quarter]);

  const actions = useMemo(
    () => (
      <>
        <ExportCsvButton
          type="ifta"
          params={{ quarter }}
          subject="IFTA report"
          allowedRoles={["admin", "dispatcher"]}
        />
        <Button
          size="sm"
          icon={<Plus className="h-3.5 w-3.5" />}
          onClick={() => setAdjusting("")}
        >
          Record adjustment
        </Button>
      </>
    ),
    [quarter],
  );
  // Summary cards → title-row counts (design.md §6 rule 3).
  const counts = useMemo(
    () =>
      report ? (
        <span className="flex items-center gap-1 whitespace-nowrap">
          <span>Fleet MPG</span>{" "}
          <span className="font-semibold text-text">
            {formatNumber(report.fleet_mpg, 2)}
          </span>
          <span aria-hidden="true">·</span>
          <span>{number(report.trucks.length)} trucks</span>
          <span aria-hidden="true">·</span>
          <span>{number(report.incomplete_trucks.length)} incomplete</span>
        </span>
      ) : null,
    [report],
  );
  const embedded = usePageChrome({ actions, counts });

  // Per-truck summary columns. The row menu toggles the jurisdiction
  // breakdown rendered via the table's renderExpanded hook.
  const truckColumns: Column<IFTATruckSummary>[] = [
    {
      key: "truck_id",
      header: "Truck",
      width: 160,
      // The per-truck IFTA subject is navigable to the Fleet module as a
      // canonical asset (Req 11.3, 13.1).
      cell: (t) => (
        <EntityLink
          type="asset"
          id={t.truck_id}
          className="font-medium"
          stopPropagation
        />
      ),
    },
    {
      key: "truck_name",
      header: "Name",
      truncate: true,
      cell: (t) => t.truck_name,
    },
    {
      key: "total_miles",
      header: "Miles",
      align: "right",
      width: 120,
      className: "tabular-nums",
      cell: (t) => formatNumber(t.total_miles),
    },
    {
      key: "total_gallons",
      header: "Gallons",
      align: "right",
      width: 120,
      className: "tabular-nums",
      cell: (t) => formatNumber(t.total_gallons),
    },
    {
      key: "fleet_mpg",
      header: "MPG",
      align: "right",
      width: 90,
      className: "tabular-nums",
      cell: (t) => formatNumber(t.fleet_mpg, 2),
    },
    {
      key: "jurisdiction_count",
      header: "Jurisdictions",
      align: "right",
      width: 120,
      className: "tabular-nums",
      cell: (t) => number(t.jurisdictions.length),
    },
  ];

  const jurisdictionColumns: Column<IFTAJurisdictionEntry>[] = [
    {
      key: "jurisdiction",
      header: "Jurisdiction",
      className: "font-medium",
      cell: (j) => j.jurisdiction,
    },
    ...(
      [
        ["total_miles", "Miles"],
        ["taxable_miles", "Taxable miles"],
        ["tax_paid_gallons", "Tax-paid gal"],
        ["net_taxable_gallons", "Net taxable gal"],
      ] as const
    ).map(([key, header]) => ({
      key,
      header,
      align: "right" as const,
      className: "tabular-nums",
      cell: (j: IFTAJurisdictionEntry) => formatNumber(j[key]),
    })),
    {
      key: "tax_rate",
      header: "Rate",
      align: "right",
      className: "tabular-nums",
      cell: (j) => formatCurrency(j.tax_rate),
    },
    {
      key: "tax_due",
      header: "Tax due",
      align: "right",
      className: "tabular-nums",
      cell: (j) => formatCurrency(j.tax_due),
    },
  ];

  const submitAdjustment = async (v: AdjustmentValues) => {
    await createMileageAdjustment({
      truck_id: v.truck_id,
      jurisdiction: v.jurisdiction.trim().toUpperCase(),
      miles: v.miles ?? 0,
      quarter: v.quarter,
      reason: v.reason.trim(),
    } satisfies CreateMileageAdjustmentPayload);
    return v;
  };

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center gap-3 border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            IFTA Quarterly Report
          </PageTitle>
          <span className="text-xs text-text-muted">{counts}</span>
          <div className="ml-auto flex gap-2">{actions}</div>
        </div>
      )}
      <Toolbar
        label="IFTA"
        filters={
          <div className="w-36">
            <Select
              id="quarter-select"
              aria-label="Quarter"
              value={quarter}
              onChange={setQuarter}
              options={getQuarterOptions().map((q) => ({ value: q, label: q }))}
            />
          </div>
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        {!loading &&
          !error &&
          report &&
          report.incomplete_trucks.length > 0 && (
            <div className="px-4 pt-3">
              <InlineBanner
                tone="warning"
                action={
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() =>
                      setAdjusting(report.incomplete_trucks[0]?.truck_id ?? "")
                    }
                  >
                    Record adjustment
                  </Button>
                }
              >
                <span className="flex flex-wrap items-center gap-x-2">
                  Incomplete Geotab data for {quarter}:
                  {report.incomplete_trucks.map((flag) => (
                    <span key={flag.truck_id} title={flag.reason}>
                      <EntityLink type="asset" id={flag.truck_id} />
                    </span>
                  ))}
                </span>
              </InlineBanner>
            </div>
          )}
        <DataTable<IFTATruckSummary>
          ariaLabel="Per-truck IFTA summary"
          columns={truckColumns}
          data={loading || error || !report ? [] : report.trucks}
          loading={loading}
          error={
            error
              ? { message: error, onRetry: () => fetchReport(quarter) }
              : null
          }
          getRowId={(truck) => truck.truck_id}
          rowLabel={(truck) => `Truck ${truck.truck_id}`}
          onRowClick={(t) =>
            setExpandedTruck((prev) =>
              prev === t.truck_id ? null : t.truck_id,
            )
          }
          rowMenu={(t) => [
            {
              id: "details",
              label:
                expandedTruck === t.truck_id
                  ? "Hide jurisdictions"
                  : "Show jurisdictions",
              onSelect: () =>
                setExpandedTruck((prev) =>
                  prev === t.truck_id ? null : t.truck_id,
                ),
            },
            {
              id: "adjust",
              label: "Record adjustment",
              onSelect: () => setAdjusting(t.truck_id),
            },
          ]}
          emptyState={
            <span className="text-text-muted">
              No truck data available for {quarter}.
            </span>
          }
          renderExpanded={(truck) =>
            expandedTruck === truck.truck_id ? (
              <div className="border-b border-slate-200 bg-slate-50 p-3">
                <DataTable<IFTAJurisdictionEntry>
                  rowHeight="compact"
                  ariaLabel={`Jurisdiction breakdown for ${truck.truck_id}`}
                  columns={jurisdictionColumns}
                  data={truck.jurisdictions}
                  getRowId={(j) => j.jurisdiction}
                  emptyState={
                    <span className="text-text-muted">
                      No jurisdiction data.
                    </span>
                  }
                />
              </div>
            ) : null
          }
        />
      </div>

      {adjusting !== null && (
        <FormDialog<AdjustmentValues, AdjustmentValues>
          open
          size="md"
          title="Record mileage adjustment"
          help="Positive miles add, negative miles subtract. Every adjustment is logged for audit."
          submitLabel="Record adjustment"
          successMessage={null}
          initialValues={{
            truck_id: adjusting,
            jurisdiction: "",
            miles: null,
            quarter,
            reason: "",
          }}
          validate={validateAdjustment}
          onSubmit={submitAdjustment}
          onSaved={(v) => {
            notifyAdjustment(v);
            if (v.quarter === quarter) void fetchReport(quarter);
          }}
          onClose={() => setAdjusting(null)}
        >
          {({ values, set, errors }) => (
            <>
              <Field label="Truck ID" required span={1} error={errors.truck_id}>
                <AssetPicker
                  id="adj-truck-id"
                  assetType="vehicle"
                  aria-label="Truck ID"
                  value={values.truck_id || null}
                  onChange={(value) => set("truck_id", value)}
                />
              </Field>
              <Field
                label="Jurisdiction (state)"
                required
                span={1}
                error={errors.jurisdiction}
              >
                <input
                  id="adj-jurisdiction"
                  type="text"
                  value={values.jurisdiction}
                  onChange={(e) => set("jurisdiction", e.target.value)}
                  placeholder="e.g. TX"
                  maxLength={2}
                  className={`${INPUT_CLASS} uppercase`}
                />
              </Field>
              <Field label="Miles (+/-)" required span={1} error={errors.miles}>
                <NumberField
                  id="adj-miles"
                  value={values.miles}
                  onChange={(n) => set("miles", n)}
                  unit="mi"
                  decimals={1}
                />
              </Field>
              <Field label="Quarter" span={1} error={errors.quarter}>
                <Select
                  id="adj-quarter"
                  value={values.quarter}
                  onChange={(v) => set("quarter", v)}
                  options={getQuarterOptions().map((q) => ({
                    value: q,
                    label: q,
                  }))}
                />
              </Field>
              <Field label="Reason" required error={errors.reason}>
                <textarea
                  id="adj-reason"
                  value={values.reason}
                  onChange={(e) => set("reason", e.target.value)}
                  placeholder="Why this adjustment is needed (kept for audit)"
                  rows={3}
                  className={`${INPUT_CLASS} h-auto py-1.5`}
                />
              </Field>
            </>
          )}
        </FormDialog>
      )}
    </div>
  );
}

function notifyAdjustment(v: AdjustmentValues) {
  const miles = v.miles ?? 0;
  notify({
    type: "success",
    message: `Adjustment recorded: ${miles > 0 ? "+" : ""}${number(miles, {
      decimals: Number.isInteger(miles) ? 0 : 1,
    })} miles for ${v.truck_id} in ${v.jurisdiction.trim().toUpperCase()}.`,
  });
}
