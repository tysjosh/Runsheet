"use client";

/**
 * CargoSearchSection — Search cargo items across all jobs.
 *
 * Provides a search form with container_number, description, and item_status fields.
 * Submit calls searchCargo from schedulingApi.ts and displays paginated results
 * with the associated job_id.
 *
 * Validates:
 * - Requirement 7.4: Search form with container_number, description, item_status fields
 * - Requirement 7.5: Calls searchCargo API and displays paginated results with job_id
 */

import { Loader2, Package, Search } from "lucide-react";
import { useCallback, useState } from "react";
import { type Column, Table } from "@/components/ui";
import {
  type CargoSearchFilters,
  type PaginationMeta,
  searchCargo,
} from "../../services/schedulingApi";
import type { CargoItemStatus, SchedulingCargoItem } from "../../types/api";

// ─── Types ───────────────────────────────────────────────────────────────────

type CargoSearchResult = SchedulingCargoItem & { job_id: string };

const CARGO_STATUS_OPTIONS: { value: CargoItemStatus | ""; label: string }[] = [
  { value: "", label: "All Statuses" },
  { value: "pending", label: "Pending" },
  { value: "loaded", label: "Loaded" },
  { value: "in_transit", label: "In Transit" },
  { value: "delivered", label: "Delivered" },
  { value: "damaged", label: "Damaged" },
];

