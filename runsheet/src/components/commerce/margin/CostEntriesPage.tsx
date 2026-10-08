"use client";

/**
 * Cost entries: list with a status filter, add, supersede (the same form
 * prefilled plus a reason), void (reason dialog) and CSV import. Entries are
 * never edited in place; every change is a new version and is audit-logged.
 */
import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  type CostEntry,
  type CostEntryFilters,
  getCostEntries,
  voidCostEntry,
} from "../../../services/marginApi";
import { Button, Modal } from "../../ui";
import CostEntryForm, { apiFieldErrors } from "./CostEntryForm";
import CostImportDialog from "./CostImportDialog";
import { formatMicros, milliToDecimalString } from "./marginFormat";

const inputClass =
  "mt-1 block w-full rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40";

type StatusFilter = NonNullable<CostEntryFilters["status"]>;

export default function CostEntriesPage() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [items, setItems] = useState<CostEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [adding, setAdding] = useState(false);
  const [superseding, setSuperseding] = useState<CostEntry | null>(null);
  const [voiding, setVoiding] = useState<CostEntry | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voidErrors, setVoidErrors] = useState<string[]>([]);
  const [importing, setImporting] = useState(false);

  const load = useCallback(
    async (filter: StatusFilter, after: string | null) => {
      setError(null);
      try {
        const page = await getCostEntries({
          status: filter,
          cursor: after ?? undefined,
        });
        setItems((prev) => (after ? [...prev, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } catch {
        setError("Cost entries could not be loaded. Try again.");
      }
    },
    [],
  );

  useEffect(() => {
    void load(statusFilter, null);
  }, [statusFilter, load]);

  const saved = (message: string) => {
    setAdding(false);
    setSuperseding(null);
    setStatus(message);
    void load(statusFilter, null);
  };

  const onVoid = async (event: FormEvent) => {
    event.preventDefault();
    if (!voiding) return;
    if (!voidReason.trim()) {
      setVoidErrors(["Enter a reason."]);
      return;
    }
    try {
      await voidCostEntry(voiding.entry_id, voidReason.trim());
      setVoiding(null);
      setVoidReason("");
      setVoidErrors([]);
      saved("Cost entry voided");
    } catch (e) {
      setVoidErrors(apiFieldErrors(e));
    }
  };

  return (
    <section aria-labelledby="cost-entries-heading" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 id="cost-entries-heading" className="text-lg font-semibold">
          Cost entries
        </h2>
        <div className="flex items-end gap-2">
          <label className="text-sm">
            Status
            <select
              className={inputClass}
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
            >
              <option value="active">Active</option>
              <option value="superseded">Superseded</option>
              <option value="voided">Voided</option>
              <option value="all">All</option>
            </select>
          </label>
          <Button
            type="button"
            size="sm"
            variant="secondary"
            onClick={() => setImporting(true)}
          >
            Import CSV
          </Button>
          <Button type="button" size="sm" onClick={() => setAdding(true)}>
            Add cost entry
          </Button>
        </div>
      </div>
      <p role="alert" className={error ? "text-sm text-error" : "sr-only"}>
        {error ?? ""}
      </p>
      <p role="status" className={status ? "text-sm text-gray-800" : "sr-only"}>
        {status}
      </p>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <caption className="sr-only">Cost entries, newest first</caption>
          <thead>
            <tr className="text-left text-gray-600 border-b">
              <th scope="col" className="py-2 pr-3">
                Kind
              </th>
              <th scope="col" className="py-2 pr-3">
                Product
              </th>
              <th scope="col" className="py-2 pr-3">
                Terminal
              </th>
              <th scope="col" className="py-2 pr-3">
                Effective
              </th>
              <th scope="col" className="py-2 pr-3 text-right">
                Unit cost
              </th>
              <th scope="col" className="py-2 pr-3 text-right">
                Gallons
              </th>
              <th scope="col" className="py-2 pr-3">
                Reference
              </th>
              <th scope="col" className="py-2 pr-3">
                Status
              </th>
              <th scope="col" className="py-2">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((entry) => (
              <tr key={entry.entry_id} className="border-b last:border-0">
                <td className="py-2 pr-3">
                  {entry.kind}
                  {entry.adder_type ? ` (${entry.adder_type})` : ""}
                </td>
                <td className="py-2 pr-3">{entry.product_code}</td>
                <td className="py-2 pr-3">
                  {entry.terminal_id ?? "Tenant-wide"}
                </td>
                <td className="py-2 pr-3 whitespace-nowrap">
                  {entry.effective_at.slice(0, 16).replace("T", " ")}
                  {entry.effective_to
                    ? ` to ${entry.effective_to.slice(0, 16).replace("T", " ")}`
                    : ""}
                </td>
                <td className="py-2 pr-3 text-right">
                  {formatMicros(entry.unit_cost_micros)}
                </td>
                <td className="py-2 pr-3 text-right">
                  {entry.gallons_milli === null
                    ? "—"
                    : milliToDecimalString(entry.gallons_milli)}
                </td>
                <td className="py-2 pr-3">
                  {entry.bol_id ?? entry.reference ?? "—"}
                </td>
                <td className="py-2 pr-3">{entry.status}</td>
                <td className="py-2 whitespace-nowrap">
                  {entry.status === "active" && (
                    <span className="flex gap-1">
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        onClick={() => setSuperseding(entry)}
                      >
                        Supersede
                        <span className="sr-only"> entry {entry.entry_id}</span>
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        onClick={() => setVoiding(entry)}
                      >
                        Void
                        <span className="sr-only"> entry {entry.entry_id}</span>
                      </Button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {items.length === 0 && !error && (
          <p className="py-6 text-sm text-gray-500">No cost entries.</p>
        )}
      </div>
      {cursor && (
        <Button
          type="button"
          size="sm"
          variant="secondary"
          onClick={() => load(statusFilter, cursor)}
        >
          Load more
        </Button>
      )}

      <Modal
        isOpen={adding}
        onClose={() => setAdding(false)}
        title="Add cost entry"
        size="lg"
      >
        <CostEntryForm
          onSaved={() => saved("Cost entry added")}
          onCancel={() => setAdding(false)}
        />
      </Modal>
      <Modal
        isOpen={superseding !== null}
        onClose={() => setSuperseding(null)}
        title="Supersede cost entry"
        size="lg"
      >
        {superseding && (
          <CostEntryForm
            entry={superseding}
            onSaved={() => saved("Cost entry superseded")}
            onCancel={() => setSuperseding(null)}
          />
        )}
      </Modal>
      <Modal
        isOpen={voiding !== null}
        onClose={() => setVoiding(null)}
        title="Void cost entry"
        size="sm"
      >
        <form onSubmit={onVoid} className="space-y-3 text-sm">
          <label className="block">
            Reason
            <textarea
              required
              className={inputClass}
              value={voidReason}
              onChange={(e) => setVoidReason(e.target.value)}
              maxLength={500}
            />
          </label>
          <div
            role="alert"
            className={voidErrors.length ? "text-error" : "sr-only"}
          >
            {voidErrors.join(" ")}
          </div>
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setVoiding(null)}
            >
              Cancel
            </Button>
            <Button type="submit" variant="danger">
              Void entry
            </Button>
          </div>
        </form>
      </Modal>
      <CostImportDialog
        isOpen={importing}
        onClose={() => setImporting(false)}
        onImported={(count) => {
          setStatus(`Imported ${count} cost entries`);
          void load(statusFilter, null);
        }}
      />
    </section>
  );
}
