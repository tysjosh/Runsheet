"use client";

/**
 * Settings → Company → Tax jurisdictions (UI revamp task 3.5): level and tax
 * type in the Filters popover, a DataTable with products by name and rates
 * in cents per gallon, "Add rate" as an md FormDialog (design.md §5) and the
 * CSV import in a Modal (a bulk upload with a per-row report, not a form).
 *
 * Rates are stored in tenths of a cent per gallon (`tax_engine.RATE_SCALE`,
 * 184 = 18.4¢); the UI shows and edits cents with one decimal.
 */
import { FileUp, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  type Column,
  DataTable,
  Field,
  FilterPopover,
  FormDialog,
  IconButton,
  INPUT_CLASS,
  Modal,
  NumberField,
  ProductCap,
  ProductChip,
  Select,
  Toolbar,
  usePageChrome,
} from "@/components/ui";
import { calendarDate, humanize, number, productName } from "../../lib/format";
import {
  type CreateJurisdictionRatePayload,
  createTaxJurisdiction,
  getTaxJurisdictions,
  type JurisdictionRate,
} from "../../services/complianceApi";
import { PRODUCT_CODES } from "../../styles/tokens";
import { PageTitle } from "../ui/PageHeader";

/** Stored rate unit: tenths of a cent per gallon (backend RATE_SCALE). */
export const RATE_SCALE = 10;

/** 184 → "18.4¢". */
export function formatRate(stored: number | null | undefined): string {
  if (stored == null || !Number.isFinite(stored)) return "—";
  return `${number(stored / RATE_SCALE, { decimals: 1 })}¢`;
}

function formatDate(dateStr: string | null): string {
  return dateStr ? calendarDate(dateStr) : "—";
}

const LEVELS: {
  value: JurisdictionRate["jurisdiction_level"];
  label: string;
}[] = [
  { value: "federal", label: "Federal" },
  { value: "state", label: "State" },
  { value: "county", label: "County" },
  { value: "city", label: "City" },
];

const TAX_TYPES: { value: JurisdictionRate["tax_type"]; label: string }[] = [
  { value: "excise", label: "Excise" },
  { value: "ust", label: "UST" },
  { value: "spcc", label: "SPCC" },
  { value: "environmental", label: "Environmental" },
];

const labelOf = (list: { value: string; label: string }[], v: string) =>
  list.find((x) => x.value === v)?.label ?? humanize(v);

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

// ─── Table columns ───────────────────────────────────────────────────────────

const jurisdictionColumns: Column<JurisdictionRate>[] = [
  {
    key: "fips_code",
    header: "FIPS",
    width: 100,
    className: "font-mono text-xs",
    cell: (r) => r.fips_code,
  },
  {
    key: "jurisdiction_level",
    header: "Level",
    width: 100,
    cell: (r) => labelOf(LEVELS, r.jurisdiction_level),
  },
  {
    key: "tax_type",
    header: "Tax type",
    width: 130,
    cell: (r) => labelOf(TAX_TYPES, r.tax_type),
  },
  {
    key: "product_codes",
    header: "Products",
    cell: (r) =>
      r.product_codes.length === 0 ? (
        "—"
      ) : (
        <span
          className="flex flex-wrap items-center gap-1"
          title={r.product_codes.map((c) => productName(c)).join(", ")}
        >
          {r.product_codes.length === 1 ? (
            <ProductChip code={r.product_codes[0]} />
          ) : (
            r.product_codes.map((c) => <ProductCap key={c} code={c} />)
          )}
        </span>
      ),
  },
  {
    key: "rate_cents_per_gallon",
    header: "Rate (¢/gal)",
    align: "right",
    width: 120,
    className: "tabular-nums font-medium",
    cell: (r) => formatRate(r.rate_cents_per_gallon),
  },
  {
    key: "effective_date",
    header: "Effective",
    width: 150,
    cell: (r) => formatDate(r.effective_date),
  },
  {
    key: "expiry_date",
    header: "Expires",
    width: 150,
    cell: (r) => formatDate(r.expiry_date),
  },
];

// ─── Main Component ──────────────────────────────────────────────────────────

