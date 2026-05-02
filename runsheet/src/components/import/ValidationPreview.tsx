import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  FileWarning,
  Loader2,
  XCircle,
  FileSpreadsheet,
  Upload,
} from "lucide-react";
import { useEffect, useState } from "react";
import type { ValidationResult } from "../../types/import";
import { importApi } from "../../services/importApi";

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
      <div className="flex flex-col items-center justify-center py-20 text-gray-400">
        <Loader2 className="w-8 h-8 animate-spin mb-4" />
        <p className="text-sm font-medium">Validating your data…</p>
        <p className="text-xs mt-1 text-gray-400">
          Checking rows against the schema template
        </p>
      </div>
    );
  }

  // ── Error state ────────────────────────────────────────────────────────

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-20">
        <XCircle className="w-10 h-10 text-red-500 mb-4" />
        <p className="text-sm font-medium text-red-600 mb-2">
          Validation Failed
        </p>
        <p className="text-xs text-gray-500 mb-6 max-w-md text-center">
          {error}
        </p>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onBackToMapping}
            className="px-4 py-2 text-sm font-medium text-gray-600 hover:text-[#232323] transition-colors"
          >
            Back to Mapping
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 text-sm font-medium text-red-600 hover:text-red-700 transition-colors"
          >
            Cancel Import
          </button>
        </div>
      </div>
    );
  }

  // At this point validationResult is guaranteed non-null
  const result = validationResult!;
  const hasErrors = result.error_count > 0;
  const hasWarnings = result.warning_count > 0;
  const canImport = result.valid_rows > 0;

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div>
      {/* Header */}
      <div className="mb-6">
        <h2 className="text-lg font-semibold text-[#232323] mb-1">
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
        <div className="mb-6 p-4 rounded-xl bg-green-50 border border-green-200">
          <div className="flex items-center gap-3">
            <CheckCircle2 className="w-5 h-5 text-green-600 flex-shrink-0" />
            <p className="text-sm font-medium text-green-800">
              All {result.total_rows} rows passed validation. Ready to import.
            </p>
          </div>
        </div>
      )}

      {/* Errors table */}
      {hasErrors && (
        <div className="mb-6">
          <div className="flex items-center gap-2 mb-3">
            <XCircle className="w-4 h-4 text-red-500" />
            <h3 className="text-sm font-semibold text-[#232323]">
              Errors ({result.error_count})
            </h3>
          </div>
          <div className="rounded-xl border border-red-200 overflow-hidden">
            <div className="max-h-64 overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="bg-red-50 sticky top-0">
                  <tr>
                    <th className="text-left px-4 py-2.5 text-xs font-medium text-red-700 uppercase tracking-wider w-20">
                      Row
                    </th>
                    <th className="text-left px-4 py-2.5 text-xs font-medium text-red-700 uppercase tracking-wider w-36">
                      Field
                    </th>
                    <th className="text-left px-4 py-2.5 text-xs font-medium text-red-700 uppercase tracking-wider">
                      Description
                    </th>
                    <th className="text-left px-4 py-2.5 text-xs font-medium text-red-700 uppercase tracking-wider w-36">
                      Value
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {result.errors.map((issue, idx) => (
                    <tr
                      key={`err-${issue.row_number}-${issue.field_name}-${idx}`}
                      className={
                        idx < result.errors.length - 1
                          ? "border-b border-red-100"
                          : ""
                      }
                    >
                      <td className="px-4 py-2.5 text-red-700 font-medium">
                        {issue.row_number}
                      </td>
                      <td className="px-4 py-2.5 text-red-800 font-mono text-xs">
                        {issue.field_name}
                      </td>
                      <td className="px-4 py-2.5 text-red-600">
                        {issue.description}
                      </td>
                      <td className="px-4 py-2.5 text-red-500 font-mono text-xs truncate max-w-[140px]">
                        {issue.value ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Warnings table */}
      {hasWarnings && (
        <div className="mb-6">
          <div className="flex items-center gap-2 mb-3">
            <AlertTriangle className="w-4 h-4 text-amber-500" />
            <h3 className="text-sm font-semibold text-[#232323]">
              Warnings ({result.warning_count})
            </h3>
          </div>
          <div className="rounded-xl border border-amber-200 overflow-hidden">
            <div className="max-h-64 overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="bg-amber-50 sticky top-0">
                  <tr>
                    <th className="text-left px-4 py-2.5 text-xs font-medium text-amber-700 uppercase tracking-wider w-20">
                      Row
                    </th>
                    <th className="text-left px-4 py-2.5 text-xs font-medium text-amber-700 uppercase tracking-wider w-36">
                      Field
                    </th>
                    <th className="text-left px-4 py-2.5 text-xs font-medium text-amber-700 uppercase tracking-wider">
                      Description
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {result.warnings.map((issue, idx) => (
                    <tr
                      key={`warn-${issue.row_number}-${issue.field_name}-${idx}`}
                      className={
                        idx < result.warnings.length - 1
                          ? "border-b border-amber-100"
                          : ""
                      }
                    >
                      <td className="px-4 py-2.5 text-amber-700 font-medium">
                        {issue.row_number}
                      </td>
                      <td className="px-4 py-2.5 text-amber-800 font-mono text-xs">
                        {issue.field_name}
                      </td>
                      <td className="px-4 py-2.5 text-amber-600">
                        {issue.description}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Footer actions */}
      <div className="flex items-center justify-between pt-6 border-t border-gray-100">
        <button
          type="button"
          onClick={onBackToMapping}
          className="flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-gray-600 hover:text-[#232323] transition-colors"
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
                ? "bg-[#232323] text-white hover:bg-black"
                : "bg-gray-100 text-gray-400 cursor-not-allowed"
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
      value: "text-[#232323]",
      label: "text-gray-500",
    },
    green: {
      bg: "bg-green-50",
      border: "border-green-200",
      icon: "text-green-600",
      value: "text-green-700",
      label: "text-green-600",
    },
    red: {
      bg: "bg-red-50",
      border: "border-red-200",
      icon: "text-red-500",
      value: "text-red-700",
      label: "text-red-500",
    },
    amber: {
      bg: "bg-amber-50",
      border: "border-amber-200",
      icon: "text-amber-500",
      value: "text-amber-700",
      label: "text-amber-500",
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
