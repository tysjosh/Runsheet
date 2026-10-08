"use client";

/**
 * Margin records: filters, a keyset-paged table, a detail drawer and the CSV
 * export (margin-feed design "Admin UI"). A `method=none` record shows a
 * "Missing cost" badge and its reason in place of the cost, margin and
 * percentage cells; it never shows $0.00 (Simplification 12).
 */
import { type FormEvent, useCallback, useEffect, useState } from "react";
import { ApiError } from "../../../services/api";
import {
  getMarginRecord,
  getMarginRecords,
  type MarginFlag,
  type MarginRecord,
  type MarginRecordDetail,
  type MarginRecordFilters,
  recordFilterParams,
} from "../../../services/marginApi";
import { Button, ExportCsvButton, Modal } from "../../ui";
import {
  FLAG_LABELS,
  formatCents,
  formatGallons,
  formatMicros,
  formatPct,
  MISSING_COST,
  noCostReasonLabel,
} from "./marginFormat";

const EMPTY_FILTERS: MarginRecordFilters = {
  start_date: "",
  end_date: "",
  customer_id: "",
  product_code: "",
  terminal_id: "",
  stage: "",
  flag: "",
  status: "active",
};

const FLAG_TONE: Record<string, string> = {
  negative_margin: "bg-red-100 text-red-800",
  below_floor: "bg-amber-100 text-amber-800",
  terminal_unattributed: "bg-gray-100 text-gray-700",
  missing_cost: "bg-orange-100 text-orange-800",
};

/** "Missing cost" badge plus the reason: text and colour, never colour alone. */
export function MissingCostBadge({ reason }: { reason: string | null }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <span className="rounded px-2 py-0.5 text-xs font-semibold bg-orange-100 text-orange-800">
        {MISSING_COST}
      </span>
      <span className="text-xs text-gray-600">{noCostReasonLabel(reason)}</span>
    </span>
  );
}

/** Flag badges. `missing_cost` is shown by the cost cell, so it is skipped here. */
export function FlagBadges({ flags }: { flags: MarginFlag[] }) {
  const shown = flags.filter((f) => f !== "missing_cost");
  if (shown.length === 0) return <span className="text-gray-400">—</span>;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {shown.map((flag) => (
        <span
          key={flag}
          className={`rounded px-2 py-0.5 text-xs font-medium ${FLAG_TONE[flag] ?? "bg-gray-100 text-gray-700"}`}
        >
          {FLAG_LABELS[flag] ?? flag}
        </span>
      ))}
    </span>
  );
}

function errorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 422) {
    return "Check the filters: one of them is not valid.";
  }
  return "Margin records could not be loaded. Try again.";
}

const inputClass =
  "mt-1 block w-full rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40";