const PAGE_SIZE = 10;

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getStatusBadge(status: CargoItemStatus): string {
  switch (status) {
    case "pending":
      return "text-gray-700 bg-gray-100";
    case "loaded":
      return "text-info-dark bg-info-light";
    case "in_transit":
      return "text-warning-dark bg-warning-light";
    case "delivered":
      return "text-success-dark bg-success-light";
    case "damaged":
      return "text-error-dark bg-error-light";
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

// ─── Table columns ───────────────────────────────────────────────────────────

const cargoSearchColumns: Column<CargoSearchResult>[] = [
  {
    key: "item_id",
    label: "Item ID",
    className: "text-sm font-medium text-primary",
    render: (item) => item.item_id,
  },
  {
    key: "description",
    label: "Description",
    className: "text-sm text-gray-700",
    render: (item) => item.description,
  },
  {
    key: "container_number",
    label: "Container",
    className: "text-sm text-gray-700",
    render: (item) => item.container_number || "—",
  },
  {
    key: "item_status",
    label: "Status",
    render: (item) => (
      <span
        className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-xs font-medium ${getStatusBadge(item.item_status)}`}
      >
        {formatStatus(item.item_status)}
      </span>
    ),
  },
  {
    key: "weight_kg",
    label: "Weight (kg)",
    className: "text-sm text-gray-700",
    render: (item) => item.weight_kg,
  },
  {
    key: "job_id",
    label: "Job ID",
    className: "text-sm font-mono text-gray-600",
    render: (item) => item.job_id,
  },
];

// ─── Component ───────────────────────────────────────────────────────────────

export default function CargoSearchSection() {
  // Form state
  const [containerNumber, setContainerNumber] = useState("");
  const [description, setDescription] = useState("");
  const [itemStatus, setItemStatus] = useState<CargoItemStatus | "">("");

  // Results state
  const [results, setResults] = useState<CargoSearchResult[]>([]);
  const [pagination, setPagination] = useState<PaginationMeta | null>(null);
  const [currentPage, setCurrentPage] = useState(1);

  // UI state
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [hasSearched, setHasSearched] = useState(false);

  // ── Search handler ─────────────────────────────────────────────────────

  const executeSearch = useCallback(
    async (page: number) => {
      setError("");
      setLoading(true);
      try {
        const filters: CargoSearchFilters = {
          page,
          size: PAGE_SIZE,
        };
        if (containerNumber.trim()) {
          filters.container_number = containerNumber.trim();
        }
        if (description.trim()) {
          filters.description = description.trim();
        }
        if (itemStatus) {
          filters.item_status = itemStatus;
        }

        const res = await searchCargo(filters);
        setResults(res.data);
        setPagination(res.pagination);
        setCurrentPage(page);
        setHasSearched(true);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to search cargo items",
        );
      } finally {
        setLoading(false);
      }
    },
    [containerNumber, description, itemStatus],
  );

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    executeSearch(1);
  };

  const handlePageChange = (page: number) => {
    executeSearch(page);
  };

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div className="bg-white rounded-lg border border-gray-200">
      {/* Header */}
      <div className="px-6 py-4 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <Search className="w-5 h-5 text-gray-500" />
          <h2 className="text-base font-semibold text-primary">Cargo Search</h2>
        </div>
        <p className="text-sm text-gray-500 mt-1">
          Search cargo items across all jobs by container number, description,
          or status.
        </p>
      </div>

      {/* Search Form */}
      <form
        onSubmit={handleSubmit}
        className="px-6 py-4 border-b border-gray-100"
      >
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          {/* Container Number */}
          <div>
            <label
              htmlFor="cargo-search-container"
              className="block text-sm font-medium text-gray-600 mb-1"
            >
              Container Number
            </label>
            <input
              id="cargo-search-container"
              type="text"
              value={containerNumber}
              onChange={(e) => setContainerNumber(e.target.value)}
              placeholder="e.g. CNTR-001"
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white text-primary placeholder-gray-400"
            />
          </div>

          {/* Description */}
          <div>
            <label
              htmlFor="cargo-search-description"
              className="block text-sm font-medium text-gray-600 mb-1"
            >
              Description
            </label>
            <input
              id="cargo-search-description"
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="e.g. fuel drums"
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white text-primary placeholder-gray-400"
            />
          </div>

          {/* Item Status */}
          <div>
            <label
              htmlFor="cargo-search-status"
              className="block text-sm font-medium text-gray-600 mb-1"
            >
              Item Status
            </label>
            <select
              id="cargo-search-status"
              value={itemStatus}
              onChange={(e) =>
                setItemStatus(e.target.value as CargoItemStatus | "")
              }
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white text-primary"
            >
              {CARGO_STATUS_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Search Button */}
        <div className="mt-4 flex items-center gap-3">
          <button
            type="submit"
            disabled={loading}
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white rounded-lg disabled:opacity-50 transition-colors bg-primary hover:bg-primary-hover"
          >
            {loading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Search className="w-4 h-4" />
            )}
            {loading ? "Searching..." : "Search"}
          </button>
        </div>
      </form>

      {/* Error */}
      {error && (
        <div className="mx-6 mt-4">
          <p className="text-sm text-error bg-error-light px-3 py-2 rounded-lg">
            {error}
          </p>
        </div>
      )}

      {/* Results */}
      {hasSearched && !error && (
        <div className="px-6 py-4">
          {results.length === 0 ? (
            <div className="text-center py-12">
              <Package className="w-10 h-10 text-gray-300 mx-auto mb-3" />
              <p className="text-sm font-medium text-gray-400">
                No cargo items found
              </p>
              <p className="text-xs text-gray-400 mt-1">
                Try adjusting your search criteria
              </p>
            </div>
          ) : (
            <>
              {/* Results count */}
              {pagination && (
                <p className="text-sm text-gray-500 mb-3">
                  Showing {(pagination.page - 1) * pagination.size + 1}–
                  {Math.min(
                    pagination.page * pagination.size,
                    pagination.total,
                  )}{" "}
                  of {pagination.total} results
                </p>
              )}

              {/* Results Table */}
              <Table<CargoSearchResult>
                ariaLabel="Cargo search results"
                className="border border-gray-200 rounded-lg"
                columns={cargoSearchColumns}
                data={results}
                getRowId={(item) => `${item.job_id}-${item.item_id}`}
                emptyState={
                  <span className="text-gray-500">No cargo items found.</span>
                }
              />

              {/* Pagination */}
              {pagination && pagination.total_pages > 1 && (
                <div className="flex items-center justify-between mt-4">
                  <p className="text-sm text-gray-500">
                    Page {pagination.page} of {pagination.total_pages}
                  </p>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => handlePageChange(currentPage - 1)}
                      disabled={currentPage <= 1 || loading}
                      className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      Previous
                    </button>
                    <button
                      onClick={() => handlePageChange(currentPage + 1)}
                      disabled={
                        currentPage >= pagination.total_pages || loading
                      }
                      className="px-3 py-1.5 text-sm border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
