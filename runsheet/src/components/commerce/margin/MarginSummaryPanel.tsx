"use client";

/**
 * Margin summary (margin-feed design "Admin API" / summary).
 *
 * Revenue is split into "Revenue (costed)" and "Revenue (no cost)". Cost and
 * margin cover costed records only, so they pair with the costed revenue,
 * never with the total; a group with no costed record shows "No cost", not
 * "$0.00". Sources the feed could not compute are shown as "Sources not
 * computed".
 */
import { type FormEvent, useCallback, useEffect, useState } from "react";
import { ApiError } from "../../../services/api";
import {
  getMarginSummary,
  type MarginSummary,
  type SummaryBlock,
} from "../../../services/marginApi";
import { Button } from "../../ui";
import {
  formatBasisPoints,
  formatCents,
  formatCostedCents,
  formatGallons,
  formatPct,
} from "./marginFormat";

export const SUMMARY_LABELS = {
  revenueAll: "Revenue (all records)",
  revenueCosted: "Revenue (costed)",
  revenueNoCost: "Revenue (no cost)",
  cost: "Cost (costed records)",
  margin: "Margin (costed records)",
  marginPct: "Margin % (costed records)",
  missingShare: "Records with no cost",
  notComputed: "Sources not computed",
} as const;

const inputClass =
  "mt-1 block w-full rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40";

type GroupBy = MarginSummary["group_by"];

function totalsRows(block: SummaryBlock): [string, string][] {
  return [
    ["Records", String(block.records)],
    ["Gallons", formatGallons(block.gallons_ugal)],
    [SUMMARY_LABELS.revenueAll, formatCents(block.revenue_cents)],
    [SUMMARY_LABELS.revenueCosted, formatCents(block.revenue_cents_with_cost)],
    [
      SUMMARY_LABELS.revenueNoCost,
      formatCents(block.revenue_cents_missing_cost),
    ],
    [SUMMARY_LABELS.cost, formatCostedCents(block, block.cost_cents)],
    [SUMMARY_LABELS.margin, formatCostedCents(block, block.margin_cents)],
    [SUMMARY_LABELS.marginPct, formatPct(block.margin_pct)],
    [
      SUMMARY_LABELS.missingShare,
      formatBasisPoints(block.missing_cost_share_bp),
    ],
  ];
}

export default function MarginSummaryPanel() {
  const [groupBy, setGroupBy] = useState<GroupBy>("day");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [summary, setSummary] = useState<MarginSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(
    async (params: {
      group_by: GroupBy;
      start_date?: string;
      end_date?: string;
    }) => {
      setLoading(true);
      setError(null);
      try {
        setSummary(await getMarginSummary(params));
      } catch (e) {
        setError(
          e instanceof ApiError && e.status === 422
            ? "Pick a range of at most 92 days, with the end on or after the start."
            : "The margin summary could not be loaded. Try again.",
        );
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void load({ group_by: "day" });
  }, [load]);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void load({
      group_by: groupBy,
      start_date: startDate || undefined,
      end_date: endDate || undefined,
    });
  };

  return (
    <section aria-labelledby="margin-summary-heading" className="space-y-4">
      <h2 id="margin-summary-heading" className="text-lg font-semibold">
        Margin summary
      </h2>
      <form
        onSubmit={onSubmit}
        className="flex flex-wrap items-end gap-3"
        aria-label="Summary options"
      >
        <label className="text-sm">
          Group by
          <select
            className={inputClass}
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value as GroupBy)}
          >
            <option value="day">Day</option>
            <option value="customer">Customer</option>
            <option value="product">Product</option>
            <option value="terminal">Terminal</option>
          </select>
        </label>
        <label className="text-sm">
          Sale date (as of) from
          <input
            type="date"
            className={inputClass}
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
          />
        </label>
        <label className="text-sm">
          Sale date (as of) to
          <input
            type="date"
            className={inputClass}
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
          />
        </label>
        <Button type="submit" size="sm" loading={loading}>
          Show summary
        </Button>
      </form>
      <p role="alert" className={error ? "text-sm text-error" : "sr-only"}>
        {error ?? ""}
      </p>
      {summary && (
        <>
          <p className="text-xs text-gray-600">
            {summary.start_date} to {summary.end_date} ({summary.timezone}).
            Cost and margin cover costed records only.
          </p>
          {summary.skipped_sources.count > 0 && (
            <p className="text-sm text-amber-800">
              {SUMMARY_LABELS.notComputed}: {summary.skipped_sources.count}
            </p>
          )}
          <table className="text-sm">
            <caption className="text-left font-medium pb-1">Totals</caption>
            <tbody>
              {totalsRows(summary.totals).map(([label, value]) => (
                <tr key={label}>
                  <th
                    scope="row"
                    className="pr-6 py-1 text-left font-normal text-gray-600"
                  >
                    {label}
                  </th>
                  <td className="py-1 text-right">{value}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {summary.groups.length > 0 && (
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <caption className="text-left font-medium pb-1">
                  By {summary.group_by}
                </caption>
                <thead>
                  <tr className="text-left text-gray-600 border-b">
                    <th scope="col" className="py-2 pr-3">
                      Group
                    </th>
                    <th scope="col" className="py-2 pr-3 text-right">
                      Records
                    </th>
                    <th scope="col" className="py-2 pr-3 text-right">
                      {SUMMARY_LABELS.revenueCosted}
                    </th>
                    <th scope="col" className="py-2 pr-3 text-right">
                      {SUMMARY_LABELS.revenueNoCost}
                    </th>
                    <th scope="col" className="py-2 pr-3 text-right">
                      {SUMMARY_LABELS.cost}
                    </th>
                    <th scope="col" className="py-2 pr-3 text-right">
                      {SUMMARY_LABELS.margin}
                    </th>
                    <th scope="col" className="py-2 text-right">
                      Margin %
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {summary.groups.map((g) => (
                    <tr key={g.key} className="border-b last:border-0">
                      <th
                        scope="row"
                        className="py-2 pr-3 text-left font-normal"
                      >
                        {g.key || "—"}
                      </th>
                      <td className="py-2 pr-3 text-right">{g.records}</td>
                      <td className="py-2 pr-3 text-right">
                        {formatCents(g.revenue_cents_with_cost)}
                      </td>
                      <td className="py-2 pr-3 text-right">
                        {formatCents(g.revenue_cents_missing_cost)}
                      </td>
                      <td className="py-2 pr-3 text-right">
                        {formatCostedCents(g, g.cost_cents)}
                      </td>
                      <td className="py-2 pr-3 text-right">
                        {formatCostedCents(g, g.margin_cents)}
                      </td>
                      <td className="py-2 text-right">
                        {formatPct(g.margin_pct)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
