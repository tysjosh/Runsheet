"use client";

/**
 * Cost entries (UI revamp task 3.4): status chips, a DataTable with products
 * by name, add and supersede in an md FormDialog, void in an sm FormDialog,
 * and CSV import. Entries are never edited in place; every change is a new
 * version and is audit-logged.
 */
import { Ban, FileUp, History, Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { dateTime, number } from "../../../lib/format";
import {
  type CostEntry,
  type CostEntryFilters,
  getCostEntries,
  voidCostEntry,
} from "../../../services/marginApi";
import type { StatusKey } from "../../../styles/tokens";
import {
  Button,
  type Column,
  DataTable,
  Field,
  FilterChips,
  FormDialog,
  INPUT_CLASS,
  ProductChip,
  StatusBadge,
  Toolbar,
  usePageChrome,
} from "../../ui";
import CostEntryForm, { apiFieldErrors } from "./CostEntryForm";
import CostImportDialog from "./CostImportDialog";
import { formatMicros } from "./marginFormat";

type StatusFilter = NonNullable<CostEntryFilters["status"]>;

const STATUS_CHIPS: { id: StatusFilter; label: string }[] = [
  { id: "active", label: "Active" },
  { id: "superseded", label: "Superseded" },
  { id: "voided", label: "Voided" },
  { id: "all", label: "All" },
];

const ENTRY_STATUS: Record<
  CostEntry["status"],
  { status: StatusKey; label: string }
> = {
  active: { status: "ok", label: "Active" },
  superseded: { status: "draft", label: "Superseded" },
  voided: { status: "cancelled", label: "Voided" },
};

const KIND_LABEL: Record<string, string> = {
  purchase: "Purchase",
  override: "Override",
  adder: "Adder",
};

export default function CostEntriesPage() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [items, setItems] = useState<CostEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [adding, setAdding] = useState(false);
  const [superseding, setSuperseding] = useState<CostEntry | null>(null);
  const [voiding, setVoiding] = useState<CostEntry | null>(null);
  const [importing, setImporting] = useState(false);

  const load = useCallback(
    async (filter: StatusFilter, after: string | null) => {
      setError(null);
      if (!after) setLoading(true);
      try {
        const page = await getCostEntries({
          status: filter,
          cursor: after ?? undefined,
        });
        setItems((prev) => (after ? [...prev, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } catch {
        setError("Cost entries could not be loaded. Try again.");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void load(statusFilter, null);
  }, [statusFilter, load]);

  const reload = (message: string) => {
    setStatus(message);
    void load(statusFilter, null);
  };

  const actions = useMemo(
    () => (
      <>
        <Button
          size="sm"
          variant="secondary"
          icon={<FileUp className="h-3.5 w-3.5" />}
          onClick={() => setImporting(true)}
        >
          Import CSV
        </Button>
        <Button
          size="sm"
          icon={<Plus className="h-3.5 w-3.5" />}
          onClick={() => setAdding(true)}
        >
          Add cost entry
        </Button>
      </>
    ),
    [],
  );
  const embedded = usePageChrome({ actions });

  const columns: Column<CostEntry>[] = [
    {
      key: "kind",
      header: "Kind",
      width: 140,
      className: "text-slate-700",
      cell: (e) =>
        `${KIND_LABEL[e.kind] ?? e.kind}${e.adder_type ? ` (${e.adder_type})` : ""}`,
    },
    {
      key: "product_code",
      header: "Product",
      width: 220,
      cell: (e) => <ProductChip code={e.product_code} />,
    },
    {
      key: "terminal_id",
      header: "Terminal",
      truncate: true,
      title: (e) => e.terminal_id ?? undefined,
      className: "text-slate-700",
      cell: (e) => e.terminal_id ?? "Tenant-wide",
    },
    {
      key: "effective_at",
      header: "Effective",
      width: 260,
      className: "whitespace-nowrap text-slate-700",
      cell: (e) =>
        `${dateTime(e.effective_at)}${e.effective_to ? ` – ${dateTime(e.effective_to)}` : ""}`,
    },
    {
      key: "unit_cost_micros",
      header: "Unit cost",
      align: "right",
      width: 120,
      className: "tabular-nums text-text",
      cell: (e) => formatMicros(e.unit_cost_micros),
    },
    {
      key: "gallons_milli",
      header: "Gallons",
      align: "right",
      width: 110,
      className: "tabular-nums text-slate-700",
      cell: (e) =>
        e.gallons_milli === null
          ? "—"
          : number(e.gallons_milli / 1000, {
              decimals: e.gallons_milli % 1000 === 0 ? 0 : 1,
            }),
    },
    {
      key: "reference",
      header: "Reference",
      truncate: true,
      title: (e) => e.bol_id ?? e.reference ?? undefined,
      className: "text-slate-700",
      cell: (e) => e.bol_id ?? e.reference ?? "—",
    },
    {
      key: "status",
      header: "Status",
      width: 130,
      cell: (e) => {
        const s = ENTRY_STATUS[e.status] ?? ENTRY_STATUS.active;
        return <StatusBadge status={s.status} label={s.label} />;
      },
    },
  ];

  return (
    <section
      aria-labelledby="cost-entries-heading"
      className="flex h-full flex-col"
    >
      <h2
        id="cost-entries-heading"
        className={embedded ? "sr-only" : "px-4 pt-3 text-base font-semibold"}
      >
        Cost entries
      </h2>
      <Toolbar
        label="Cost entries"
        filters={
          <FilterChips
            label="Cost entry status"
            options={STATUS_CHIPS.map((c) => ({
              id: c.id,
              label: c.label,
              status: c.id === "all" ? undefined : ENTRY_STATUS[c.id].status,
            }))}
            value={statusFilter}
            onChange={(v) => setStatusFilter(v as StatusFilter)}
          />
        }
        end={
          <>
            <p role="status" className="sr-only">
              {status}
            </p>
            {!embedded && actions}
          </>
        }
      />
      <div className="min-h-0 flex-1 overflow-auto">
        <DataTable<CostEntry>
          ariaLabel="Cost entries, newest first"
          columns={columns}
          data={loading || error ? [] : items}
          loading={loading}
          error={
            error
              ? { message: error, onRetry: () => load(statusFilter, null) }
              : null
          }
          getRowId={(e) => e.entry_id}
          rowLabel={(e) => `Cost entry ${e.entry_id}`}
          rowMenu={(e) =>
            e.status === "active"
              ? [
                  {
                    id: "supersede",
                    label: "Supersede",
                    icon: <History className="h-3.5 w-3.5" />,
                    onSelect: () => setSuperseding(e),
                  },
                  {
                    id: "void",
                    label: "Void",
                    danger: true,
                    icon: <Ban className="h-3.5 w-3.5" />,
                    onSelect: () => setVoiding(e),
                  },
                ]
              : []
          }
          emptyState={
            <p className="text-sm text-text-muted">No cost entries.</p>
          }
          footer={
            cursor ? (
              <div className="flex justify-center p-2">
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => load(statusFilter, cursor)}
                >
                  Load more
                </Button>
              </div>
            ) : undefined
          }
        />
      </div>

      {adding && (
        <CostEntryForm
          onSaved={() => reload("Cost entry added")}
          onClose={() => setAdding(false)}
        />
      )}
      {superseding && (
        <CostEntryForm
          entry={superseding}
          onSaved={() => reload("Cost entry superseded")}
          onClose={() => setSuperseding(null)}
        />
      )}
      {voiding && (
        <FormDialog<{ reason: string }, void>
          open
          size="sm"
          title="Void cost entry"
          help="Voided entries no longer cost any sale. This is audit-logged."
          submitLabel="Void entry"
          successMessage="Cost entry voided"
          initialValues={{ reason: "" }}
          validate={(v) => ({
            reason: v.reason.trim() ? undefined : "Enter a reason.",
          })}
          onSubmit={async (v) => {
            try {
              await voidCostEntry(voiding.entry_id, v.reason.trim());
            } catch (e) {
              throw new Error(apiFieldErrors(e).join(" "));
            }
          }}
          onSaved={() => reload("Cost entry voided")}
          onClose={() => setVoiding(null)}
        >
          {({ values, set, errors }) => (
            <Field label="Reason" required error={errors.reason}>
              <textarea
                id="void-cost-reason"
                rows={3}
                maxLength={500}
                className={`${INPUT_CLASS} h-auto py-1.5`}
                value={values.reason}
                onChange={(e) => set("reason", e.target.value)}
              />
            </Field>
          )}
        </FormDialog>
      )}
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
