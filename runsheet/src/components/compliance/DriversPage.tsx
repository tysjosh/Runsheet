"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  getDrivers,
  getDriver,
  getDriversDashboard,
  createDriver,
  updateDriver,
  type Driver,
  type DriverStatus,
  type DQFDashboard,
  type DQFDashboardEntry,
  type DriverQualificationStatus,
  type CreateDriverPayload,
  type UpdateDriverPayload,
} from "../../services/complianceApi";

// ─── Sub-view types ──────────────────────────────────────────────────────────

type ViewMode = "list" | "detail" | "dashboard" | "add" | "edit";

// ─── Alert level color mapping ───────────────────────────────────────────────

function alertLevelBadge(level: DriverQualificationStatus["alert_level"]) {
  switch (level) {
    case "ok":
      return "bg-green-100 text-green-800";
    case "warning":
      return "bg-yellow-100 text-yellow-800";
    case "urgent":
      return "bg-orange-100 text-orange-800";
    case "critical":
      return "bg-red-100 text-red-800";
    case "expired":
      return "bg-red-200 text-red-900";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

function statusBadge(status: DriverStatus) {
  switch (status) {
    case "active":
      return "bg-green-100 text-green-800";
    case "suspended":
      return "bg-yellow-100 text-yellow-800";
    case "expired":
      return "bg-red-100 text-red-800";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString();
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function DriversPage() {
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [drivers, setDrivers] = useState<Driver[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [statusFilter, setStatusFilter] = useState<string>("");

  // Detail view state
  const [selectedDriver, setSelectedDriver] = useState<Driver | null>(null);

  // Dashboard state
  const [dashboard, setDashboard] = useState<DQFDashboard | null>(null);
  const [dashboardLoading, setDashboardLoading] = useState(false);

  // Form state
  const [editingDriver, setEditingDriver] = useState<Driver | null>(null);

  // ─── Fetch drivers list ──────────────────────────────────────────────────

  const fetchDrivers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: { status?: DriverStatus; page: number; size: number } = {
        page,
        size: 20,
      };
      if (statusFilter) filters.status = statusFilter as DriverStatus;

      const response = await getDrivers(filters);
      setDrivers(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load drivers");
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter]);

  useEffect(() => {
    if (viewMode === "list") {
      fetchDrivers();
    }
  }, [fetchDrivers, viewMode]);

  // ─── Fetch dashboard ─────────────────────────────────────────────────────

  const fetchDashboard = useCallback(async () => {
    setDashboardLoading(true);
    setError(null);
    try {
      const response = await getDriversDashboard();
      setDashboard(response.data);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load DQF dashboard",
      );
    } finally {
      setDashboardLoading(false);
    }
  }, []);

  useEffect(() => {
    if (viewMode === "dashboard") {
      fetchDashboard();
    }
  }, [fetchDashboard, viewMode]);

  // ─── Fetch single driver detail ──────────────────────────────────────────

  const handleViewDetail = async (driverId: string) => {
    setLoading(true);
    setError(null);
    try {
      const response = await getDriver(driverId);
      setSelectedDriver(response.data);
      setViewMode("detail");
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load driver details",
      );
    } finally {
      setLoading(false);
    }
  };

  // ─── Edit driver ─────────────────────────────────────────────────────────

  const handleEditDriver = (driver: Driver) => {
    setEditingDriver(driver);
    setViewMode("edit");
  };

  // ─── Render: DQF Dashboard Summary Cards ────────────────────────────────

  function renderDashboardSummary() {
    if (!dashboard) return null;
    return (
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
          <p className="text-sm text-gray-500">Active Drivers</p>
          <p className="text-2xl font-bold text-green-700">
            {dashboard.total_active}
          </p>
        </div>
        <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
          <p className="text-sm text-gray-500">Suspended</p>
          <p className="text-2xl font-bold text-yellow-700">
            {dashboard.total_suspended}
          </p>
        </div>
        <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
          <p className="text-sm text-gray-500">Expiring Soon</p>
          <p className="text-2xl font-bold text-red-700">
            {dashboard.total_expiring_soon}
          </p>
        </div>
      </div>
    );
  }

  // ─── Render: Dashboard View ──────────────────────────────────────────────

  function renderDashboard() {
    if (dashboardLoading) {
      return (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading DQF dashboard...</span>
          <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
        </div>
      );
    }

    if (!dashboard) return null;

    return (
      <div>
        {renderDashboardSummary()}

        <table className="w-full border-collapse" role="table">
          <thead>
            <tr className="border-b bg-gray-50">
              <th className="text-left p-3 font-medium">Driver</th>
              <th className="text-left p-3 font-medium">Status</th>
              <th className="text-left p-3 font-medium">Qualifications</th>
            </tr>
          </thead>
          <tbody>
            {dashboard.drivers.map((entry: DQFDashboardEntry) => (
              <tr
                key={entry.driver_id}
                className="border-b hover:bg-gray-50 cursor-pointer"
                onClick={() => handleViewDetail(entry.driver_id)}
              >
                <td className="p-3 font-medium">{entry.full_name}</td>
                <td className="p-3">
                  <span
                    className={`inline-block px-2 py-1 rounded text-xs font-medium ${statusBadge(entry.status)}`}
                  >
                    {entry.status}
                  </span>
                </td>
                <td className="p-3">
                  <div className="flex flex-wrap gap-1">
                    {entry.qualifications.map((qual) => (
                      <span
                        key={qual.field}
                        className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${alertLevelBadge(qual.alert_level)}`}
                        title={`${qual.field}: expires ${formatDate(qual.expiry_date)} (${qual.days_until_expiry} days)`}
                      >
                        {qual.field.replace(/_/g, " ")}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
            {dashboard.drivers.length === 0 && (
              <tr>
                <td colSpan={3} className="p-6 text-center text-gray-500">
                  No drivers found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    );
  }

  // ─── Render: Driver Detail View ──────────────────────────────────────────

  function renderDetail() {
    if (!selectedDriver) return null;

    const qualificationFields = [
      { label: "CDL Expiry", value: selectedDriver.cdl_expiry_date },
      {
        label: "Medical Card Expiry",
        value: selectedDriver.medical_card_expiry_date,
      },
      {
        label: "HAZMAT Endorsement",
        value: selectedDriver.hazmat_endorsement_expiry_date,
      },
      {
        label: "Tanker Endorsement",
        value: selectedDriver.tanker_endorsement_expiry_date,
      },
      { label: "Last Drug Test", value: selectedDriver.last_drug_test_date },
      { label: "Last MVR", value: selectedDriver.last_mvr_date },
    ];

    return (
      <div className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm">
        <div className="flex justify-between items-start mb-6">
          <div>
            <h2 className="text-xl font-bold">{selectedDriver.full_name}</h2>
            <p className="text-gray-500 mt-1">
              CDL: {selectedDriver.cdl_number} ({selectedDriver.cdl_class}) —{" "}
              {selectedDriver.cdl_state}
            </p>
          </div>
          <div className="flex gap-2">
            <span
              className={`inline-block px-3 py-1 rounded text-sm font-medium ${statusBadge(selectedDriver.status)}`}
            >
              {selectedDriver.status}
            </span>
            <button
              type="button"
              onClick={() => handleEditDriver(selectedDriver)}
              className="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-700"
            >
              Edit
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {qualificationFields.map((field) => (
            <div
              key={field.label}
              className="border border-gray-100 rounded p-3"
            >
              <p className="text-sm text-gray-500">{field.label}</p>
              <p className="font-medium">{formatDate(field.value)}</p>
            </div>
          ))}
        </div>
      </div>
    );
  }

  // ─── Render: Add/Edit Form ───────────────────────────────────────────────

  function renderForm() {
    const isEdit = viewMode === "edit";
    return (
      <DriverForm
        initialData={isEdit ? editingDriver : null}
        onSubmit={async (data) => {
          setLoading(true);
          setError(null);
          try {
            if (isEdit && editingDriver) {
              await updateDriver(editingDriver.driver_id, data as UpdateDriverPayload);
            } else {
              await createDriver(data as CreateDriverPayload);
            }
            setViewMode("list");
          } catch (err) {
            setError(
              err instanceof Error ? err.message : "Failed to save driver",
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

  // ─── Render: Listing View ────────────────────────────────────────────────

  function renderList() {
    return (
      <>
        {/* Filters */}
        <div className="flex gap-4 mb-6 items-end">
          <div>
            <label
              htmlFor="driver-status-filter"
              className="block text-sm font-medium mb-1"
            >
              Status
            </label>
            <select
              id="driver-status-filter"
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2"
            >
              <option value="">All</option>
              <option value="active">Active</option>
              <option value="suspended">Suspended</option>
              <option value="expired">Expired</option>
            </select>
          </div>
        </div>

        {/* Loading state */}
        {loading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading drivers...</span>
            <div className="animate-spin h-8 w-8 border-4 border-blue-600 border-t-transparent rounded-full" />
          </div>
        )}

        {/* Driver table */}
        {!loading && !error && (
          <>
            <table className="w-full border-collapse" role="table">
              <thead>
                <tr className="border-b bg-gray-50">
                  <th className="text-left p-3 font-medium">Name</th>
                  <th className="text-left p-3 font-medium">CDL Number</th>
                  <th className="text-left p-3 font-medium">CDL Class</th>
                  <th className="text-left p-3 font-medium">Status</th>
                  <th className="text-left p-3 font-medium">CDL Expiry</th>
                  <th className="text-left p-3 font-medium">
                    Medical Card Expiry
                  </th>
                  <th className="text-left p-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {drivers.map((driver) => (
                  <tr
                    key={driver.driver_id}
                    className="border-b hover:bg-gray-50"
                  >
                    <td className="p-3 font-medium">{driver.full_name}</td>
                    <td className="p-3">{driver.cdl_number}</td>
                    <td className="p-3">{driver.cdl_class}</td>
                    <td className="p-3">
                      <span
                        className={`inline-block px-2 py-1 rounded text-xs font-medium ${statusBadge(driver.status)}`}
                      >
                        {driver.status}
                      </span>
                    </td>
                    <td className="p-3">{formatDate(driver.cdl_expiry_date)}</td>
                    <td className="p-3">
                      {formatDate(driver.medical_card_expiry_date)}
                    </td>
                    <td className="p-3">
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() => handleViewDetail(driver.driver_id)}
                          className="text-blue-600 hover:underline text-sm"
                        >
                          View
                        </button>
                        <button
                          type="button"
                          onClick={() => handleEditDriver(driver)}
                          className="text-gray-600 hover:underline text-sm"
                        >
                          Edit
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
                {drivers.length === 0 && (
                  <tr>
                    <td
                      colSpan={7}
                      className="p-6 text-center text-gray-500"
                    >
                      No drivers found.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>

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

  // ─── Main Render ─────────────────────────────────────────────────────────

  return (
    <div className="p-6">
      <header className="mb-6">
        <div className="flex justify-between items-center">
          <div>
            <h1 className="text-2xl font-bold">Driver Qualification Files</h1>
            <p className="text-gray-600 mt-1">
              Manage driver qualifications, certifications, and DQF compliance.
            </p>
          </div>
          <div className="flex gap-2">
            {viewMode !== "list" && viewMode !== "add" && viewMode !== "edit" && (
              <button
                type="button"
                onClick={() => setViewMode("list")}
                className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
              >
                Back to List
              </button>
            )}
            {viewMode === "list" && (
              <>
                <button
                  type="button"
                  onClick={() => setViewMode("dashboard")}
                  className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
                >
                  DQF Dashboard
                </button>
                <button
                  type="button"
                  onClick={() => setViewMode("add")}
                  className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700"
                >
                  Add Driver
                </button>
              </>
            )}
            {viewMode === "dashboard" && (
              <button
                type="button"
                onClick={() => setViewMode("list")}
                className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
              >
                Back to List
              </button>
            )}
            {viewMode === "detail" && (
              <button
                type="button"
                onClick={() => setViewMode("list")}
                className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
              >
                Back to List
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Error state */}
      {error && (
        <div
          role="alert"
          className="bg-red-50 border border-red-200 text-red-700 p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* View content */}
      {viewMode === "list" && renderList()}
      {viewMode === "detail" && renderDetail()}
      {viewMode === "dashboard" && renderDashboard()}
      {(viewMode === "add" || viewMode === "edit") && renderForm()}
    </div>
  );
}

// ─── Driver Form Sub-Component ───────────────────────────────────────────────

interface DriverFormProps {
  initialData: Driver | null;
  onSubmit: (data: CreateDriverPayload | UpdateDriverPayload) => Promise<void>;
  onCancel: () => void;
  loading: boolean;
}

function DriverForm({ initialData, onSubmit, onCancel, loading }: DriverFormProps) {
  const [fullName, setFullName] = useState(initialData?.full_name ?? "");
  const [cdlNumber, setCdlNumber] = useState(initialData?.cdl_number ?? "");
  const [cdlState, setCdlState] = useState(initialData?.cdl_state ?? "");
  const [cdlClass, setCdlClass] = useState<"A" | "B" | "C">(
    initialData?.cdl_class ?? "A",
  );
  const [cdlExpiryDate, setCdlExpiryDate] = useState(
    initialData?.cdl_expiry_date ?? "",
  );
  const [medicalCardExpiryDate, setMedicalCardExpiryDate] = useState(
    initialData?.medical_card_expiry_date ?? "",
  );
  const [hazmatExpiryDate, setHazmatExpiryDate] = useState(
    initialData?.hazmat_endorsement_expiry_date ?? "",
  );
  const [tankerExpiryDate, setTankerExpiryDate] = useState(
    initialData?.tanker_endorsement_expiry_date ?? "",
  );

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const data: CreateDriverPayload = {
      full_name: fullName,
      cdl_number: cdlNumber,
      cdl_state: cdlState,
      cdl_class: cdlClass,
      cdl_expiry_date: cdlExpiryDate,
      medical_card_expiry_date: medicalCardExpiryDate,
      hazmat_endorsement_expiry_date: hazmatExpiryDate || null,
      tanker_endorsement_expiry_date: tankerExpiryDate || null,
    };
    await onSubmit(data);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-2xl"
    >
      <h2 className="text-lg font-bold mb-4">
        {initialData ? "Edit Driver" : "Add New Driver"}
      </h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label htmlFor="full-name" className="block text-sm font-medium mb-1">
            Full Name
          </label>
          <input
            id="full-name"
            type="text"
            required
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
        <div>
          <label htmlFor="cdl-number" className="block text-sm font-medium mb-1">
            CDL Number
          </label>
          <input
            id="cdl-number"
            type="text"
            required
            value={cdlNumber}
            onChange={(e) => setCdlNumber(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
        <div>
          <label htmlFor="cdl-state" className="block text-sm font-medium mb-1">
            CDL State
          </label>
          <input
            id="cdl-state"
            type="text"
            required
            maxLength={2}
            value={cdlState}
            onChange={(e) => setCdlState(e.target.value.toUpperCase())}
            className="w-full border rounded px-3 py-2"
            placeholder="TX"
          />
        </div>
        <div>
          <label htmlFor="cdl-class" className="block text-sm font-medium mb-1">
            CDL Class
          </label>
          <select
            id="cdl-class"
            value={cdlClass}
            onChange={(e) => setCdlClass(e.target.value as "A" | "B" | "C")}
            className="w-full border rounded px-3 py-2"
          >
            <option value="A">Class A</option>
            <option value="B">Class B</option>
            <option value="C">Class C</option>
          </select>
        </div>
        <div>
          <label htmlFor="cdl-expiry" className="block text-sm font-medium mb-1">
            CDL Expiry Date
          </label>
          <input
            id="cdl-expiry"
            type="date"
            required
            value={cdlExpiryDate}
            onChange={(e) => setCdlExpiryDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
        <div>
          <label
            htmlFor="medical-expiry"
            className="block text-sm font-medium mb-1"
          >
            Medical Card Expiry
          </label>
          <input
            id="medical-expiry"
            type="date"
            required
            value={medicalCardExpiryDate}
            onChange={(e) => setMedicalCardExpiryDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
        <div>
          <label
            htmlFor="hazmat-expiry"
            className="block text-sm font-medium mb-1"
          >
            HAZMAT Endorsement Expiry
          </label>
          <input
            id="hazmat-expiry"
            type="date"
            value={hazmatExpiryDate}
            onChange={(e) => setHazmatExpiryDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
        <div>
          <label
            htmlFor="tanker-expiry"
            className="block text-sm font-medium mb-1"
          >
            Tanker Endorsement Expiry
          </label>
          <input
            id="tanker-expiry"
            type="date"
            value={tankerExpiryDate}
            onChange={(e) => setTankerExpiryDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
        </div>
      </div>

      <div className="flex gap-3 mt-6">
        <button
          type="submit"
          disabled={loading}
          className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 disabled:opacity-50"
        >
          {loading ? "Saving..." : initialData ? "Update Driver" : "Add Driver"}
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
