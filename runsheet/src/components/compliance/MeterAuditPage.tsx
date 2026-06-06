"use client";

import type React from "react";
import { useCallback, useEffect, useState } from "react";
import { type Column, EntityLink, Table } from "@/components/ui";
import {
  type CreateMeterPayload,
  createMeter,
  getMeterAuditTrail,
  getMeters,
  type MeterAuditEntry,
  type MeterRegistration,
} from "../../services/complianceApi";

// ─── Sub-view types ──────────────────────────────────────────────────────────

type ViewMode = "list" | "add" | "audit-trail";

// ─── Badge helpers ───────────────────────────────────────────────────────────

function calibrationStatusBadge(expiryDate: string): {
  label: string;
  className: string;
} {
  const now = new Date();
  const expiry = new Date(expiryDate);
  const diffMs = expiry.getTime() - now.getTime();
  const diffDays = Math.ceil(diffMs / (1000 * 60 * 60 * 24));

  if (diffDays < 0) {
    return { label: "Expired", className: "bg-error-light text-error-dark" };
  }
  if (diffDays <= 30) {
    return {
      label: `Expiring (${diffDays}d)`,
      className: "bg-warning-light text-warning-dark",
    };
  }
  return { label: "Valid", className: "bg-success-light text-success-dark" };
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString();
}

function varianceBadge(flag: string | null): {
  label: string;
  className: string;
} | null {
  if (!flag) return null;
  return { label: flag, className: "bg-error-light text-error-dark" };
}

// ─── Table columns ───────────────────────────────────────────────────────────

function getMeterColumns(
  onViewAuditTrail: (meter: MeterRegistration) => void,
): Column<MeterRegistration>[] {
  return [
    {
      key: "meter_number",
      label: "Meter Number",
      render: (meter) => (
        <span className="font-medium">{meter.meter_number}</span>
      ),
    },
    {
      key: "truck_id",
      label: "Truck ID",
      // The meter's subject is its truck, navigable to the Fleet module as a
      // canonical asset (Req 11.3, 13.1).
      render: (meter) => <EntityLink type="asset" id={meter.truck_id} />,
    },
    {
      key: "calibration_certificate_number",
      label: "Calibration Cert #",
      render: (meter) => meter.calibration_certificate_number,
    },
    {
      key: "calibration_date",
      label: "Calibration Date",
      render: (meter) => formatDate(meter.calibration_date),
    },
    {
      key: "calibration_expiry_date",
      label: "Expiry Date",
      render: (meter) => formatDate(meter.calibration_expiry_date),
    },
    {
      key: "status",
      label: "Status",
      render: (meter) => {
        const status = calibrationStatusBadge(meter.calibration_expiry_date);
        return (
          <span
            className={`inline-block px-2 py-1 rounded text-xs font-medium ${status.className}`}
          >
            {status.label}
          </span>
        );
      },
    },
    {
      key: "weights_measures_authority",
      label: "W&M Authority",
      render: (meter) => meter.weights_measures_authority,
    },
    {
      key: "actions",
      label: "Actions",
      render: (meter) => (
        <button
          type="button"
          onClick={() => onViewAuditTrail(meter)}
          className="text-info hover:underline text-sm"
        >
          Audit Trail
        </button>
      ),
    },
  ];
}