export default function MarginRecordsPage() {
  const [draft, setDraft] = useState<MarginRecordFilters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<MarginRecordFilters>(EMPTY_FILTERS);
  const [items, setItems] = useState<MarginRecord[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<MarginRecordDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const load = useCallback(
    async (filters: MarginRecordFilters, after: string | null) => {
      setLoading(true);
      setError(null);
      try {
        const page = await getMarginRecords(filters, after);
        setItems((prev) => (after ? [...prev, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } catch (e) {
        setError(errorText(e));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void load(applied, null);
  }, [applied, load]);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setApplied({ ...draft });
  };

  const field =
    (name: keyof MarginRecordFilters) => (e: { target: { value: string } }) =>
      setDraft((d) => ({ ...d, [name]: e.target.value }));

  const openDetail = async (recordId: string) => {
    setDetailError(null);
    try {
      setDetail(await getMarginRecord(recordId));
    } catch {
      setDetailError("The record could not be loaded. Try again.");
    }
  };

  return (
    <section aria-labelledby="margin-records-heading" className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <h2 id="margin-records-heading" className="text-lg font-semibold">
          Margin records
        </h2>
        <ExportCsvButton
          type="margin"
          params={recordFilterParams(applied)}
          allowedRoles={["admin"]}
          subject="margin records"
        />
      </div>

      <form
        onSubmit={onSubmit}
        className="grid grid-cols-2 gap-3 md:grid-cols-4"
        aria-label="Margin record filters"
      >
        <label className="text-sm">
          Sale date (as of) from
          <input
            type="date"
            className={inputClass}
            value={draft.start_date}
            onChange={field("start_date")}
          />
        </label>
        <label className="text-sm">
          Sale date (as of) to
          <input
            type="date"
            className={inputClass}
            value={draft.end_date}
            onChange={field("end_date")}
          />
        </label>
        <label className="text-sm">
          Customer
          <input
            type="text"
            className={inputClass}
            value={draft.customer_id}
            onChange={field("customer_id")}
          />
        </label>
        <label className="text-sm">
          Product
          <input
            type="text"
            className={inputClass}
            value={draft.product_code}
            onChange={field("product_code")}
          />
        </label>
        <label className="text-sm">
          Terminal
          <input
            type="text"
            className={inputClass}
            value={draft.terminal_id}
            onChange={field("terminal_id")}
          />
        </label>
        <label className="text-sm">
          Stage
          <select
            className={inputClass}
            value={draft.stage}
            onChange={field("stage")}
          >
            <option value="">Any stage</option>
            <option value="invoice">Invoice</option>
            <option value="delivery">Delivery</option>
            <option value="order_estimate">Order estimate</option>
          </select>
        </label>
        <label className="text-sm">
          Flag
          <select
            className={inputClass}
            value={draft.flag}
            onChange={field("flag")}
          >
            <option value="">Any flag</option>
            <option value="missing_cost">Missing cost</option>
            <option value="negative_margin">Negative margin</option>
            <option value="below_floor">Below floor</option>
            <option value="terminal_unattributed">No terminal</option>
          </select>
        </label>
        <label className="text-sm">
          Status
          <select
            className={inputClass}
            value={draft.status}
            onChange={field("status")}
          >
            <option value="active">Active</option>
            <option value="superseded">Superseded</option>
            <option value="void">Void</option>
            <option value="all">All</option>
          </select>
        </label>
        <div className="col-span-2 md:col-span-4">
          <Button type="submit" size="sm">
            Apply filters
          </Button>
        </div>
      </form>

      <p role="alert" className={error ? "text-sm text-error" : "sr-only"}>
        {error ?? ""}
      </p>

      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <caption className="sr-only">
            Margin records, newest sale first
          </caption>
          <thead>
            <tr className="text-left text-gray-600 border-b">
              <th scope="col" className="py-2 pr-3">
                Sale date
              </th>
              <th scope="col" className="py-2 pr-3">
                Order
              </th>
              <th scope="col" className="py-2 pr-3">
                Stage
              </th>
              <th scope="col" className="py-2 pr-3">
                Customer
              </th>
              <th scope="col" className="py-2 pr-3">
                Product
              </th>
              <th scope="col" className="py-2 pr-3">
                Terminal
              </th>
              <th scope="col" className="py-2 pr-3 text-right">
                Gallons
              </th>
              <th scope="col" className="py-2 pr-3 text-right">
                Revenue
              </th>
              <th scope="col" className="py-2 pr-3 text-right">
                Cost
              </th>
              <th scope="col" className="py-2 pr-3 text-right">
                Margin
              </th>
              <th scope="col" className="py-2 pr-3 text-right">
                Margin %
              </th>
              <th scope="col" className="py-2 pr-3">
                Flags
              </th>
              <th scope="col" className="py-2">
                <span className="sr-only">Details</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.record_id} className="border-b last:border-0">
                <td className="py-2 pr-3 whitespace-nowrap">
                  {r.as_of.slice(0, 10)}
                </td>
                <td className="py-2 pr-3">{r.order_id ?? "—"}</td>
                <td className="py-2 pr-3">{r.stage}</td>
                <td className="py-2 pr-3">{r.customer_id ?? "—"}</td>
                <td className="py-2 pr-3">{r.product_code}</td>
                <td className="py-2 pr-3">{r.terminal_id ?? "—"}</td>
                <td className="py-2 pr-3 text-right">
                  {formatGallons(r.gallons_ugal)}
                </td>
                <td className="py-2 pr-3 text-right">
                  {formatCents(r.revenue_cents)}
                </td>
                {r.method === "none" ? (
                  <td colSpan={3} className="py-2 pr-3">
                    <MissingCostBadge reason={r.no_cost_reason} />
                  </td>
                ) : (
                  <>
                    <td className="py-2 pr-3 text-right">
                      {formatCents(r.cost_cents)}
                    </td>
                    <td className="py-2 pr-3 text-right">
                      {formatCents(r.margin_cents)}
                    </td>
                    <td className="py-2 pr-3 text-right">
                      {formatPct(r.margin_pct)}
                    </td>
                  </>
                )}
                <td className="py-2 pr-3">
                  <FlagBadges flags={r.flags} />
                </td>
                <td className="py-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => openDetail(r.record_id)}
                  >
                    Details
                    <span className="sr-only"> for record {r.record_id}</span>
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && items.length === 0 && !error && (
          <p className="py-6 text-sm text-gray-500">
            No margin records match these filters.
          </p>
        )}
      </div>

      <p role="status" className="sr-only">
        {loading
          ? "Loading margin records"
          : `${items.length} margin records shown`}
      </p>
      {cursor && (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          loading={loading}
          onClick={() => load(applied, cursor)}
        >
          Load more
        </Button>
      )}
      <p
        role="alert"
        className={detailError ? "text-sm text-error" : "sr-only"}
      >
        {detailError ?? ""}
      </p>

      <Modal
        isOpen={detail !== null}
        onClose={() => setDetail(null)}
        title={detail ? `Margin record ${detail.record_id}` : "Margin record"}
        size="xl"
      >
        {detail && <RecordDetail record={detail} />}
      </Modal>
    </section>
  );
}

function CostCells({ record }: { record: MarginRecord }) {
  if (record.method === "none")
    return <MissingCostBadge reason={record.no_cost_reason} />;
  return (
    <span>
      Cost {formatCents(record.cost_cents)} · Margin{" "}
      {formatCents(record.margin_cents)} · {formatPct(record.margin_pct)}
    </span>
  );
}

function RecordDetail({ record }: { record: MarginRecordDetail }) {
  return (
    <div className="space-y-4 text-sm">
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2">
        <dt className="text-gray-600">Stage</dt>
        <dd>{record.stage}</dd>
        <dt className="text-gray-600">Order / invoice</dt>
        <dd>
          {record.order_id ?? "—"} / {record.invoice_id ?? "—"}
        </dd>
        <dt className="text-gray-600">Unit price</dt>
        <dd>{formatMicros(record.unit_price_micros)}</dd>
        <dt className="text-gray-600">Cost method</dt>
        <dd>{record.method}</dd>
        <dt className="text-gray-600">Landed cost per gallon</dt>
        <dd>
          {record.method === "none" ? (
            <MissingCostBadge reason={record.no_cost_reason} />
          ) : (
            formatMicros(record.landed_cost_micros)
          )}
        </dd>
        <dt className="text-gray-600">Cost and margin</dt>
        <dd>
          <CostCells record={record} />
        </dd>
        <dt className="text-gray-600">Floor used</dt>
        <dd>{formatMicros(record.floor_micros_used)} per gallon</dd>
        <dt className="text-gray-600">Flags</dt>
        <dd>
          <FlagBadges flags={record.flags} />
        </dd>
      </dl>
      <div>
        <h3 className="font-semibold">Cost snapshot</h3>
        <pre className="mt-1 max-h-64 overflow-auto rounded bg-gray-50 p-2 text-xs">
          {JSON.stringify(record.cost_snapshot, null, 2)}
        </pre>
      </div>
      <div>
        <h3 className="font-semibold">Version history</h3>
        <table className="mt-1 min-w-full text-xs">
          <thead>
            <tr className="text-left text-gray-600">
              <th scope="col" className="pr-3">
                Version
              </th>
              <th scope="col" className="pr-3">
                Status
              </th>
              <th scope="col" className="pr-3">
                Origin
              </th>
              <th scope="col" className="pr-3">
                Cost and margin
              </th>
              <th scope="col">Computed</th>
            </tr>
          </thead>
          <tbody>
            {record.versions.map((v) => (
              <tr key={v.record_id}>
                <td className="pr-3">{v.version}</td>
                <td className="pr-3">{v.status}</td>
                <td className="pr-3">{v.origin}</td>
                <td className="pr-3">
                  <CostCells record={v} />
                </td>
                <td>{v.computed_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
