"use client";

import type React from "react";
import { useCallback, useEffect, useState } from "react";
import {
  type CreateJurisdictionRatePayload,
  createTaxJurisdiction,
  getTaxJurisdictions,
  type JurisdictionRate,
} from "../../services/complianceApi";

// ─── Sub-view types ──────────────────────────────────────────────────────────

type ViewMode = "list" | "add" | "import";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString();
}

function jurisdictionLevelBadge(level: JurisdictionRate["jurisdiction_level"]) {
  switch (level) {
    case "federal":
      return "bg-info-light text-info-dark";
    case "state":
      return "bg-brand-secondary-soft text-brand-secondary";
    case "county":
      return "bg-warning-light text-warning-dark";
    case "city":
      return "bg-success-light text-success-dark";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

function taxTypeBadge(taxType: JurisdictionRate["tax_type"]) {
  switch (taxType) {
    case "excise":
      return "bg-error-light text-error-dark";
    case "ust":
      return "bg-warning-light text-warning-dark";
    case "spcc":
      return "bg-teal-100 text-teal-800";
    case "environmental":
      return "bg-success-light text-success-dark";
    default:
      return "bg-gray-100 text-gray-800";
  }
}

// ─── CSV Parsing ─────────────────────────────────────────────────────────────

interface CSVParseResult {
  rows: CreateJurisdictionRatePayload[];
  errors: string[];
}

function parseCSV(csvText: string): CSVParseResult {
  const lines = csvText.trim().split("\n");
  if (lines.length < 2) {
    return {
      rows: [],
      errors: ["CSV must have a header row and at least one data row."],
    };
  }

  const header = lines[0].split(",").map((h) => h.trim().toLowerCase());
  const requiredColumns = [
    "fips_code",
    "jurisdiction_level",
    "tax_type",
    "product_codes",
    "rate_cents_per_gallon",
    "effective_date",
  ];

  const missingColumns = requiredColumns.filter((col) => !header.includes(col));
  if (missingColumns.length > 0) {
    return {
      rows: [],
      errors: [`Missing required columns: ${missingColumns.join(", ")}`],
    };
  }

  const rows: CreateJurisdictionRatePayload[] = [];
  const errors: string[] = [];

  for (let i = 1; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;

    const values = line.split(",").map((v) => v.trim());
    if (values.length < header.length) {
      errors.push(`Row ${i + 1}: insufficient columns`);
      continue;
    }

    const getVal = (col: string) => values[header.indexOf(col)] ?? "";

    const jurisdictionLevel = getVal("jurisdiction_level");
    const taxType = getVal("tax_type");
    const rateCents = parseInt(getVal("rate_cents_per_gallon"), 10);

    if (!["federal", "state", "county", "city"].includes(jurisdictionLevel)) {
      errors.push(
        `Row ${i + 1}: invalid jurisdiction_level "${jurisdictionLevel}"`,
      );
      continue;
    }
    if (!["excise", "ust", "spcc", "environmental"].includes(taxType)) {
      errors.push(`Row ${i + 1}: invalid tax_type "${taxType}"`);
      continue;
    }
    if (Number.isNaN(rateCents)) {
      errors.push(`Row ${i + 1}: invalid rate_cents_per_gallon`);
      continue;
    }

    const productCodesRaw = getVal("product_codes");
    const productCodes = productCodesRaw
      ? productCodesRaw
          .split(";")
          .map((c) => c.trim())
          .filter(Boolean)
      : [];

    rows.push({
      fips_code: getVal("fips_code"),
      jurisdiction_level:
        jurisdictionLevel as CreateJurisdictionRatePayload["jurisdiction_level"],
      tax_type: taxType as CreateJurisdictionRatePayload["tax_type"],
      product_codes: productCodes,
      rate_cents_per_gallon: rateCents,
      effective_date: getVal("effective_date"),
      expiry_date: getVal("expiry_date") || null,
    });
  }

  return { rows, errors };
}

// ─── Main Component ──────────────────────────────────────────────────────────

export default function TaxJurisdictionsPage() {
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [jurisdictions, setJurisdictions] = useState<JurisdictionRate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);

  // Filters
  const [jurisdictionLevelFilter, setJurisdictionLevelFilter] =
    useState<string>("");
  const [taxTypeFilter, setTaxTypeFilter] = useState<string>("");

  // CSV import state
  const [importResult, setImportResult] = useState<{
    success: number;
    failed: number;
    errors: string[];
  } | null>(null);
  const [importing, setImporting] = useState(false);

  // ─── Fetch jurisdictions list ────────────────────────────────────────────

  const fetchJurisdictions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: Record<string, string | number | undefined> = {
        page,
        size: 20,
      };
      if (jurisdictionLevelFilter)
        filters.jurisdiction_level = jurisdictionLevelFilter;
      if (taxTypeFilter) filters.tax_type = taxTypeFilter;

      const response = await getTaxJurisdictions(
        filters as Parameters<typeof getTaxJurisdictions>[0],
      );
      setJurisdictions(response.data ?? []);
      setTotalPages(response.pagination?.total_pages ?? 1);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load tax jurisdictions",
      );
    } finally {
      setLoading(false);
    }
  }, [page, jurisdictionLevelFilter, taxTypeFilter]);

  useEffect(() => {
    if (viewMode === "list") {
      fetchJurisdictions();
    }
  }, [fetchJurisdictions, viewMode]);

  // ─── CSV Import Handler ──────────────────────────────────────────────────

  const handleCSVImport = async (file: File) => {
    setImporting(true);
    setImportResult(null);
    setError(null);

    try {
      const text = await file.text();
      const { rows, errors: parseErrors } = parseCSV(text);

      if (parseErrors.length > 0 && rows.length === 0) {
        setImportResult({ success: 0, failed: 0, errors: parseErrors });
        setImporting(false);
        return;
      }

      let success = 0;
      let failed = 0;
      const importErrors: string[] = [...parseErrors];

      for (const row of rows) {
        try {
          await createTaxJurisdiction(row);
          success++;
        } catch (err) {
          failed++;
          importErrors.push(
            `Failed to import FIPS ${row.fips_code} (${row.tax_type}): ${err instanceof Error ? err.message : "Unknown error"}`,
          );
        }
      }

      setImportResult({ success, failed, errors: importErrors });

      if (success > 0) {
        fetchJurisdictions();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to read CSV file");
    } finally {
      setImporting(false);
    }
  };

  // ─── Render: CSV Import View ─────────────────────────────────────────────

  function renderImport() {
    return (
      <div className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-2xl">
        <h2 className="text-lg font-bold mb-4">
          Import Tax Jurisdictions from CSV
        </h2>

        <div className="mb-4">
          <p className="text-sm text-gray-600 mb-2">
            Upload a CSV file with the following columns:
          </p>
          <code className="block bg-gray-50 p-3 rounded text-xs text-gray-700">
            fips_code, jurisdiction_level, tax_type, product_codes,
            rate_cents_per_gallon, effective_date, expiry_date
          </code>
          <p className="text-xs text-gray-500 mt-2">
            • <strong>jurisdiction_level:</strong> federal, state, county, or
            city
            <br />• <strong>tax_type:</strong> excise, ust, spcc, or
            environmental
            <br />• <strong>product_codes:</strong> semicolon-separated (e.g.,
            UNL;DSL)
            <br />• <strong>expiry_date:</strong> optional
          </p>
        </div>

        <div className="mb-4">
          <label
            htmlFor="csv-file-input"
            className="block text-sm font-medium mb-1"
          >
            Select CSV File
          </label>
          <input
            id="csv-file-input"
            type="file"
            accept=".csv,text/csv"
            disabled={importing}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) {
                handleCSVImport(file);
              }
            }}
            className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded file:border-0 file:text-sm file:font-medium file:bg-info-light file:text-info-dark hover:file:bg-info-light"
          />
        </div>

        {importing && (
          <div role="status" className="flex items-center gap-2 py-4">
            <div className="animate-spin h-5 w-5 border-2 border-primary border-t-transparent rounded-full" />
            <span className="text-sm text-gray-600">Importing...</span>
          </div>
        )}

        {importResult && (
          <div className="mt-4">
            <div className="flex gap-4 mb-3">
              <div className="bg-success-light border border-success-light rounded px-3 py-2">
                <span className="text-sm font-medium text-success-dark">
                  ✓ {importResult.success} imported
                </span>
              </div>
              {importResult.failed > 0 && (
                <div className="bg-error-light border border-error-light rounded px-3 py-2">
                  <span className="text-sm font-medium text-error-dark">
                    ✗ {importResult.failed} failed
                  </span>
                </div>
              )}
            </div>
            {importResult.errors.length > 0 && (
              <div className="bg-warning-light border border-warning-light rounded p-3">
                <p className="text-sm font-medium text-warning-dark mb-1">
                  Errors:
                </p>
                <ul className="text-xs text-warning-dark list-disc list-inside max-h-40 overflow-y-auto">
                  {importResult.errors.map((err, idx) => (
                    <li key={idx}>{err}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        <div className="mt-6">
          <button
            type="button"
            onClick={() => {
              setViewMode("list");
              setImportResult(null);
            }}
            className="px-4 py-2 border rounded hover:bg-gray-50"
          >
            Back to List
          </button>
        </div>
      </div>
    );
  }

  // ─── Render: Add Rate Form ───────────────────────────────────────────────

  function renderAddForm() {
    return (
      <AddRateForm
        onSubmit={async (data) => {
          setLoading(true);
          setError(null);
          try {
            await createTaxJurisdiction(data);
            setViewMode("list");
          } catch (err) {
            setError(
              err instanceof Error
                ? err.message
                : "Failed to create jurisdiction rate",
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
        <div className="flex gap-4 mb-6 items-end flex-wrap">
          <div>
            <label
              htmlFor="jurisdiction-level-filter"
              className="block text-sm font-medium mb-1"
            >
              Jurisdiction Level
            </label>
            <select
              id="jurisdiction-level-filter"
              value={jurisdictionLevelFilter}
              onChange={(e) => {
                setJurisdictionLevelFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2"
            >
              <option value="">All</option>
              <option value="federal">Federal</option>
              <option value="state">State</option>
              <option value="county">County</option>
              <option value="city">City</option>
            </select>
          </div>
          <div>
            <label
              htmlFor="tax-type-filter"
              className="block text-sm font-medium mb-1"
            >
              Tax Type
            </label>
            <select
              id="tax-type-filter"
              value={taxTypeFilter}
              onChange={(e) => {
                setTaxTypeFilter(e.target.value);
                setPage(1);
              }}
              className="border rounded px-3 py-2"
            >
              <option value="">All</option>
              <option value="excise">Excise</option>
              <option value="ust">UST</option>
              <option value="spcc">SPCC</option>
              <option value="environmental">Environmental</option>
            </select>
          </div>
        </div>

        {/* Loading state */}
        {loading && (
          <div role="status" className="flex justify-center py-12">
            <span className="sr-only">Loading tax jurisdictions...</span>
            <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
          </div>
        )}

        {/* Jurisdictions table */}
        {!loading && !error && (
          <>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse" role="table">
                <thead>
                  <tr className="border-b bg-gray-50">
                    <th className="text-left p-3 font-medium">FIPS Code</th>
                    <th className="text-left p-3 font-medium">Level</th>
                    <th className="text-left p-3 font-medium">Tax Type</th>
                    <th className="text-left p-3 font-medium">Product Codes</th>
                    <th className="text-left p-3 font-medium">Rate (¢/gal)</th>
                    <th className="text-left p-3 font-medium">
                      Effective Date
                    </th>
                    <th className="text-left p-3 font-medium">Expiry Date</th>
                  </tr>
                </thead>
                <tbody>
                  {jurisdictions.map((rate) => (
                    <tr
                      key={rate.jurisdiction_id}
                      className="border-b hover:bg-gray-50"
                    >
                      <td className="p-3 font-mono text-sm">
                        {rate.fips_code}
                      </td>
                      <td className="p-3">
                        <span
                          className={`inline-block px-2 py-1 rounded text-xs font-medium ${jurisdictionLevelBadge(rate.jurisdiction_level)}`}
                        >
                          {rate.jurisdiction_level}
                        </span>
                      </td>
                      <td className="p-3">
                        <span
                          className={`inline-block px-2 py-1 rounded text-xs font-medium ${taxTypeBadge(rate.tax_type)}`}
                        >
                          {rate.tax_type}
                        </span>
                      </td>
                      <td className="p-3 text-sm">
                        {rate.product_codes.join(", ") || "—"}
                      </td>
                      <td className="p-3 font-medium">
                        {rate.rate_cents_per_gallon}¢
                      </td>
                      <td className="p-3 text-sm">
                        {formatDate(rate.effective_date)}
                      </td>
                      <td className="p-3 text-sm">
                        {formatDate(rate.expiry_date)}
                      </td>
                    </tr>
                  ))}
                  {jurisdictions.length === 0 && (
                    <tr>
                      <td colSpan={7} className="p-6 text-center text-gray-500">
                        No tax jurisdiction rates found.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

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
            <h1 className="text-2xl font-bold">Tax Jurisdictions</h1>
            <p className="text-gray-600 mt-1">
              Manage fuel tax rates by jurisdiction level, FIPS code, and tax
              type.
            </p>
          </div>
          <div className="flex gap-2">
            {viewMode === "list" && (
              <>
                <button
                  type="button"
                  onClick={() => setViewMode("import")}
                  className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
                >
                  Import CSV
                </button>
                <button
                  type="button"
                  onClick={() => setViewMode("add")}
                  className="bg-primary text-white px-4 py-2 rounded text-sm hover:bg-primary-hover"
                >
                  Add Rate
                </button>
              </>
            )}
            {(viewMode === "import" || viewMode === "add") && (
              <button
                type="button"
                onClick={() => {
                  setViewMode("list");
                  setImportResult(null);
                }}
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
          className="bg-error-light border border-error-light text-error-dark p-4 rounded mb-4"
        >
          {error}
        </div>
      )}

      {/* View content */}
      {viewMode === "list" && renderList()}
      {viewMode === "add" && renderAddForm()}
      {viewMode === "import" && renderImport()}
    </div>
  );
}

// ─── Add Rate Form Sub-Component ─────────────────────────────────────────────

interface AddRateFormProps {
  onSubmit: (data: CreateJurisdictionRatePayload) => Promise<void>;
  onCancel: () => void;
  loading: boolean;
}

function AddRateForm({ onSubmit, onCancel, loading }: AddRateFormProps) {
  const [fipsCode, setFipsCode] = useState("");
  const [jurisdictionLevel, setJurisdictionLevel] =
    useState<CreateJurisdictionRatePayload["jurisdiction_level"]>("state");
  const [taxType, setTaxType] =
    useState<CreateJurisdictionRatePayload["tax_type"]>("excise");
  const [productCodes, setProductCodes] = useState("");
  const [rateCentsPerGallon, setRateCentsPerGallon] = useState("");
  const [effectiveDate, setEffectiveDate] = useState("");
  const [expiryDate, setExpiryDate] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const data: CreateJurisdictionRatePayload = {
      fips_code: fipsCode,
      jurisdiction_level: jurisdictionLevel,
      tax_type: taxType,
      product_codes: productCodes
        .split(",")
        .map((c) => c.trim())
        .filter(Boolean),
      rate_cents_per_gallon: parseInt(rateCentsPerGallon, 10),
      effective_date: effectiveDate,
      expiry_date: expiryDate || null,
    };
    await onSubmit(data);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-white border border-gray-200 rounded-lg p-6 shadow-sm max-w-2xl"
    >
      <h2 className="text-lg font-bold mb-4">Add New Jurisdiction Rate</h2>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label htmlFor="fips-code" className="block text-sm font-medium mb-1">
            FIPS Code
          </label>
          <input
            id="fips-code"
            type="text"
            required
            value={fipsCode}
            onChange={(e) => setFipsCode(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g., 48 (TX) or 48201 (Harris County)"
          />
        </div>
        <div>
          <label
            htmlFor="jurisdiction-level"
            className="block text-sm font-medium mb-1"
          >
            Jurisdiction Level
          </label>
          <select
            id="jurisdiction-level"
            value={jurisdictionLevel}
            onChange={(e) =>
              setJurisdictionLevel(
                e.target
                  .value as CreateJurisdictionRatePayload["jurisdiction_level"],
              )
            }
            className="w-full border rounded px-3 py-2"
          >
            <option value="federal">Federal</option>
            <option value="state">State</option>
            <option value="county">County</option>
            <option value="city">City</option>
          </select>
        </div>
        <div>
          <label htmlFor="tax-type" className="block text-sm font-medium mb-1">
            Tax Type
          </label>
          <select
            id="tax-type"
            value={taxType}
            onChange={(e) =>
              setTaxType(
                e.target.value as CreateJurisdictionRatePayload["tax_type"],
              )
            }
            className="w-full border rounded px-3 py-2"
          >
            <option value="excise">Excise</option>
            <option value="ust">UST</option>
            <option value="spcc">SPCC</option>
            <option value="environmental">Environmental</option>
          </select>
        </div>
        <div>
          <label
            htmlFor="product-codes"
            className="block text-sm font-medium mb-1"
          >
            Product Codes
          </label>
          <input
            id="product-codes"
            type="text"
            required
            value={productCodes}
            onChange={(e) => setProductCodes(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g., UNL, DSL, E85"
          />
          <p className="text-xs text-gray-500 mt-1">Comma-separated</p>
        </div>
        <div>
          <label
            htmlFor="rate-cents"
            className="block text-sm font-medium mb-1"
          >
            Rate (cents per gallon)
          </label>
          <input
            id="rate-cents"
            type="number"
            required
            min="0"
            value={rateCentsPerGallon}
            onChange={(e) => setRateCentsPerGallon(e.target.value)}
            className="w-full border rounded px-3 py-2"
            placeholder="e.g., 1840 for 18.4¢"
          />
          <p className="text-xs text-gray-500 mt-1">
            Integer cents (e.g., 1840 = 18.40¢)
          </p>
        </div>
        <div>
          <label
            htmlFor="effective-date"
            className="block text-sm font-medium mb-1"
          >
            Effective Date
          </label>
          <input
            id="effective-date"
            type="date"
            required
            value={effectiveDate}
            onChange={(e) => setEffectiveDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
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
            value={expiryDate}
            onChange={(e) => setExpiryDate(e.target.value)}
            className="w-full border rounded px-3 py-2"
          />
          <p className="text-xs text-gray-500 mt-1">
            Optional — leave blank for no expiry
          </p>
        </div>
      </div>

      <div className="flex gap-3 mt-6">
        <button
          type="submit"
          disabled={loading}
          className="bg-primary text-white px-4 py-2 rounded hover:bg-primary-hover disabled:opacity-50"
        >
          {loading ? "Saving..." : "Add Rate"}
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