const auditTrailColumns: Column<MeterAuditEntry>[] = [
  {
    key: "delivery_id",
    label: "Delivery ID",
    render: (entry) => <span className="font-medium">{entry.delivery_id}</span>,
  },
  {
    key: "invoice_id",
    label: "Invoice ID",
    render: (entry) => entry.invoice_id,
  },
  {
    key: "gross_gallons",
    label: "Gross Gallons",
    render: (entry) => entry.gross_gallons.toFixed(1),
  },
  {
    key: "net_gallons",
    label: "Net Gallons",
    render: (entry) => entry.net_gallons.toFixed(1),
  },
  {
    key: "variance",
    label: "Variance",
    render: (entry) => {
      const vBadge = varianceBadge(entry.variance_flag);
      return vBadge ? (
        <span
          className={`inline-block px-2 py-1 rounded text-xs font-medium ${vBadge.className}`}
        >
          {vBadge.label}
        </span>
      ) : (
        <span className="text-success text-xs font-medium">OK</span>
      );
    },
  },
  {
    key: "timestamp",
    label: "Timestamp",
    render: (entry) => formatDate(entry.timestamp),
  },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function MeterAuditPage() {
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [meters, setMeters] = useState<MeterRegistration[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);

  // Filters
  const [truckIdFilter, setTruckIdFilter] = useState<string>("");

  // Audit trail state
  const [selectedMeter, setSelectedMeter] = useState<MeterRegistration | null>(
    null,
  );
  const [auditEntries, setAuditEntries] = useState<MeterAuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditPage, setAuditPage] = useState(1);
  const [auditTotalPages, setAuditTotalPages] = useState(1);

  // ─── Fetch meters ────────────────────────────────────────────────────────

  const fetchMeters = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: { truck_id?: string; page: number; size: number } = {
        page,
        size: 20,
      };
      if (truckIdFilter) filters.truck_id = truckIdFilter;

      const response = await getMeters(filters);
      setMeters(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load meters");
    } finally {
      setLoading(false);
    }
  }, [page, truckIdFilter]);

  useEffect(() => {
    if (viewMode === "list") {
      fetchMeters();
    }
  }, [fetchMeters, viewMode]);

  // ─── Fetch audit trail ───────────────────────────────────────────────────

  const fetchAuditTrail = useCallback(async () => {
    if (!selectedMeter) return;
    setAuditLoading(true);
    setAuditError(null);
    try {
      const response = await getMeterAuditTrail(selectedMeter.meter_id, {
        page: auditPage,
        size: 20,
      });
      setAuditEntries(response.data ?? []);
      setAuditTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setAuditError(
        err instanceof Error ? err.message : "Failed to load audit trail",
      );
    } finally {
      setAuditLoading(false);
    }
  }, [selectedMeter, auditPage]);

  useEffect(() => {
    if (viewMode === "audit-trail" && selectedMeter) {
      fetchAuditTrail();
    }
  }, [fetchAuditTrail, viewMode, selectedMeter]);

  // ─── Handlers ────────────────────────────────────────────────────────────

  function handleViewAuditTrail(meter: MeterRegistration) {
    setSelectedMeter(meter);
    setAuditPage(1);
    setAuditEntries([]);
    setViewMode("audit-trail");
  }

  // ─── Render: Listing View ────────────────────────────────────────────────

  function renderList() {
    return (
      <>
        {/* Filters */}
        <div className="flex flex-wrap gap-4 mb-6 items-end">
          <div>
            <label
              htmlFor="truck-id-filter"
              className="block text-sm font-medium mb-1"
            >
              Truck ID
            </label>
            <input
              id="truck-id-filter"
              type="text"
              value={truckIdFilter}
              onChange={(e) => {
                setTruckIdFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2 w-48"
              placeholder="Filter by truck..."
            />
          </div>
        </div>

        {/* Loading state */}
        {loading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading meters...</span>
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

        {/* Meters table */}
        {!loading && !error && (
          <>
            <Table<MeterRegistration>
              ariaLabel="Registered meters"
              columns={getMeterColumns(handleViewAuditTrail)}
              data={meters}
              getRowId={(meter) => meter.meter_id}
              emptyState={
                <span className="text-gray-500">No meters registered.</span>
              }
            />

            {/* Pagination */}
            <nav
              aria-label="Pagination"
              className="flex justify-between items-center mt-4"
            >
              <button
                type="button"
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="px-3 py-1 border rounded disabled:opacity-50"
              >
                Previous
              </button>
              <span className="text-sm text-gray-600">
                Page {page} of {totalPages}
              </span>
              <button
                type="button"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
                className="px-3 py-1 border rounded disabled:opacity-50"
              >
                Next
              </button>
            </nav>
          </>
        )}
      </>
    );
  }

  // ─── Render: Audit Trail View ────────────────────────────────────────────

  function renderAuditTrail() {
    if (!selectedMeter) return null;

    const status = calibrationStatusBadge(
      selectedMeter.calibration_expiry_date,
    );

    return (
      <div>
        {/* Meter summary header */}
        <div className="bg-gray-50 border border-gray-200 rounded-lg p-4 mb-6">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <span className="text-gray-500 block">Meter Number</span>
              <span className="font-bold">{selectedMeter.meter_number}</span>
            </div>
            <div>
              <span className="text-gray-500 block">Truck ID</span>
              <span className="font-medium">
                <EntityLink type="asset" id={selectedMeter.truck_id} />
              </span>
            </div>
            <div>
              <span className="text-gray-500 block">Calibration Expiry</span>
              <span className="font-medium">
                {formatDate(selectedMeter.calibration_expiry_date)}
              </span>
            </div>
            <div>
              <span className="text-gray-500 block">Status</span>
              <span
                className={`inline-block px-2 py-1 rounded text-xs font-medium ${status.className}`}
              >
                {status.label}
              </span>
            </div>
          </div>
        </div>

        {/* Loading state */}
        {auditLoading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading audit trail...</span>
            <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
          </div>
        )}

        {/* Error state */}
        {!auditLoading && auditError && (
          <div
            role="alert"
            className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
          >
            {auditError}
          </div>
        )}

        {/* Audit trail table */}
        {!auditLoading && !auditError && (
          <>
            <Table<MeterAuditEntry>
              ariaLabel="Meter delivery audit trail"
              columns={auditTrailColumns}
              data={auditEntries}
              getRowId={(entry) => entry.audit_id}
              emptyState={
                <span className="text-gray-500">
                  No audit entries found for this meter.
                </span>
              }
            />

            {/* Audit trail pagination */}
            <nav
              aria-label="Audit trail pagination"
              className="flex justify-between items-center mt-4"
            >
              <button
                type="button"
                disabled={auditPage <= 1}
                onClick={() => setAuditPage((p) => p - 1)}
                className="px-3 py-1 border rounded disabled:opacity-50"
              >
                Previous
              </button>
              <span className="text-sm text-gray-600">
                Page {auditPage} of {auditTotalPages}
              </span>
              <button
                type="button"
                disabled={auditPage >= auditTotalPages}
                onClick={() => setAuditPage((p) => p + 1)}
                className="px-3 py-1 border rounded disabled:opacity-50"
              >
                Next
              </button>
            </nav>
          </>
        )}
      </div>
    );
  }

  // ─── Render: Register Meter Form ─────────────────────────────────────────

  function renderForm() {
    return (
      <RegisterMeterForm
        onSubmit={async (data) => {
          setLoading(true);
          setError(null);
          try {
            await createMeter(data);
            setViewMode("list");
          } catch (err) {
            setError(
              err instanceof Error ? err.message : "Failed to register meter",
            );
          } finally {
            setLoading(false);
          }
        }}
        onCancel={() => setViewMode("list")}
        loading={loading}
      />
    );
  }

  // ─── Main Render ─────────────────────────────────────────────────────────

  return (
    <div className="p-6">
      <header className="mb-6">
        <div className="flex justify-between items-center">
          <div>
            <h1 className="text-2xl font-bold">Meter Registry & Audit</h1>
            <p className="text-gray-600 mt-1">
              Manage registered meters, track calibration status, and view
              per-meter delivery audit trails.
            </p>
          </div>
          <div className="flex gap-2">
            {viewMode !== "list" && (
              <button
                type="button"
                onClick={() => {
                  setViewMode("list");
                  setSelectedMeter(null);
                }}
                className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
              >
                Back to List
              </button>
            )}
            {viewMode === "list" && (
              <button
                type="button"
                onClick={() => setViewMode("add")}
                className="bg-primary text-white px-4 py-2 rounded text-sm hover:bg-primary-hover"
              >
                Register Meter
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Error state (top-level) */}
      {error && viewMode === "list" && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* View content */}
      {viewMode === "list" && renderList()}
      {viewMode === "add" && renderForm()}
      {viewMode === "audit-trail" && renderAuditTrail()}
    </div>
  );
}

// ─── Register Meter Form Sub-Component ───────────────────────────────────────

interface RegisterMeterFormProps {
  onSubmit: (data: CreateMeterPayload) => Promise<void>;
  onCancel: () => void;
  loading: boolean;
}

function RegisterMeterForm({
  onSubmit,
  onCancel,
  loading,
}: RegisterMeterFormProps) {
  const [meterNumber, setMeterNumber] = useState("");
  const [truckId, setTruckId] = useState("");
  const [calibrationCertNumber, setCalibrationCertNumber] = useState("");
  const [calibrationDate, setCalibrationDate] = useState("");
  const [calibrationExpiryDate, setCalibrationExpiryDate] = useState("");
  const [weightsMeasuresAuthority, setWeightsMeasuresAuthority] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const data: CreateMeterPayload = {
      meter_number: meterNumber,
      truck_id: truckId,
      calibration_certificate_number: calibrationCertNumber,
      calibration_date: calibrationDate,
      calibration_expiry_date: calibrationExpiryDate,
      weights_measures_authority: weightsMeasuresAuthority,
    };

    await onSubmit(data);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-2xl"
    >
      <h2 className="text-lg font-bold mb-4">Register New Meter</h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Meter Number */}
        <div>
          <label
            htmlFor="meter-number"
            className="block text-sm font-medium mb-1"
          >
            Meter Number
          </label>
          <input
            id="meter-number"
            type="text"
            required
            value={meterNumber}
            onChange={(e) => setMeterNumber(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g. MTR-001"
          />
        </div>

        {/* Truck ID */}
        <div>
          <label
            htmlFor="meter-truck-id"
            className="block text-sm font-medium mb-1"
          >
            Truck ID
          </label>
          <input
            id="meter-truck-id"
            type="text"
            required
            value={truckId}
            onChange={(e) => setTruckId(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g. TRK-101"
          />
        </div>

        {/* Calibration Certificate Number */}
        <div>
          <label
            htmlFor="meter-cert-number"
            className="block text-sm font-medium mb-1"
          >
            Calibration Certificate #
          </label>
          <input
            id="meter-cert-number"
            type="text"
            required
            value={calibrationCertNumber}
            onChange={(e) => setCalibrationCertNumber(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g. CAL-2024-0001"
          />
        </div>

        {/* Weights & Measures Authority */}
        <div>
          <label
            htmlFor="meter-wm-authority"
            className="block text-sm font-medium mb-1"
          >
            Weights & Measures Authority
          </label>
          <input
            id="meter-wm-authority"
            type="text"
            required
            value={weightsMeasuresAuthority}
            onChange={(e) => setWeightsMeasuresAuthority(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g. TX Dept of Agriculture"
          />
        </div>

        {/* Calibration Date */}
        <div>
          <label
            htmlFor="meter-calibration-date"
            className="block text-sm font-medium mb-1"
          >
            Calibration Date
          </label>
          <input
            id="meter-calibration-date"
            type="date"
            required
            value={calibrationDate}
            onChange={(e) => setCalibrationDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>

        {/* Calibration Expiry Date */}
        <div>
          <label
            htmlFor="meter-expiry-date"
            className="block text-sm font-medium mb-1"
          >
            Calibration Expiry Date
          </label>
          <input
            id="meter-expiry-date"
            type="date"
            required
            value={calibrationExpiryDate}
            onChange={(e) => setCalibrationExpiryDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
      </div>

      <div className="flex gap-3 mt-6">
        <button
          type="submit"
          disabled={loading}
          className="bg-primary text-white px-4 py-2 rounded hover:bg-primary-hover disabled:opacity-50"
        >
          {loading ? "Registering..." : "Register Meter"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 border rounded hover:bg-gray-50"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
