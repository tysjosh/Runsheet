"use client";

import type React from "react";
import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  FilterBar,
  PageHeader,
  Pagination,
  StatsBar,
  Table,
} from "@/components/ui";
import {
  type CreateDriverPayload,
  createDriver,
  type DQFDashboard,
  type DQFDashboardEntry,
  type Driver,
  type DriverQualificationStatus,
  type DriverStatus,
  getDriver,
  getDrivers,
  getDriversDashboard,
  type UpdateDriverPayload,
  updateDriver,
} from "../../services/complianceApi";

// ─── Sub-view types ──────────────────────────────────────────────────────────

type ViewMode = "list" | "detail" | "dashboard" | "add" | "edit";

// ─── Alert level color mapping ───────────────────────────────────────────────

function _alertLevelBadge(level: DriverQualificationStatus["alert_level"]) {
  switch (level) {
    case "ok":
      return "bg-success-light text-success-dark";
    case "warning":
      return "bg-warning-light text-warning-dark";
    case "urgent":
      return "bg-warning-light text-warning-dark";
    case "critical":
      return "bg-error-light text-error-dark";
    case "expired":
      return "bg-error-light text-error-dark";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

function _statusBadge(status: DriverStatus) {
  switch (status) {
    case "active":
      return "bg-success-light text-success-dark";
    case "suspended":
      return "bg-warning-light text-warning-dark";
    case "expired":
      return "bg-error-light text-error-dark";
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
      <StatsBar
        stats={[
          {
            label: "Active Drivers",
            value: dashboard.total_active.toString(),
            variant: "success",
          },
          {
            label: "Suspended",
            value: dashboard.total_suspended.toString(),
            variant: "warning",
          },
          {
            label: "Expiring Soon",
            value: dashboard.total_expiring_soon.toString(),
            variant: "error",
          },
        ]}
        className="mb-6"
      />
    );
  }

  // ─── Render: Dashboard View ──────────────────────────────────────────────

  function renderDashboard() {
    if (dashboardLoading) {
      return (
        <div role="status" className="flex justify-center py-12">
          <span className="sr-only">Loading DQF dashboard...</span>
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
        </div>
      );
    }

    if (!dashboard) return null;

    return (
      <div>
        {renderDashboardSummary()}

        <Table
          columns={[
            { key: "full_name", label: "Driver" },
            {
              key: "status",
              label: "Status",
              render: (entry: DQFDashboardEntry) => (
                <Badge
                  variant={
                    entry.status === "active"
                      ? "success"
                      : entry.status === "suspended"
                        ? "warning"
                        : "error"
                  }
                >
                  {entry.status}
                </Badge>
              ),
            },
            {
              key: "qualifications",
              label: "Qualifications",
              render: (entry: DQFDashboardEntry) => (
                <div className="flex flex-wrap gap-1">
                  {entry.qualifications.map((qual) => (
                    <Badge
                      key={qual.field}
                      variant={
                        qual.alert_level === "ok"
                          ? "success"
                          : qual.alert_level === "warning"
                            ? "warning"
                            : qual.alert_level === "urgent" ||
                                qual.alert_level === "critical" ||
                                qual.alert_level === "expired"
                              ? "error"
                              : "default"
                      }
                      size="sm"
                    >
                      {qual.field.replace(/_/g, " ")}
                    </Badge>
                  ))}
                </div>
              ),
            },
          ]}
          data={dashboard.drivers}
          getRowId={(entry) => entry.driver_id}
          onRowClick={(entry) => handleViewDetail(entry.driver_id)}
          emptyState={
            <EmptyState
              icon={<span className="text-4xl">👤</span>}
              title="No drivers found"
            />
          }
        />
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
            <Badge
              variant={
                selectedDriver.status === "active"
                  ? "success"
                  : selectedDriver.status === "suspended"
                    ? "warning"
                    : "error"
              }
            >
              {selectedDriver.status}
            </Badge>
            <Button
              variant="primary"
              size="sm"
              onClick={() => handleEditDriver(selectedDriver)}
            >
              Edit
            </Button>
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
              await updateDriver(
                editingDriver.driver_id,
                data as UpdateDriverPayload,
              );
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
        <FilterBar
          filters={
            <select
              id="driver-status-filter"
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setPage(1);
              }}
              className="px-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px]"
              aria-label="Status"
            >
              <option value="">All</option>
              <option value="active">Active</option>
              <option value="suspended">Suspended</option>
              <option value="expired">Expired</option>
            </select>
          }
        />

        {/* Loading state */}
        {loading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading drivers...</span>
            <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
          </div>
        )}

        {/* Driver table */}
        {!loading && !error && (
          <>
            <Table
              columns={[
                { key: "full_name", label: "Name" },
                { key: "cdl_number", label: "CDL Number" },
                { key: "cdl_class", label: "CDL Class" },
                {
                  key: "status",
                  label: "Status",
                  render: (driver) => (
                    <Badge
                      variant={
                        driver.status === "active"
                          ? "success"
                          : driver.status === "suspended"
                            ? "warning"
                            : "error"
                      }
                    >
                      {driver.status}
                    </Badge>
                  ),
                },
                {
                  key: "cdl_expiry_date",
                  label: "CDL Expiry",
                  render: (driver) => formatDate(driver.cdl_expiry_date),
                },
                {
                  key: "medical_card_expiry_date",
                  label: "Medical Card Expiry",
                  render: (driver) =>
                    formatDate(driver.medical_card_expiry_date),
                },
                {
                  key: "actions",
                  label: "Actions",
                  render: (driver) => (
                    <div className="flex gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleViewDetail(driver.driver_id)}
                      >
                        View
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleEditDriver(driver)}
                      >
                        Edit
                      </Button>
                    </div>
                  ),
                },
              ]}
              data={drivers}
              getRowId={(driver) => driver.driver_id}
              emptyState={
                <EmptyState
                  icon={<span className="text-4xl">👤</span>}
                  title="No drivers found"
                />
              }
            />

            <Pagination
              currentPage={page}
              totalPages={totalPages}
              onPageChange={setPage}
            />
          </>
        )}
      </>
    );
  }

  // ─── Main Render ─────────────────────────────────────────────────────────

  return (
    <div className="p-6">
      <PageHeader
        title="Driver Qualification Files"
        subtitle="Manage driver qualifications, certifications, and DQF compliance."
        actions={
          <div className="flex gap-2">
            {viewMode !== "list" &&
              viewMode !== "add" &&
              viewMode !== "edit" && (
                <Button variant="secondary" onClick={() => setViewMode("list")}>
                  Back to List
                </Button>
              )}
            {viewMode === "list" && (
              <>
                <Button
                  variant="secondary"
                  onClick={() => setViewMode("dashboard")}
                >
                  DQF Dashboard
                </Button>
                <Button variant="primary" onClick={() => setViewMode("add")}>
                  Add Driver
                </Button>
              </>
            )}
            {viewMode === "dashboard" && (
              <Button variant="secondary" onClick={() => setViewMode("list")}>
                Back to List
              </Button>
            )}
            {viewMode === "detail" && (
              <Button variant="secondary" onClick={() => setViewMode("list")}>
                Back to List
              </Button>
            )}
          </div>
        }
      />

      {/* Error state */}
      {error && (
        <div
          role="alert"
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
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

function DriverForm({
  initialData,
  onSubmit,
  onCancel,
  loading,
}: DriverFormProps) {
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
          <label
            htmlFor="cdl-number"
            className="block text-sm font-medium mb-1"
          >
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
          <label
            htmlFor="cdl-expiry"
            className="block text-sm font-medium mb-1"
          >
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
        <Button type="submit" variant="primary" loading={loading}>
          {initialData ? "Update Driver" : "Add Driver"}
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
