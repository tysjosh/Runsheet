"use client";

import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { type Column, EntityLink, Table } from "@/components/ui";
import {
  getTerminalBOLs,
  type TerminalBOL,
  type TerminalBOLStatus,
  uploadTerminalBOL,
} from "../../services/complianceApi";

// ─── View modes ──────────────────────────────────────────────────────────────

type ViewMode = "list" | "upload";

// ─── Badge helpers ───────────────────────────────────────────────────────────

function statusBadge(status: TerminalBOLStatus): {
  label: string;
  className: string;
} {
  switch (status) {
    case "ingested":
      return {
        label: "Ingested",
        className: "bg-success-light text-success-dark",
      };
    case "pending_confirmation":
      return {
        label: "Pending Confirmation",
        className: "bg-warning-light text-warning-dark",
      };
    case "linked":
      return { label: "Linked", className: "bg-info-light text-info-dark" };
    default:
      return { label: status, className: "bg-gray-100 text-gray-800" };
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString();
}

function formatGallons(gallons: number): string {
  return gallons.toFixed(1);
}

// ─── Table columns ───────────────────────────────────────────────────────────

const bolColumns: Column<TerminalBOL>[] = [
  {
    key: "load_number",
    label: "Load Number",
    render: (bol) => <span className="font-medium">{bol.load_number}</span>,
  },
  {
    key: "product_code",
    label: "Product Code",
    render: (bol) => bol.product_code,
  },
  {
    key: "gross_gallons",
    label: "Gross Gallons",
    render: (bol) => formatGallons(bol.gross_gallons),
  },
  {
    key: "net_gallons",
    label: "Net Gallons",
    render: (bol) => formatGallons(bol.net_gallons),
  },
  {
    key: "supplier_name",
    label: "Supplier",
    render: (bol) => bol.supplier_name,
  },
  {
    key: "terminal_name",
    label: "Terminal",
    render: (bol) => bol.terminal_name,
  },
  {
    key: "driver_id",
    label: "Driver ID",
    // The terminal BOL's subject is its driver, navigable to the Drivers
    // module (Req 11.3, 13.1).
    render: (bol) => <EntityLink type="driver" id={bol.driver_id} />,
  },
  {
    key: "timestamp",
    label: "Timestamp",
    render: (bol) => formatDate(bol.timestamp),
  },
  {
    key: "status",
    label: "Status",
    render: (bol) => {
      const badge = statusBadge(bol.status);
      return (
        <span
          className={`inline-block px-2 py-1 rounded text-xs font-medium ${badge.className}`}
        >
          {badge.label}
        </span>
      );
    },
  },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function TerminalBOLsPage() {
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [bols, setBols] = useState<TerminalBOL[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);

  // Filters
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [productCodeFilter, setProductCodeFilter] = useState<string>("");
  const [driverIdFilter, setDriverIdFilter] = useState<string>("");

  // Upload state
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ─── Fetch BOLs ──────────────────────────────────────────────────────────

  const fetchBOLs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: {
        status?: TerminalBOLStatus;
        product_code?: string;
        driver_id?: string;
        page: number;
        size: number;
      } = {
        page,
        size: 20,
      };
      if (statusFilter) filters.status = statusFilter as TerminalBOLStatus;
      if (productCodeFilter) filters.product_code = productCodeFilter;
      if (driverIdFilter) filters.driver_id = driverIdFilter;

      const response = await getTerminalBOLs(filters);
      setBols(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load terminal BOLs",
      );
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter, productCodeFilter, driverIdFilter]);

  useEffect(() => {
    if (viewMode === "list") {
      fetchBOLs();
    }
  }, [fetchBOLs, viewMode]);

  // ─── Upload handler ──────────────────────────────────────────────────────

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!uploadFile) return;

    setUploading(true);
    setUploadError(null);
    setUploadSuccess(false);

    try {
      await uploadTerminalBOL(uploadFile);
      setUploadSuccess(true);
      setUploadFile(null);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      // Return to list after short delay
      setTimeout(() => {
        setViewMode("list");
        setUploadSuccess(false);
      }, 1500);
    } catch (err) {
      setUploadError(
        err instanceof Error ? err.message : "Failed to upload BOL",
      );
    } finally {
      setUploading(false);
    }
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0] ?? null;
    setUploadFile(file);
    setUploadError(null);
    setUploadSuccess(false);
  }

  // ─── Render: Listing View ────────────────────────────────────────────────

  function renderList() {
    return (
      <>
        {/* Filters */}
        <div className="flex flex-wrap gap-4 mb-6 items-end">
          <div>
            <label
              htmlFor="status-filter"
              className="block text-sm font-medium mb-1"
            >
              Status
            </label>
            <select
              id="status-filter"
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2 w-48"
            >
              <option value="">All Statuses</option>
              <option value="ingested">Ingested</option>
              <option value="pending_confirmation">Pending Confirmation</option>
              <option value="linked">Linked</option>
            </select>
          </div>
          <div>
            <label
              htmlFor="product-code-filter"
              className="block text-sm font-medium mb-1"
            >
              Product Code
            </label>
            <input
              id="product-code-filter"
              type="text"
              value={productCodeFilter}
              onChange={(e) => {
                setProductCodeFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2 w-48"
              placeholder="Filter by product..."
            />
          </div>
          <div>
            <label
              htmlFor="driver-id-filter"
              className="block text-sm font-medium mb-1"
            >
              Driver ID
            </label>
            <input
              id="driver-id-filter"
              type="text"
              value={driverIdFilter}
              onChange={(e) => {
                setDriverIdFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2 w-48"
              placeholder="Filter by driver..."
            />
          </div>
        </div>

        {/* Loading state */}
        {loading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading terminal BOLs...</span>
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

        {/* BOLs table */}
        {!loading && !error && (
          <>
            <Table<TerminalBOL>
              ariaLabel="Terminal BOLs"
              columns={bolColumns}
              data={bols}
              getRowId={(bol) => bol.bol_id}
              emptyState={
                <span className="text-gray-500">No terminal BOLs found.</span>
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

  // ─── Render: Upload Form ─────────────────────────────────────────────────

  function renderUploadForm() {
    return (
      <form
        onSubmit={handleUpload}
        className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-2xl"
      >
        <h2 className="text-lg font-bold mb-4">Upload Terminal BOL</h2>
        <p className="text-gray-600 text-sm mb-4">
          Upload a scanned BOL document (PDF or image). The system will extract
          data via OCR and create a pending confirmation record.
        </p>

        <div className="mb-4">
          <label htmlFor="bol-file" className="block text-sm font-medium mb-1">
            BOL Document
          </label>
          <input
            id="bol-file"
            ref={fileInputRef}
            type="file"
            accept=".pdf,.png,.jpg,.jpeg,.tiff,.tif"
            onChange={handleFileChange}
            className="w-full border rounded px-3 py-2"
          />
          <p className="text-xs text-gray-500 mt-1">
            Accepted formats: PDF, PNG, JPG, TIFF
          </p>
        </div>

        {/* Upload error */}
        {uploadError && (
          <div
            role="alert"
            className="bg-error-light border border-error-light text-error-dark p-3 rounded mb-4 text-sm"
          >
            {uploadError}
          </div>
        )}

        {/* Upload success */}
        {uploadSuccess && (
          <div
            role="status"
            className="bg-success-light border border-success-light text-success-dark p-3 rounded mb-4 text-sm"
          >
            BOL uploaded successfully. Returning to list...
          </div>
        )}

        <div className="flex gap-3">
          <button
            type="submit"
            disabled={uploading || !uploadFile}
            className="bg-primary text-white px-4 py-2 rounded hover:bg-primary-hover disabled:opacity-50"
          >
            {uploading ? "Uploading..." : "Upload BOL"}
          </button>
          <button
            type="button"
            onClick={() => {
              setViewMode("list");
              setUploadFile(null);
              setUploadError(null);
              setUploadSuccess(false);
            }}
            className="px-4 py-2 border rounded hover:bg-gray-50"
          >
            Cancel
          </button>
        </div>
      </form>
    );
  }

  // ─── Main Render ─────────────────────────────────────────────────────────

  return (
    <div className="p-6">
      <header className="mb-6">
        <div className="flex justify-between items-center">
          <div>
            <h1 className="text-2xl font-bold">Terminal BOLs</h1>
            <p className="text-gray-600 mt-1">
              View ingested terminal Bills of Lading and manually upload scanned
              BOL documents.
            </p>
          </div>
          <div className="flex gap-2">
            {viewMode !== "list" && (
              <button
                type="button"
                onClick={() => {
                  setViewMode("list");
                  setUploadFile(null);
                  setUploadError(null);
                  setUploadSuccess(false);
                }}
                className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
              >
                Back to List
              </button>
            )}
            {viewMode === "list" && (
              <button
                type="button"
                onClick={() => setViewMode("upload")}
                className="bg-primary text-white px-4 py-2 rounded text-sm hover:bg-primary-hover"
              >
                Upload BOL
              </button>
            )}
          </div>
        </div>
      </header>

      {/* View content */}
      {viewMode === "list" && renderList()}
      {viewMode === "upload" && renderUploadForm()}
    </div>
  );
}
