"use client";

/**
 * CSV import of cost entries as an lg two-step FormDialog (design §5):
 * 1. "Check file": a dry run (per-row errors and duplicates, nothing written);
 * 2. "Import N rows" once the dry run passes.
 * All or nothing: any invalid row means nothing is imported. The step moves
 * on the server's dry-run answer (async), so it doesn't use FormDialog
 * `steps` (whose Next validates synchronously); the stepper line shows where
 * the user is instead. The dialog stays open after each call to show the
 * result (`keepOpenOnSuccess`).
 */
import { useState } from "react";
import {
  type ImportReport,
  importCostEntries,
} from "../../../services/marginApi";
import { Field, FormDialog } from "../../ui";
import { apiFieldErrors } from "./CostEntryForm";

export interface CostImportDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onImported: (count: number) => void;
}

// The file lives outside the form values: picking one isn't an edit worth a
// discard prompt (the dialog closed without asking before, too).
const NO_VALUES: Record<string, never> = {};

export default function CostImportDialog({
  isOpen,
  onClose,
  onImported,
}: CostImportDialogProps) {
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<ImportReport | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [status, setStatus] = useState("");

  const reset = () => {
    setReport(null);
    setErrors([]);
    setStatus("");
  };

  const close = () => {
    reset();
    setFile(null);
    onClose();
  };

  const ready = report?.dry_run === true && report.rows_valid > 0;
  const imported = report?.dry_run === false;

  const run = async () => {
    if (!file) return;
    const dryRun = !ready;
    setErrors([]);
    setStatus("");
    try {
      const result = await importCostEntries(file, dryRun);
      setReport(result);
      if (dryRun) {
        setStatus(
          `Dry run: ${result.rows_valid} of ${result.rows_total} rows can be imported.`,
        );
      } else {
        setStatus(`Imported ${result.created_entry_ids.length} rows.`);
        onImported(result.created_entry_ids.length);
      }
    } catch (e) {
      setReport(null);
      setErrors(apiFieldErrors(e));
    }
  };

  return (
    <FormDialog<Record<string, never>>
      open={isOpen}
      title="Import cost entries"
      size="lg"
      help="Columns: kind, product_code, effective_at, unit_cost_usd, and optionally terminal_id, supplier_name, effective_to, gallons, adder_type, bol_id, reference, notes. Up to 10,000 rows or 5 MB."
      initialValues={NO_VALUES}
      validate={() => (file ? {} : { file: "Choose a CSV file first." })}
      onSubmit={run}
      onClose={close}
      successMessage={null}
      keepOpenOnSuccess
      submitDisabled={imported}
      submitLabel={
        ready
          ? `Import ${report?.rows_valid} rows`
          : imported
            ? "Imported"
            : "Check file"
      }
    >
      {({ errors: fieldErrors }) => (
        <>
          <p
            className="col-span-2 text-xs font-semibold text-text-muted"
            data-testid="import-step"
          >
            {ready || imported
              ? "Step 2 of 2 · Import"
              : "Step 1 of 2 · Check file"}
          </p>
          <Field label="Cost entries CSV" error={fieldErrors.file} required>
            <input
              type="file"
              accept=".csv,text/csv"
              className="block text-sm"
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null);
                reset();
              }}
            />
          </Field>
          <div
            role="alert"
            className={
              errors.length ? "col-span-2 text-sm text-error" : "sr-only"
            }
          >
            {errors.length > 0 && (
              <ul className="list-disc pl-5 max-h-48 overflow-auto">
                {errors.map((msg) => (
                  <li key={msg}>{msg}</li>
                ))}
              </ul>
            )}
          </div>
          <p
            role="status"
            className={status ? "col-span-2 text-sm text-gray-800" : "sr-only"}
          >
            {status}
          </p>
          {report && report.duplicates.length > 0 && (
            <div className="col-span-2 text-sm">
              <h3 className="font-medium">Duplicates (skipped)</h3>
              <ul className="list-disc pl-5">
                {report.duplicates.map((d) => (
                  <li key={`${d.row}-${d.natural_key}`}>
                    Row {d.row}:{" "}
                    {d.existing_entry_id
                      ? `matches existing entry ${d.existing_entry_id}`
                      : `repeats row ${d.duplicate_of_row}`}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {report && report.warnings.length > 0 && (
            <ul className="col-span-2 list-disc pl-5 text-sm text-amber-800">
              {report.warnings.map((w) => (
                <li key={w}>{w.split("_").join(" ")}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </FormDialog>
  );
}
