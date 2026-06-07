import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  FileSpreadsheet,
  Loader2,
  Upload,
  XCircle,
} from "lucide-react";
import { useEffect, useState } from "react";
import { importApi } from "../../services/importApi";
import type { ValidationIssue, ValidationResult } from "../../types/import";
import { type Column, Table } from "../ui";

// ─── Table columns ───────────────────────────────────────────────────────────

const errorColumns: Column<ValidationIssue>[] = [
  {
    key: "row_number",
    label: "Row",
    headerClassName: "text-error-dark w-20",
    className: "text-error-dark font-medium",
    render: (issue) => issue.row_number,
  },
  {
    key: "field_name",
    label: "Field",
    headerClassName: "text-error-dark w-36",
    className: "text-error-dark font-mono text-xs",
    render: (issue) => issue.field_name,
  },
  {
    key: "description",
    label: "Description",
    headerClassName: "text-error-dark",
    className: "text-error",
    render: (issue) => issue.description,
  },
  {
    key: "value",
    label: "Value",
    headerClassName: "text-error-dark w-36",
    className: "text-error font-mono text-xs truncate max-w-[140px]",
    render: (issue) => issue.value ?? "—",
  },
];

const warningColumns: Column<ValidationIssue>[] = [
  {
    key: "row_number",
    label: "Row",
    headerClassName: "text-warning-dark w-20",
    className: "text-warning-dark font-medium",
    render: (issue) => issue.row_number,
  },
  {
    key: "field_name",
    label: "Field",
    headerClassName: "text-warning-dark w-36",
    className: "text-warning-dark font-mono text-xs",
    render: (issue) => issue.field_name,
  },
  {
    key: "description",
    label: "Description",
    headerClassName: "text-warning-dark",
    className: "text-warning",
    render: (issue) => issue.description,
  },
];

// ─── Props ───────────────────────────────────────────────────────────────────

interface ValidationPreviewProps {
  sessionId: string;
  fieldMapping: Record<string, string>;
  validationResult: ValidationResult | null;
  onValidationComplete: (result: ValidationResult) => void;
  onCommit: () => void;
  onBackToMapping: () => void;
  onCancel: () => void;
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function ValidationPreview({
  sessionId,
  fieldMapping,
  validationResult,
  onValidationComplete,
  onCommit,
  onBackToMapping,
  onCancel,
}: ValidationPreviewProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ── Run validation on mount if no result yet ───────────────────────────

  useEffect(() => {
    if (validationResult) return;

    let cancelled = false;

    async function runValidation() {
      setLoading(true);
      setError(null);

      try {
        const result = await importApi.validate(sessionId, fieldMapping);
        if (!cancelled) {
          onValidationComplete(result);
        }
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error
              ? err.message
              : "Validation failed. Please try again.",
          );
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    runValidation();
    return () => {
      cancelled = true;
    };
  }, [sessionId, fieldMapping, validationResult, onValidationComplete]);

  // ── Loading state ──────────────────────────────────────────────────────

  if (loading || (!validationResult && !error)) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-gray-500">
        <Loader2 className="w-8 h-8 animate-spin mb-4" />
        <p className="text-sm font-medium">Validating your data…</p>
        <p className="text-xs mt-1 text-gray-500">
          Checking rows against the schema template
        </p>
      </div>
    );
  }

