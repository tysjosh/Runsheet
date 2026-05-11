"use client";

import type React from "react";
import { useCallback, useEffect, useState } from "react";
import {
  type CreateTaxExemptionPayload,
  createTaxExemption,
  getTaxExemptions,
  type TaxExemption,
} from "../../services/complianceApi";

// ─── Sub-view types ──────────────────────────────────────────────────────────

type ViewMode = "list" | "add";

// ─── Expiry status helpers ───────────────────────────────────────────────────

type ExpiryStatus = "active" | "expiring_soon" | "expired";

function getExpiryStatus(expiryDate: string): ExpiryStatus {
  const now = new Date();
  const expiry = new Date(expiryDate);
  const diffMs = expiry.getTime() - now.getTime();
  const diffDays = Math.ceil(diffMs / (1000 * 60 * 60 * 24));

  if (diffDays < 0) return "expired";
  if (diffDays <= 30) return "expiring_soon";
  return "active";
}

function expiryStatusBadge(status: ExpiryStatus): string {
  switch (status) {
    case "active":
      return "bg-success-light text-success-dark";
    case "expiring_soon":
      return "bg-warning-light text-warning-dark";
    case "expired":
      return "bg-error-light text-error-dark";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

function expiryStatusLabel(status: ExpiryStatus): string {
  switch (status) {
    case "active":
      return "Active";
    case "expiring_soon":
      return "Expiring Soon";
    case "expired":
      return "Expired";
    default:
      return "Unknown";
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString();
}

// ─── Exemption type options ──────────────────────────────────────────────────

const EXEMPTION_TYPES = [
  { value: "dyed_diesel", label: "Dyed Diesel (IRS 637M)" },
  { value: "farm_agricultural", label: "Farm / Agricultural" },
  { value: "road_use", label: "Road-Use Exemption" },
  { value: "government", label: "Government Entity" },
  { value: "nonprofit", label: "Nonprofit Organization" },
];

function getExemptionTypeLabel(type: string): string {
  const found = EXEMPTION_TYPES.find((t) => t.value === type);
  return found ? found.label : type;
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function ExemptionsPage() {
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [exemptions, setExemptions] = useState<TaxExemption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [typeFilter, setTypeFilter] = useState<string>("");

  // ─── Fetch exemptions list ───────────────────────────────────────────────

  const fetchExemptions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: { exemption_type?: string; page: number; size: number } = {
        page,
        size: 20,
      };
      if (typeFilter) filters.exemption_type = typeFilter;

      const response = await getTaxExemptions(filters);
      setExemptions(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load exemptions",
      );
    } finally {
      setLoading(false);
    }
  }, [page, typeFilter]);

  useEffect(() => {
    if (viewMode === "list") {
      fetchExemptions();
    }
  }, [fetchExemptions, viewMode]);

  // ─── Render: Add Exemption Form ─────────────────────────────────────────

  function renderForm() {
    return (
      <ExemptionForm
        onSubmit={async (data) => {
          setLoading(true);
          setError(null);
          try {
            await createTaxExemption(data);
            setViewMode("list");
          } catch (err) {
            setError(
              err instanceof Error ? err.message : "Failed to create exemption",
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
              htmlFor="exemption-type-filter"
              className="block text-sm font-medium mb-1"
            >
              Exemption Type
            </label>
            <select
              id="exemption-type-filter"
              value={typeFilter}
              onChange={(e) => {
                setTypeFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2"
            >
              <option value="">All Types</option>
              {EXEMPTION_TYPES.map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Loading state */}
        {loading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading exemptions...</span>
            <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
          </div>
        )}

        {/* Exemptions table */}
        {!loading && !error && (
          <>
            <table className="w-full border-collapse" role="table">
              <thead>
                <tr className="border-b bg-gray-50">
                  <th className="text-left p-3 font-medium">Customer ID</th>
                  <th className="text-left p-3 font-medium">Exemption Type</th>
                  <th className="text-left p-3 font-medium">
                    Certificate Number
                  </th>
                  <th className="text-left p-3 font-medium">Expiry Date</th>
                  <th className="text-left p-3 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {exemptions.map((exemption) => {
                  const status = getExpiryStatus(exemption.expiry_date);
                  return (
                    <tr
                      key={exemption.exemption_id}
                      className="border-b hover:bg-gray-50"
                    >
                      <td className="p-3 font-medium">
                        {exemption.customer_id}
                      </td>
                      <td className="p-3">
                        {getExemptionTypeLabel(exemption.exemption_type)}
                      </td>
                      <td className="p-3 font-mono text-sm">
                        {exemption.certificate_number}
                      </td>
                      <td className="p-3">
                        {formatDate(exemption.expiry_date)}
                      </td>
                      <td className="p-3">
                        <span
                          className={`inline-block px-2 py-1 rounded text-xs font-medium ${expiryStatusBadge(status)}`}
                        >
                          {expiryStatusLabel(status)}
                        </span>
                      </td>
                    </tr>
                  );
                })}
                {exemptions.length === 0 && (
                  <tr>
                    <td colSpan={5} className="p-6 text-center text-gray-500">
                      No exemption certificates found.
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
            <h1 className="text-2xl font-bold">Tax Exemption Certificates</h1>
            <p className="text-gray-600 mt-1">
              Manage customer tax exemption certificates for dyed diesel, farm,
              and road-use exemptions.
            </p>
          </div>
          <div className="flex gap-2">
            {viewMode === "add" && (
              <button
                type="button"
                onClick={() => setViewMode("list")}
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
                Add Exemption
              </button>
            )}
          </div>
        </div>
      </header>

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
      {viewMode === "add" && renderForm()}
    </div>
  );
}

// ─── Exemption Form Sub-Component ────────────────────────────────────────────

interface ExemptionFormProps {
  onSubmit: (data: CreateTaxExemptionPayload) => Promise<void>;
  onCancel: () => void;
  loading: boolean;
}

function ExemptionForm({ onSubmit, onCancel, loading }: ExemptionFormProps) {
  const [customerId, setCustomerId] = useState("");
  const [exemptionType, setExemptionType] = useState("");
  const [certificateNumber, setCertificateNumber] = useState("");
  const [expiryDate, setExpiryDate] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const data: CreateTaxExemptionPayload = {
      customer_id: customerId,
      exemption_type: exemptionType,
      certificate_number: certificateNumber,
      expiry_date: expiryDate,
    };
    await onSubmit(data);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-2xl"
    >
      <h2 className="text-lg font-bold mb-4">Add New Exemption Certificate</h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label
            htmlFor="customer-id"
            className="block text-sm font-medium mb-1"
          >
            Customer ID
          </label>
          <input
            id="customer-id"
            type="text"
            required
            value={customerId}
            onChange={(e) => setCustomerId(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g. CUST-001"
          />
        </div>
        <div>
          <label
            htmlFor="exemption-type"
            className="block text-sm font-medium mb-1"
          >
            Exemption Type
          </label>
          <select
            id="exemption-type"
            required
            value={exemptionType}
            onChange={(e) => setExemptionType(e.target.value)}
            className="w-full border rounded px-3 py-2"
          >
            <option value="">Select type...</option>
            {EXEMPTION_TYPES.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label
            htmlFor="certificate-number"
            className="block text-sm font-medium mb-1"
          >
            Certificate Number
          </label>
          <input
            id="certificate-number"
            type="text"
            required
            value={certificateNumber}
            onChange={(e) => setCertificateNumber(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g. 637M-12345"
          />
        </div>
        <div>
          <label
            htmlFor="expiry-date"
            className="block text-sm font-medium mb-1"
          >
            Expiry Date
          </label>
          <input
            id="expiry-date"
            type="date"
            required
            value={expiryDate}
            onChange={(e) => setExpiryDate(e.target.value)}
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
          {loading ? "Saving..." : "Add Exemption"}
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
