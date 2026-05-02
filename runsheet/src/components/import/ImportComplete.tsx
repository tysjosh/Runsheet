import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Database,
  FileSpreadsheet,
  Plus,
  SkipForward,
} from "lucide-react";
import type { ImportResult } from "../../types/import";

// ─── Props ───────────────────────────────────────────────────────────────────

interface ImportCompleteProps {
  result: ImportResult;
  onStartNew: () => void;
}

// ─── Data Type Labels ────────────────────────────────────────────────────────

const DATA_TYPE_LABELS: Record<string, string> = {
  fleet: "Fleet",
  orders: "Orders",
  riders: "Riders",
  fuel_stations: "Fuel Stations",
  inventory: "Inventory",
  support_tickets: "Support Tickets",
  jobs: "Jobs / Scheduling",
};

// ─── Component ───────────────────────────────────────────────────────────────

export default function ImportComplete({
  result,
  onStartNew,
}: ImportCompleteProps) {
  const isPartial = result.status === "partial";
  const dataTypeLabel = DATA_TYPE_LABELS[result.data_type] ?? result.data_type;

  return (
    <div className="flex flex-col items-center justify-center py-16">
      <div className="w-full max-w-lg">
        {/* Success / Partial icon */}
        <div className="flex justify-center mb-6">
          <div
            className={`w-16 h-16 rounded-full flex items-center justify-center ${
              isPartial ? "bg-amber-50" : "bg-green-50"
            }`}
          >
            {isPartial ? (
              <AlertTriangle className="w-8 h-8 text-amber-500" />
            ) : (
              <CheckCircle2 className="w-8 h-8 text-green-500" />
            )}
          </div>
        </div>

        {/* Heading */}
        <h2 className="text-lg font-semibold text-[#232323] text-center mb-1">
          {isPartial ? "Import Completed with Warnings" : "Import Complete"}
        </h2>
        <p className="text-sm text-gray-500 text-center mb-6">
          {isPartial
            ? "Some records were skipped during the import."
            : "All records have been successfully imported."}
        </p>

        {/* Partial warning banner */}
        {isPartial && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 mb-6">
            <div className="flex items-center gap-3">
              <AlertTriangle className="w-5 h-5 text-amber-600 flex-shrink-0" />
              <p className="text-sm text-amber-800">
                <span className="font-medium">
                  {result.skipped_records} record
                  {result.skipped_records !== 1 ? "s" : ""}
                </span>{" "}
                were skipped due to validation errors. Review your source data
                and re-import if needed.
              </p>
            </div>
          </div>
        )}

        {/* Summary card */}
        <div className="rounded-xl border border-gray-200 bg-white p-6 mb-8">
          <h3 className="text-sm font-medium text-gray-400 uppercase tracking-wider mb-4">
            Import Summary
          </h3>

          <div className="grid grid-cols-2 gap-4">
            {/* Records imported */}
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg bg-green-50 flex items-center justify-center flex-shrink-0">
                <CheckCircle2 className="w-4.5 h-4.5 text-green-600" />
              </div>
              <div>
                <p className="text-xs text-gray-400">Imported</p>
                <p className="text-lg font-semibold text-[#232323]">
                  {result.imported_records}
                </p>
              </div>
            </div>

            {/* Records skipped */}
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg bg-gray-50 flex items-center justify-center flex-shrink-0">
                <SkipForward className="w-4.5 h-4.5 text-gray-400" />
              </div>
              <div>
                <p className="text-xs text-gray-400">Skipped</p>
                <p className="text-lg font-semibold text-[#232323]">
                  {result.skipped_records}
                </p>
              </div>
            </div>

            {/* Data type */}
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg bg-blue-50 flex items-center justify-center flex-shrink-0">
                <FileSpreadsheet className="w-4.5 h-4.5 text-blue-600" />
              </div>
              <div>
                <p className="text-xs text-gray-400">Data Type</p>
                <p className="text-sm font-medium text-[#232323]">
                  {dataTypeLabel}
                </p>
              </div>
            </div>

            {/* ES Index */}
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg bg-purple-50 flex items-center justify-center flex-shrink-0">
                <Database className="w-4.5 h-4.5 text-purple-600" />
              </div>
              <div>
                <p className="text-xs text-gray-400">ES Index</p>
                <p className="text-sm font-medium text-[#232323] font-mono">
                  {result.es_index}
                </p>
              </div>
            </div>

            {/* Duration */}
            <div className="flex items-start gap-3 col-span-2">
              <div className="w-9 h-9 rounded-lg bg-gray-50 flex items-center justify-center flex-shrink-0">
                <Clock className="w-4.5 h-4.5 text-gray-500" />
              </div>
              <div>
                <p className="text-xs text-gray-400">Duration</p>
                <p className="text-sm font-medium text-[#232323]">
                  {result.duration_seconds.toFixed(1)}s
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* Start New Import button */}
        <div className="flex justify-center">
          <button
            type="button"
            onClick={onStartNew}
            className="flex items-center gap-2 px-6 py-2.5 text-sm font-medium rounded-xl bg-[#232323] text-white hover:bg-black transition-colors"
          >
            <Plus className="w-4 h-4" />
            Start New Import
          </button>
        </div>
      </div>
    </div>
  );
}
