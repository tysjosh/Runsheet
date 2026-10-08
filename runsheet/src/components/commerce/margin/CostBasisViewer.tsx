"use client";

/**
 * Cost basis diagnostic: which method, lots, rack pick and exclusions the
 * resolver would use for a product at a terminal and time (fresh reads).
 */
import { type FormEvent, useState } from "react";
import { ApiError } from "../../../services/api";
import { type CostBasis, getCostBasis } from "../../../services/marginApi";
import { Button } from "../../ui";
import { MissingCostBadge } from "./MarginRecordsPage";
import {
  formatMicros,
  milliToDecimalString,
  noCostReasonLabel,
} from "./marginFormat";

const inputClass =
  "mt-1 block w-full rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40";

const METHOD_LABELS: Record<CostBasis["method"], string> = {
  override: "Manual override",
  wac: "Weighted average of purchases",
  rack_fallback: "Rack price fallback",
  none: "No cost",
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const errors =
      (error.details?.errors as { type?: string }[] | undefined) ?? [];
    if (errors.some((e) => e.type === "unknown_product"))
      return "That product is not known.";
    if (errors.some((e) => e.type === "unknown_terminal"))
      return "That terminal is not known.";
    if (error.status === 503)
      return "The cost basis could not be read. Try again.";
  }
  return "The cost basis could not be loaded. Try again.";
}

export default function CostBasisViewer() {
  const [product, setProduct] = useState("");
  const [terminal, setTerminal] = useState("");
  const [asOf, setAsOf] = useState("");
  const [basis, setBasis] = useState<CostBasis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      setBasis(
        await getCostBasis({
          product_code: product.trim(),
          terminal_id: terminal.trim() || undefined,
          as_of: asOf ? new Date(asOf).toISOString() : undefined,
        }),
      );
    } catch (e) {
      setBasis(null);
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  };

  const excluded = basis ? Object.entries(basis.diagnostics.excluded) : [];

  return (
    <section aria-labelledby="cost-basis-heading" className="space-y-4">
      <h2 id="cost-basis-heading" className="text-lg font-semibold">
        Cost basis
      </h2>
      <form
        onSubmit={onSubmit}
        className="flex flex-wrap items-end gap-3"
        aria-label="Cost basis lookup"
      >
        <label className="text-sm">
          Product
          <input
            required
            className={inputClass}
            value={product}
            onChange={(e) => setProduct(e.target.value)}
          />
        </label>
        <label className="text-sm">
          Terminal (optional)
          <input
            className={inputClass}
            value={terminal}
            onChange={(e) => setTerminal(e.target.value)}
          />
        </label>
        <label className="text-sm">
          As of (optional)
          <input
            type="datetime-local"
            className={inputClass}
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
          />
        </label>
        <Button type="submit" size="sm" loading={loading}>
          Show cost basis
        </Button>
      </form>
      <p role="alert" className={error ? "text-sm text-error" : "sr-only"}>
        {error ?? ""}
      </p>
      {basis && (
        <div className="space-y-3 text-sm">
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 max-w-xl">
            <dt className="text-gray-600">Method</dt>
            <dd>{METHOD_LABELS[basis.method]}</dd>
            <dt className="text-gray-600">Landed cost per gallon</dt>
            <dd>
              {basis.method === "none" ? (
                <MissingCostBadge reason={basis.no_cost_reason} />
              ) : (
                formatMicros(basis.landed_cost_micros)
              )}
            </dd>
            <dt className="text-gray-600">Product cost per gallon</dt>
            <dd>
              {basis.method === "none"
                ? noCostReasonLabel(basis.no_cost_reason)
                : formatMicros(basis.product_cost_micros)}
            </dd>
            <dt className="text-gray-600">Adders per gallon</dt>
            <dd>
              {basis.adders_micros === null
                ? "None configured"
                : formatMicros(basis.adders_micros)}
            </dd>
            <dt className="text-gray-600">Window</dt>
            <dd>
              {basis.window_start ?? "—"} to {basis.as_of} (
              {basis.wac_window_days} days)
            </dd>
            <dt className="text-gray-600">Terminal</dt>
            <dd>
              {basis.terminal_id ?? "Not attributed (tenant-wide entries only)"}
            </dd>
            <dt className="text-gray-600">BOLs scanned</dt>
            <dd>{basis.diagnostics.bols_scanned}</dd>
          </dl>
          <table className="min-w-full text-sm">
            <caption className="text-left font-medium pb-1">
              Lots ({basis.lots.length})
            </caption>
            <thead>
              <tr className="text-left text-gray-600 border-b">
                <th scope="col" className="py-1 pr-3">
                  Type
                </th>
                <th scope="col" className="py-1 pr-3">
                  Id
                </th>
                <th scope="col" className="py-1 pr-3 text-right">
                  Gallons
                </th>
                <th scope="col" className="py-1 pr-3 text-right">
                  Unit cost
                </th>
                <th scope="col" className="py-1">
                  Priced by
                </th>
              </tr>
            </thead>
            <tbody>
              {basis.lots.map((lot) => (
                <tr key={`${lot.lot_type}-${lot.id}`}>
                  <td className="py-1 pr-3">{lot.lot_type}</td>
                  <td className="py-1 pr-3">{lot.id}</td>
                  <td className="py-1 pr-3 text-right">
                    {milliToDecimalString(lot.gallons_milli)}
                  </td>
                  <td className="py-1 pr-3 text-right">
                    {formatMicros(lot.unit_cost_micros)}
                  </td>
                  <td className="py-1">{lot.priced_by}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div>
            <h3 className="font-medium">Excluded BOLs</h3>
            {excluded.length === 0 ? (
              <p className="text-gray-500">None</p>
            ) : (
              <ul className="list-disc pl-5">
                {excluded.map(([reason, count]) => (
                  <li key={reason}>
                    {reason.split("_").join(" ")}: {count}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
