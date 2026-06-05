"use client";

import type React from "react";
import { useCallback, useEffect, useState } from "react";
import { type Column, PageHeader, Table } from "@/components/ui";
import {
  type CreateMileageAdjustmentPayload,
  createMileageAdjustment,
  getIFTAReport,
  type IFTAJurisdictionEntry,
  type IFTAReport,
  type IFTAReportFilters,
  type IFTATruckSummary,
} from "../../services/complianceApi";

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
  if (value == null || Number.isNaN(value)) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function formatCurrency(cents: number | null | undefined): string {
  if (cents == null || Number.isNaN(cents)) return "—";
  return `$${(cents / 100).toFixed(2)}`;
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function IFTAReportPage() {
  // Report state
  const [quarter, setQuarter] = useState(getCurrentQuarter());
  const [report, setReport] = useState<IFTAReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Expanded truck detail
  const [expandedTruck, setExpandedTruck] = useState<string | null>(null);

  // Manual adjustment form state
  const [adjustmentForm, setAdjustmentForm] =
    useState<CreateMileageAdjustmentPayload>({
      truck_id: "",
      jurisdiction: "",
      miles: 0,
      quarter: getCurrentQuarter(),
      reason: "",
    });
  const [submittingAdjustment, setSubmittingAdjustment] = useState(false);
  const [adjustmentError, setAdjustmentError] = useState<string | null>(null);
  const [adjustmentSuccess, setAdjustmentSuccess] = useState<string | null>(
    null,
  );

  // ─── Fetch report ──────────────────────────────────────────────────────────

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

  // ─── Quarter selector handler ──────────────────────────────────────────────

  function handleQuarterChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const value = e.target.value;
    setQuarter(value);
    setAdjustmentForm((prev) => ({ ...prev, quarter: value }));
  }

  // ─── Generate quarter options (current year ± 1 year) ──────────────────────

  function getQuarterOptions(): string[] {
    const now = new Date();
    const currentYear = now.getFullYear();
    const options: string[] = [];
    for (let year = currentYear - 1; year <= currentYear + 1; year++) {
      for (let q = 1; q <= 4; q++) {
        options.push(`${year}-Q${q}`);
      }
    }
    return options;
  }

  // ─── Toggle truck detail ───────────────────────────────────────────────────

  function toggleTruckDetail(truckId: string) {
    setExpandedTruck((prev) => (prev === truckId ? null : truckId));
  }

  // ─── Manual adjustment form handlers ───────────────────────────────────────

  function handleAdjustmentFieldChange(
    field: keyof CreateMileageAdjustmentPayload,
    value: string | number,
  ) {
    setAdjustmentForm((prev) => ({ ...prev, [field]: value }));
    setAdjustmentError(null);
    setAdjustmentSuccess(null);
  }

  async function handleSubmitAdjustment(e: React.FormEvent) {
    e.preventDefault();

    // Validate
    if (!adjustmentForm.truck_id.trim()) {
      setAdjustmentError("Truck ID is required.");
      return;
    }
    if (!adjustmentForm.jurisdiction.trim()) {
      setAdjustmentError("Jurisdiction is required.");
      return;
    }
    if (adjustmentForm.miles === 0) {
      setAdjustmentError("Miles must be non-zero.");
      return;
    }
    if (!isValidQuarter(adjustmentForm.quarter)) {
      setAdjustmentError("Quarter must be in YYYY-Q[1-4] format.");
      return;
    }
    if (!adjustmentForm.reason.trim()) {
      setAdjustmentError("Reason is required for audit trail.");
      return;
    }

    setSubmittingAdjustment(true);
    setAdjustmentError(null);
    setAdjustmentSuccess(null);

    try {
      await createMileageAdjustment({
        ...adjustmentForm,
        jurisdiction: adjustmentForm.jurisdiction.toUpperCase(),
      });
      setAdjustmentSuccess(
        `Adjustment recorded: ${adjustmentForm.miles > 0 ? "+" : ""}${adjustmentForm.miles} miles for ${adjustmentForm.truck_id} in ${adjustmentForm.jurisdiction.toUpperCase()}.`,
      );
      // Reset form (keep quarter)
      setAdjustmentForm({
        truck_id: "",
        jurisdiction: "",
        miles: 0,
        quarter: adjustmentForm.quarter,
        reason: "",
      });
      // Refresh report if same quarter
      if (adjustmentForm.quarter === quarter) {
        await fetchReport(quarter);
      }
    } catch (err) {
      setAdjustmentError(
        err instanceof Error ? err.message : "Failed to submit adjustment",
      );
    } finally {
      setSubmittingAdjustment(false);
    }
  }

  // ─── Render ────────────────────────────────────────────────────────────────

  // Per-truck summary columns. ``Details`` toggles the expandable
  // jurisdiction breakdown rendered via the Table's renderExpanded hook.
  const truckColumns: Column<IFTATruckSummary>[] = [
    {
      key: "truck_id",
      label: "Truck ID",
      render: (t) => <span className="font-medium">{t.truck_id}</span>,
    },
    { key: "truck_name", label: "Truck Name", render: (t) => t.truck_name },
    {
      key: "total_miles",
      label: "Total Miles",
      align: "right",
      render: (t) => (
        <span className="font-mono text-sm">{formatNumber(t.total_miles)}</span>
      ),
    },
    {
      key: "total_gallons",
      label: "Total Gallons",
      align: "right",
      render: (t) => (
        <span className="font-mono text-sm">
          {formatNumber(t.total_gallons)}
        </span>
      ),
    },
    {
      key: "fleet_mpg",
      label: "MPG",
      align: "right",
      render: (t) => (
        <span className="font-mono text-sm">
          {formatNumber(t.fleet_mpg, 2)}
        </span>
      ),
    },
    {
      key: "jurisdiction_count",
      label: "Jurisdictions",
      align: "center",
      render: (t) => t.jurisdictions.length,
    },
    {
      key: "details",
      label: "Details",
      render: (t) => (
        <button
          type="button"
          onClick={() => toggleTruckDetail(t.truck_id)}
          className="text-info hover:text-info-dark text-sm underline"
        >
          {expandedTruck === t.truck_id ? "Hide" : "View"}
        </button>
      ),
    },
  ];

  const jurisdictionColumns: Column<IFTAJurisdictionEntry>[] = [
    {
      key: "jurisdiction",
      label: "Jurisdiction",
      render: (j) => <span className="font-medium">{j.jurisdiction}</span>,
    },
    {
      key: "total_miles",
      label: "Total Miles",
      align: "right",
      render: (j) => (
        <span className="font-mono">{formatNumber(j.total_miles)}</span>
      ),
    },
    {
      key: "taxable_miles",
      label: "Taxable Miles",
      align: "right",
      render: (j) => (
        <span className="font-mono">{formatNumber(j.taxable_miles)}</span>
      ),
    },
    {
      key: "tax_paid_gallons",
      label: "Tax Paid Gallons",
      align: "right",
      render: (j) => (
        <span className="font-mono">{formatNumber(j.tax_paid_gallons)}</span>
      ),
    },
    {
      key: "net_taxable_gallons",
      label: "Net Taxable Gallons",
      align: "right",
      render: (j) => (
        <span className="font-mono">{formatNumber(j.net_taxable_gallons)}</span>
      ),
    },
    {
      key: "tax_rate",
      label: "Tax Rate",
      align: "right",
      render: (j) => (
        <span className="font-mono">{formatCurrency(j.tax_rate)}</span>
      ),
    },
    {
      key: "tax_due",
      label: "Tax Due",
      align: "right",
      render: (j) => (
        <span className="font-mono">{formatCurrency(j.tax_due)}</span>
      ),
    },
  ];

  return (
    <div className="p-6">
      <PageHeader
        title="IFTA Quarterly Report"
        subtitle="View per-truck mileage by jurisdiction, fleet MPG, and manage manual mileage adjustments for IFTA filing.
        "
      />

      {/* Quarter selector */}
      <div className="mb-6 flex items-center gap-4">
        <label
          htmlFor="quarter-select"
          className="text-sm font-medium text-gray-700"
        >
          Quarter:
        </label>
        <select
          id="quarter-select"
          value={quarter}
          onChange={handleQuarterChange}
          className="border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
        >
          {getQuarterOptions().map((q) => (
            <option key={q} value={q}>
              {q}
            </option>
          ))}
        </select>
      </div>

      {/* Loading state */}
      {loading && (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading IFTA report...</span>
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      )}

      {/* Error state */}
      {!loading && error && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* Report content */}
      {!loading && !error && report && (
        <>
          {/* Summary cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
            <div className="bg-info-light border border-info rounded-lg p-4">
              <div className="text-sm text-info-dark font-medium">
                Fleet MPG
              </div>
              <div className="text-2xl font-bold text-info-dark mt-1">
                {formatNumber(report.fleet_mpg, 2)}
              </div>
              <div className="text-xs text-info mt-1">
                Total miles / total gallons
              </div>
            </div>
            <div className="bg-success-light border border-success-light rounded-lg p-4">
              <div className="text-sm text-success-dark font-medium">
                Total Trucks
              </div>
              <div className="text-2xl font-bold text-success-dark mt-1">
                {report.trucks.length}
              </div>
              <div className="text-xs text-success mt-1">
                Trucks with mileage data
              </div>
            </div>
            <div className="bg-warning-light border border-warning-light rounded-lg p-4">
              <div className="text-sm text-warning-dark font-medium">
                Incomplete Trucks
              </div>
              <div className="text-2xl font-bold text-warning-dark mt-1">
                {report.incomplete_trucks.length}
              </div>
              <div className="text-xs text-warning mt-1">
                Missing Geotab data — requires review
              </div>
            </div>
          </div>

          {/* Per-truck table */}
          <div className="mb-8">
            <h2 className="text-lg font-semibold mb-3">Per-Truck Summary</h2>
            <Table<IFTATruckSummary>
              ariaLabel="Per-truck IFTA summary"
              columns={truckColumns}
              data={report.trucks}
              getRowId={(truck) => truck.truck_id}
              emptyState={
                <span className="text-gray-500">
                  No truck data available for {quarter}.
                </span>
              }
              renderExpanded={(truck) =>
                expandedTruck === truck.truck_id ? (
                  <div className="bg-gray-50 p-4 border-b border-gray-200">
                    <Table<IFTAJurisdictionEntry>
                      variant="compact"
                      ariaLabel={`Jurisdiction breakdown for ${truck.truck_id}`}
                      columns={jurisdictionColumns}
                      data={truck.jurisdictions}
                      getRowId={(j) => j.jurisdiction}
                      emptyState={
                        <span className="text-gray-500">
                          No jurisdiction data.
                        </span>
                      }
                    />
                  </div>
                ) : null
              }
            />
          </div>

          {/* Incomplete trucks section */}
          {report.incomplete_trucks.length > 0 && (
            <div className="mb-8">
              <h2 className="text-lg font-semibold mb-3">Incomplete Trucks</h2>
              <div className="bg-warning-light border border-warning-light rounded-lg p-4">
                <p className="text-sm text-warning-dark mb-3">
                  The following trucks have incomplete Geotab data for {quarter}
                  . Manual mileage adjustments may be required.
                </p>
                <div className="flex flex-wrap gap-2">
                  {report.incomplete_trucks.map((flag) => (
                    <span
                      key={flag.truck_id}
                      title={flag.reason}
                      className="inline-block bg-warning-light text-warning-dark px-3 py-1 rounded text-sm font-medium"
                    >
                      {flag.truck_id}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}
        </>
      )}

      {/* Manual adjustment form */}
      <div className="border-t pt-6 mt-6">
        <h2 className="text-lg font-semibold mb-3">
          Manual Mileage Adjustment
        </h2>
        <p className="text-sm text-gray-600 mb-4">
          Record a manual mileage adjustment for a truck. Positive values add
          miles; negative values subtract. All adjustments are logged for audit
          purposes.
        </p>

        <form onSubmit={handleSubmitAdjustment} className="space-y-4 max-w-xl">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="adj-truck-id"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Truck ID
              </label>
              <input
                id="adj-truck-id"
                type="text"
                value={adjustmentForm.truck_id}
                onChange={(e) =>
                  handleAdjustmentFieldChange("truck_id", e.target.value)
                }
                placeholder="e.g. truck_001"
                className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>
            <div>
              <label
                htmlFor="adj-jurisdiction"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Jurisdiction (State)
              </label>
              <input
                id="adj-jurisdiction"
                type="text"
                value={adjustmentForm.jurisdiction}
                onChange={(e) =>
                  handleAdjustmentFieldChange("jurisdiction", e.target.value)
                }
                placeholder="e.g. TX"
                maxLength={2}
                className="w-full border border-gray-300 rounded px-3 py-2 text-sm uppercase focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>
            <div>
              <label
                htmlFor="adj-miles"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Miles (+/-)
              </label>
              <input
                id="adj-miles"
                type="number"
                step="0.1"
                value={adjustmentForm.miles}
                onChange={(e) =>
                  handleAdjustmentFieldChange(
                    "miles",
                    parseFloat(e.target.value) || 0,
                  )
                }
                className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>
            <div>
              <label
                htmlFor="adj-quarter"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Quarter
              </label>
              <select
                id="adj-quarter"
                value={adjustmentForm.quarter}
                onChange={(e) =>
                  handleAdjustmentFieldChange("quarter", e.target.value)
                }
                className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
              >
                {getQuarterOptions().map((q) => (
                  <option key={q} value={q}>
                    {q}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label
              htmlFor="adj-reason"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Reason
            </label>
            <textarea
              id="adj-reason"
              value={adjustmentForm.reason}
              onChange={(e) =>
                handleAdjustmentFieldChange("reason", e.target.value)
              }
              placeholder="Explain the reason for this adjustment (required for audit trail)"
              rows={3}
              className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>

          {/* Adjustment error */}
          {adjustmentError && (
            <div
              role="alert"
              className="bg-error-light border border-error-light text-error-dark p-3 rounded text-sm"
            >
              {adjustmentError}
            </div>
          )}

          {/* Adjustment success */}
          {adjustmentSuccess && (
            <div
              role="status"
              className="bg-success-light border border-success-light text-success-dark p-3 rounded text-sm"
            >
              {adjustmentSuccess}
            </div>
          )}

          <button
            type="submit"
            disabled={submittingAdjustment}
            className="bg-primary text-white px-4 py-2 rounded hover:bg-primary-hover disabled:opacity-50 text-sm"
          >
            {submittingAdjustment ? "Submitting..." : "Record Adjustment"}
          </button>
        </form>
      </div>
    </div>
  );
}