  // ── Error state ────────────────────────────────────────────────────────

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-20">
        <XCircle className="w-10 h-10 text-error mb-4" />
        <p className="text-sm font-medium text-error mb-2">Validation Failed</p>
        <p className="text-xs text-gray-500 mb-6 max-w-md text-center">
          {error}
        </p>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onBackToMapping}
            className="px-4 py-2 text-sm font-medium text-gray-600 hover:text-primary transition-colors"
          >
            Back to Mapping
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 text-sm font-medium text-error hover:text-error-dark transition-colors"
          >
            Cancel Import
          </button>
        </div>
      </div>
    );
  }

  // At this point validationResult is guaranteed non-null (loading/error
  // states returned above), but guard explicitly to satisfy strict checks.
  if (!validationResult) return null;
  const result = validationResult;
  const hasErrors = result.error_count > 0;
  const hasWarnings = result.warning_count > 0;
  const canImport = result.valid_rows > 0;

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div>
      {/* Header */}
      <div className="mb-6">
        <h2 className="text-lg font-semibold text-primary mb-1">
          Validation Preview
        </h2>
        <p className="text-sm text-gray-500">
          Review the validation results below. You can import valid rows or go
          back to fix issues in your source data.
        </p>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-6">
        <SummaryCard
          label="Total Rows"
          value={result.total_rows}
          icon={<FileSpreadsheet className="w-5 h-5" />}
          color="gray"
        />
        <SummaryCard
          label="Valid Rows"
          value={result.valid_rows}
          icon={<CheckCircle2 className="w-5 h-5" />}
          color="green"
        />
        <SummaryCard
          label="Errors"
          value={result.error_count}
          icon={<XCircle className="w-5 h-5" />}
          color="red"
        />
        <SummaryCard
          label="Warnings"
          value={result.warning_count}
          icon={<AlertTriangle className="w-5 h-5" />}
          color="amber"
        />
      </div>

      {/* All valid banner */}
      {!hasErrors && !hasWarnings && (
        <div className="mb-6 p-4 rounded-xl bg-success-light border border-success-light">
          <div className="flex items-center gap-3">
            <CheckCircle2 className="w-5 h-5 text-success flex-shrink-0" />
            <p className="text-sm font-medium text-success-dark">
              All {result.total_rows} rows passed validation. Ready to import.
            </p>
          </div>
        </div>
      )}

      {/* Errors table */}
      {hasErrors && (
        <div className="mb-6">
          <div className="flex items-center gap-2 mb-3">
            <XCircle className="w-4 h-4 text-error" />
            <h3 className="text-sm font-semibold text-primary">
              Errors ({result.error_count})
            </h3>
          </div>
          <div className="rounded-xl border border-error-light overflow-hidden">
            <div className="max-h-64 overflow-y-auto">
              <Table<ValidationIssue>
                ariaLabel="Validation errors"
                columns={errorColumns}
                data={result.errors}
                variant="compact"
              />
            </div>
          </div>
        </div>
      )}

      {/* Warnings table */}
      {hasWarnings && (
        <div className="mb-6">
          <div className="flex items-center gap-2 mb-3">
            <AlertTriangle className="w-4 h-4 text-warning" />
            <h3 className="text-sm font-semibold text-primary">
              Warnings ({result.warning_count})
            </h3>
          </div>
          <div className="rounded-xl border border-warning-light overflow-hidden">
            <div className="max-h-64 overflow-y-auto">
              <Table<ValidationIssue>
                ariaLabel="Validation warnings"
                columns={warningColumns}
                data={result.warnings}
                variant="compact"
              />
            </div>
          </div>
        </div>
      )}

      {/* Footer actions */}
      <div className="flex items-center justify-between pt-6 border-t border-gray-100">
        <button
          type="button"
          onClick={onBackToMapping}
          className="flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-gray-600 hover:text-primary transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          Back to Mapping
        </button>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2.5 text-sm font-medium text-gray-500 hover:text-gray-700 transition-colors"
          >
            Cancel
          </button>

          <button
            type="button"
            onClick={onCommit}
            disabled={!canImport}
            className={`flex items-center gap-2 px-6 py-2.5 text-sm font-medium rounded-xl transition-colors ${
              canImport
                ? "bg-primary text-white hover:bg-primary-hover"
                : "bg-gray-100 text-gray-500 cursor-not-allowed"
            }`}
          >
            <Upload className="w-4 h-4" />
            {hasErrors
              ? `Import ${result.valid_rows} Valid Rows`
              : "Import Valid Rows"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Summary Card Sub-component ──────────────────────────────────────────────

function SummaryCard({
  label,
  value,
  icon,
  color,
}: {
  label: string;
  value: number;
  icon: React.ReactNode;
  color: "gray" | "green" | "red" | "amber";
}) {
  const colorMap = {
    gray: {
      bg: "bg-gray-50",
      border: "border-gray-200",
      icon: "text-gray-500",
      value: "text-primary",
      label: "text-gray-500",
    },
    green: {
      bg: "bg-success-light",
      border: "border-success-light",
      icon: "text-success",
      value: "text-success-dark",
      label: "text-success",
    },
    red: {
      bg: "bg-error-light",
      border: "border-error-light",
      icon: "text-error",
      value: "text-error-dark",
      label: "text-error",
    },
    amber: {
      bg: "bg-warning-light",
      border: "border-warning-light",
      icon: "text-warning",
      value: "text-warning-dark",
      label: "text-warning",
    },
  };

  const c = colorMap[color];

  return (
    <div className={`rounded-xl border ${c.border} ${c.bg} p-4`}>
      <div className="flex items-center gap-2 mb-2">
        <span className={c.icon}>{icon}</span>
        <span className={`text-xs font-medium ${c.label}`}>{label}</span>
      </div>
      <p className={`text-2xl font-semibold ${c.value}`}>{value}</p>
    </div>
  );
}
