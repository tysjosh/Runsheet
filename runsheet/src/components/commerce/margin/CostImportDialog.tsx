"use client";

/**
 * CSV import of cost entries: a dry run first (per-row errors and
 * duplicates, nothing written), then "Import N rows". All or nothing: any
 * invalid row means nothing is imported.
 */
import { type FormEvent, useState } from "react";
import {
  type ImportReport,
  importCostEntries,
} from "../../../services/marginApi";
import { Button, Modal } from "../../ui";
import { apiFieldErrors } from "./CostEntryForm";

export interface CostImportDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onImported: (count: number) => void;
}

export default function CostImportDialog({
  isOpen,
  onClose,
  onImported,
}: CostImportDialogProps) {
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<ImportReport | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

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

  const run = async (dryRun: boolean) => {
    if (!file) {
      setErrors(["Choose a CSV file first."]);
      return;
    }
    setBusy(true);
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
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void run(true);
  };

  const ready = report?.dry_run === true && report.rows_valid > 0;

  return (
    <Modal
      isOpen={isOpen}
      onClose={close}
      title="Import cost entries"
      size="lg"
    >
      <form onSubmit={onSubmit} className="space-y-3 text-sm">
        <p className="text-gray-600">
          Columns: kind, product_code, effective_at, unit_cost_usd, and
          optionally terminal_id, supplier_name, effective_to, gallons,
          adder_type, bol_id, reference, notes. Up to 10,000 rows or 5 MB.
        </p>
        <label className="block">
          Cost entries CSV
          <input
            type="file"
            accept=".csv,text/csv"
            className="mt-1 block"
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              reset();
            }}
          />
        </label>
        <div role="alert" className={errors.length ? "text-error" : "sr-only"}>
          {errors.length > 0 && (
            <ul className="list-disc pl-5 max-h-48 overflow-auto">
              {errors.map((msg) => (
                <li key={msg}>{msg}</li>
              ))}
            </ul>
          )}
        </div>
        <p role="status" className={status ? "text-gray-800" : "sr-only"}>
          {status}
        </p>
        {report && report.duplicates.length > 0 && (
          <div>
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
          <ul className="list-disc pl-5 text-amber-800">
            {report.warnings.map((w) => (
              <li key={w}>{w.split("_").join(" ")}</li>
            ))}
          </ul>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            Close
          </Button>
          <Button type="submit" variant="secondary" loading={busy && !ready}>
            Check file
          </Button>
          {ready && (
            <Button type="button" loading={busy} onClick={() => run(false)}>
              Import {report?.rows_valid} rows
            </Button>
          )}
        </div>
      </form>
    </Modal>
  );
}