export default function TaxJurisdictionsPage() {
  const [jurisdictions, setJurisdictions] = useState<JurisdictionRate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [reload, setReload] = useState(0);
  const [jurisdictionLevelFilter, setJurisdictionLevelFilter] =
    useState<string>("");
  const [taxTypeFilter, setTaxTypeFilter] = useState<string>("");
  const [adding, setAdding] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importResult, setImportResult] = useState<{
    success: number;
    failed: number;
    errors: string[];
  } | null>(null);
  const [importing, setImporting] = useState(false);

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
    // `reload` refetches after a create or import.
  }, [page, jurisdictionLevelFilter, taxTypeFilter, reload]);

  useEffect(() => {
    fetchJurisdictions();
  }, [fetchJurisdictions]);

  const handleCSVImport = async (file: File) => {
    setImporting(true);
    setImportResult(null);
    try {
      const text = await file.text();
      const { rows, errors: parseErrors } = parseCSV(text);
      if (parseErrors.length > 0 && rows.length === 0) {
        setImportResult({ success: 0, failed: 0, errors: parseErrors });
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
      if (success > 0) setReload((n) => n + 1);
    } catch (err) {
      setImportResult({
        success: 0,
        failed: 0,
        errors: [
          err instanceof Error ? err.message : "Failed to read CSV file",
        ],
      });
    } finally {
      setImporting(false);
    }
  };

  const actions = useMemo(
    () => (
      <>
        <Button
          size="sm"
          variant="secondary"
          icon={<FileUp className="h-3.5 w-3.5" />}
          onClick={() => {
            setImportResult(null);
            setImportOpen(true);
          }}
        >
          Import CSV
        </Button>
        <Button
          size="sm"
          icon={<Plus className="h-3.5 w-3.5" />}
          onClick={() => setAdding(true)}
        >
          Add Rate
        </Button>
      </>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });
  const popoverCount =
    (jurisdictionLevelFilter ? 1 : 0) + (taxTypeFilter ? 1 : 0);

  return (
    <div className="flex h-full flex-col bg-surface">
      {!embedded && (
        <div className="flex h-11 items-center border-b border-slate-200 px-4">
          <PageTitle className="text-base font-semibold text-text">
            Tax Jurisdictions
          </PageTitle>
          <div className="ml-auto flex gap-2">{actions}</div>
        </div>
      )}
      <Toolbar
        label="Tax jurisdictions"
        filters={
          <FilterPopover
            count={popoverCount}
            label="Tax jurisdiction filters"
            onClear={() => {
              setJurisdictionLevelFilter("");
              setTaxTypeFilter("");
              setPage(1);
            }}
          >
            <div className="grid w-72 grid-cols-2 gap-3">
              <Field label="Jurisdiction level" id="jurisdiction-level-filter">
                <Select
                  id="jurisdiction-level-filter"
                  value={jurisdictionLevelFilter}
                  onChange={(v) => {
                    setJurisdictionLevelFilter(v);
                    setPage(1);
                  }}
                  placeholder="All"
                  options={LEVELS}
                />
              </Field>
              <Field label="Tax type" id="tax-type-filter">
                <Select
                  id="tax-type-filter"
                  value={taxTypeFilter}
                  onChange={(v) => {
                    setTaxTypeFilter(v);
                    setPage(1);
                  }}
                  placeholder="All"
                  options={TAX_TYPES}
                />
              </Field>
            </div>
          </FilterPopover>
        }
        end={
          <IconButton
            label="Refresh"
            size="sm"
            onClick={() => setReload((n) => n + 1)}
            icon={
              <RefreshCw
                className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
              />
            }
          />
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<JurisdictionRate>
          ariaLabel="Tax jurisdiction rates"
          columns={jurisdictionColumns}
          data={loading || error ? [] : jurisdictions}
          loading={loading}
          error={error ? { message: error, onRetry: fetchJurisdictions } : null}
          getRowId={(r) => r.jurisdiction_id}
          pagination={
            totalPages > 1
              ? { page, totalPages, onPageChange: setPage }
              : undefined
          }
          emptyState={
            <span className="text-text-muted">
              No tax jurisdiction rates found.
            </span>
          }
        />
      </div>

      {adding && (
        <AddRateDialog
          onClose={() => setAdding(false)}
          onSaved={() => setReload((n) => n + 1)}
        />
      )}

      <Modal
        isOpen={importOpen}
        onClose={() => setImportOpen(false)}
        title="Import tax jurisdictions"
        size="lg"
        footer={
          <Button variant="ghost" onClick={() => setImportOpen(false)}>
            Close
          </Button>
        }
      >
        <div className="space-y-3 text-sm">
          <p className="text-text-muted">
            A CSV with the columns fips_code, jurisdiction_level, tax_type,
            product_codes, rate_cents_per_gallon, effective_date and optionally
            expiry_date. Levels: federal, state, county, city. Tax types:
            excise, ust, spcc, environmental. Product codes are
            semicolon-separated catalog codes (DIESEL_2;GASOLINE_REG). The rate
            is in tenths of a cent per gallon (184 = 18.4¢).
          </p>
          <Field label="CSV file" id="csv-file-input">
            <input
              id="csv-file-input"
              type="file"
              accept=".csv,text/csv"
              disabled={importing}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void handleCSVImport(file);
              }}
              className="block w-full text-sm"
            />
          </Field>
          {importing && (
            <p role="status" className="text-text-muted">
              Importing…
            </p>
          )}
          {importResult && (
            <div role="status" className="space-y-2">
              <p className="font-medium text-text">
                {number(importResult.success)} imported
                {importResult.failed > 0 &&
                  ` · ${number(importResult.failed)} failed`}
              </p>
              {importResult.errors.length > 0 && (
                <ul className="max-h-40 list-inside list-disc overflow-y-auto text-xs text-amber-800">
                  {importResult.errors.map((err) => (
                    <li key={err}>{err}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </Modal>
    </div>
  );
}

// ─── Add rate dialog ─────────────────────────────────────────────────────────

type RateValues = {
  fips_code: string;
  jurisdiction_level: CreateJurisdictionRatePayload["jurisdiction_level"];
  tax_type: CreateJurisdictionRatePayload["tax_type"];
  product_codes: string[];
  /** Cents per gallon, one decimal (stored ×10). */
  rate_cents: number | null;
  effective_date: string;
  expiry_date: string;
};

export function validateRate(v: RateValues) {
  const errors: Record<string, string | undefined> = {};
  if (!/^\d{2}(\d{3}|\d{5})?$/.test(v.fips_code.trim()))
    errors.fips_code =
      "Enter a 2-digit state, 5-digit county or 7-digit place FIPS code.";
  if (v.product_codes.length === 0)
    errors.product_codes = "Pick at least one product.";
  if (v.rate_cents == null) errors.rate_cents = "Enter the rate.";
  else if (v.rate_cents < 0) errors.rate_cents = "Rate can't be negative.";
  if (!v.effective_date) errors.effective_date = "Enter the effective date.";
  if (v.expiry_date && v.effective_date && v.expiry_date <= v.effective_date)
    errors.expiry_date = "Expiry must be after the effective date.";
  return errors;
}

function AddRateDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const submit = async (v: RateValues) => {
    const data: CreateJurisdictionRatePayload = {
      fips_code: v.fips_code.trim(),
      jurisdiction_level: v.jurisdiction_level,
      tax_type: v.tax_type,
      product_codes: v.product_codes,
      rate_cents_per_gallon: Math.round((v.rate_cents ?? 0) * RATE_SCALE),
      effective_date: v.effective_date,
      expiry_date: v.expiry_date || null,
    };
    await createTaxJurisdiction(data);
  };
  return (
    <FormDialog<RateValues, void>
      open
      size="md"
      title="Add jurisdiction rate"
      submitLabel="Add Rate"
      successMessage="Rate added"
      initialValues={{
        fips_code: "",
        jurisdiction_level: "state",
        tax_type: "excise",
        product_codes: [],
        rate_cents: null,
        effective_date: "",
        expiry_date: "",
      }}
      validate={validateRate}
      onSubmit={submit}
      onSaved={onSaved}
      onClose={onClose}
    >
      {({ values, set, errors }) => (
        <>
          <Field
            label="FIPS code"
            required
            span={1}
            help="48 (Texas) or 48201 (Harris County)"
            error={errors.fips_code}
          >
            <input
              id="fips-code"
              type="text"
              inputMode="numeric"
              value={values.fips_code}
              onChange={(e) => set("fips_code", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
          <Field label="Jurisdiction level" span={1}>
            <Select
              id="jurisdiction-level"
              value={values.jurisdiction_level}
              onChange={(v) =>
                set("jurisdiction_level", v as RateValues["jurisdiction_level"])
              }
              options={LEVELS}
            />
          </Field>
          <Field label="Tax type" span={1}>
            <Select
              id="tax-type"
              value={values.tax_type}
              onChange={(v) => set("tax_type", v as RateValues["tax_type"])}
              options={TAX_TYPES}
            />
          </Field>
          <Field
            label="Rate"
            required
            span={1}
            help="Cents per gallon, e.g. 18.4"
            error={errors.rate_cents}
          >
            <NumberField
              id="rate-cents"
              value={values.rate_cents}
              onChange={(n) => set("rate_cents", n)}
              unit="¢/gal"
              decimals={1}
              min={0}
            />
          </Field>
          <fieldset className="col-span-2">
            <legend className="mb-1 text-xs font-medium text-slate-700">
              Products <span className="text-red-700">*</span>
            </legend>
            <div className="grid grid-cols-2 gap-1.5">
              {PRODUCT_CODES.map((code) => {
                const checked = values.product_codes.includes(code);
                return (
                  <label
                    key={code}
                    className="flex items-center gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-slate-50"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() =>
                        set(
                          "product_codes",
                          checked
                            ? values.product_codes.filter((c) => c !== code)
                            : [...values.product_codes, code],
                        )
                      }
                    />
                    <ProductCap code={code} decorative />
                    <span>{productName(code)}</span>
                  </label>
                );
              })}
            </div>
            {errors.product_codes && (
              <p role="alert" className="mt-1 text-xs text-red-800">
                {errors.product_codes}
              </p>
            )}
          </fieldset>
          <Field
            label="Effective date"
            required
            span={1}
            error={errors.effective_date}
          >
            <input
              id="effective-date"
              type="date"
              value={values.effective_date}
              onChange={(e) => set("effective_date", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
          <Field
            label="Expiry date"
            span={1}
            help="Optional"
            error={errors.expiry_date}
          >
            <input
              id="expiry-date"
              type="date"
              value={values.expiry_date}
              onChange={(e) => set("expiry_date", e.target.value)}
              className={INPUT_CLASS}
            />
          </Field>
        </>
      )}
    </FormDialog>
  );
}
