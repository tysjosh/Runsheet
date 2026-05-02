import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ImportResult } from "../../types/import";
import { importApi } from "../../services/importApi";

// ─── Props ───────────────────────────────────────────────────────────────────

interface ImportProgressProps {
  sessionId: string;
  skipErrors: boolean;
  onComplete: (result: ImportResult) => void;
}

// ─── Status Phases ───────────────────────────────────────────────────────────

type StatusPhase = "processing" | "indexing" | "completing";

const STATUS_LABELS: Record<StatusPhase, string> = {
  processing: "Processing records…",
  indexing: "Indexing into Elasticsearch…",
  completing: "Completing import…",
};

// ─── Component ───────────────────────────────────────────────────────────────

export default function ImportProgress({
  sessionId,
  skipErrors,
  onComplete,
}: ImportProgressProps) {
  const [phase, setPhase] = useState<StatusPhase>("processing");
  const [error, setError] = useState<string | null>(null);
  const [importedBeforeFailure, setImportedBeforeFailure] = useState<
    number | null
  >(null);
  const [result, setResult] = useState<ImportResult | null>(null);

  // Track whether the component is still mounted
  const mountedRef = useRef(true);
  // Prevent double-invocation in React strict mode
  const calledRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // ── Call commit on mount ─────────────────────────────────────────────────

  useEffect(() => {
    if (calledRef.current) return;
    calledRef.current = true;

    async function runCommit() {
      try {
        const commitResult = await importApi.commit(sessionId, skipErrors);

        if (!mountedRef.current) return;

        // API returned — transition through indexing → completing → done
        setResult(commitResult);
        setPhase("indexing");

        setTimeout(() => {
          if (!mountedRef.current) return;
          setPhase("completing");

          setTimeout(() => {
            if (!mountedRef.current) return;
            onComplete(commitResult);
          }, 600);
        }, 800);
      } catch (err) {
        if (!mountedRef.current) return;

        // Extract error details
        const message =
          err instanceof Error
            ? err.message
            : "An unexpected error occurred during import.";

        // Try to extract imported count from error if available
        let importedCount: number | null = null;
        if (err && typeof err === "object" && "imported_records" in err) {
          importedCount = (err as { imported_records: number })
            .imported_records;
        }

        setError(message);
        setImportedBeforeFailure(importedCount);
      }
    }

    runCommit();
  }, [sessionId, skipErrors, onComplete]);

  // ── Error state ──────────────────────────────────────────────────────────

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-20">
        <div className="w-full max-w-md">
          {/* Error icon */}
          <div className="flex justify-center mb-6">
            <div className="w-16 h-16 rounded-full bg-red-50 flex items-center justify-center">
              <AlertCircle className="w-8 h-8 text-red-500" />
            </div>
          </div>

          {/* Error heading */}
          <h2 className="text-lg font-semibold text-[#232323] text-center mb-2">
            Import Failed
          </h2>

          {/* Error message */}
          <p className="text-sm text-gray-500 text-center mb-4">{error}</p>

          {/* Records imported before failure */}
          {importedBeforeFailure !== null && importedBeforeFailure > 0 && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 mb-6">
              <div className="flex items-center gap-3">
                <CheckCircle2 className="w-5 h-5 text-amber-600 flex-shrink-0" />
                <p className="text-sm text-amber-800">
                  <span className="font-medium">
                    {importedBeforeFailure} records
                  </span>{" "}
                  were successfully imported before the failure occurred.
                </p>
              </div>
            </div>
          )}

          {importedBeforeFailure === null && (
            <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 mb-6">
              <p className="text-sm text-gray-500 text-center">
                The number of records imported before the failure could not be
                determined.
              </p>
            </div>
          )}
        </div>
      </div>
    );
  }

  // ── Progress state ───────────────────────────────────────────────────────

  // Determine progress bar width based on phase
  const progressPercent =
    phase === "processing" ? 40 : phase === "indexing" ? 75 : 95;

  const totalRecords = result?.total_records ?? null;
  const importedRecords = result?.imported_records ?? null;

  return (
    <div className="flex flex-col items-center justify-center py-20">
      <div className="w-full max-w-md">
        {/* Spinner */}
        <div className="flex justify-center mb-6">
          <div className="w-16 h-16 rounded-full bg-gray-50 flex items-center justify-center">
            <Loader2 className="w-8 h-8 text-[#232323] animate-spin" />
          </div>
        </div>

        {/* Status label */}
        <h2 className="text-lg font-semibold text-[#232323] text-center mb-2">
          Importing Data
        </h2>
        <p className="text-sm text-gray-500 text-center mb-6">
          {STATUS_LABELS[phase]}
        </p>

        {/* Progress bar */}
        <div className="mb-4">
          <div className="w-full h-2.5 bg-gray-100 rounded-full overflow-hidden">
            <div
              className="h-full bg-[#232323] rounded-full transition-all duration-700 ease-out"
              style={{ width: `${progressPercent}%` }}
            />
          </div>
        </div>

        {/* Record count */}
        <div className="text-center">
          {totalRecords !== null && importedRecords !== null ? (
            <p className="text-sm text-gray-500">
              <span className="font-medium text-[#232323]">
                {importedRecords}
              </span>{" "}
              / {totalRecords} records processed
            </p>
          ) : (
            <p className="text-sm text-gray-400">
              Waiting for results…
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
