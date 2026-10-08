"use client";

/**
 * Recompute margin records over a sale-date (as_of) range: the only backfill
 * and late-BOL path (margin-feed FR3.5). Polls the run every 5 seconds until
 * it completes or fails.
 */
import { type FormEvent, useEffect, useRef, useState } from "react";
import { ApiError } from "../../../services/api";
import {
  getMarginRecomputeRun,
  type RecomputeRun,
  startMarginRecompute,
} from "../../../services/marginApi";
import { Button } from "../../ui";
import { apiFieldErrors } from "./CostEntryForm";

export const RECOMPUTE_POLL_MS = 5_000;
export const RECOMPUTE_HELP =
  "Uses the sale date shown in Records. Invoices created more than 15 days after the end date, or orders delivered more than 30 days after creation, are not reached.";
export const RECOMPUTE_RUNNING_MESSAGE = "A recompute is already running";

const inputClass =
  "mt-1 block w-full rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40";

function countsText(run: RecomputeRun): string {
  const counts = (run.counts ?? {}) as Record<string, unknown>;
  const n = (key: string) =>
    typeof counts[key] === "number" ? (counts[key] as number) : 0;
  return `${n("sources")} sources, ${n("written")} written, ${n("skipped")} unchanged, ${n("invalid_inputs")} invalid, ${n("errors")} errors`;
}

export default function MarginRecomputePanel() {
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [invoices, setInvoices] = useState(true);
  const [deliveries, setDeliveries] = useState(true);
  const [onlyMissing, setOnlyMissing] = useState(true);
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const run = await getMarginRecomputeRun(runId);
        if (cancelled) return;
        if (run.status === "running") {
          setStatus("Recompute running…");
          timer.current = setTimeout(poll, RECOMPUTE_POLL_MS);
          return;
        }
        setStatus(
          run.status === "completed"
            ? `Recompute completed: ${countsText(run)}.`
            : `Recompute failed: ${countsText(run)}. Run it again; it is safe to repeat.`,
        );
        setRunId(null);
      } catch {
        if (!cancelled) timer.current = setTimeout(poll, RECOMPUTE_POLL_MS);
      }
    };
    timer.current = setTimeout(poll, RECOMPUTE_POLL_MS);
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [runId]);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const problems: string[] = [];
    if (!startDate || !endDate) problems.push("Enter both sale dates.");
    if (!invoices && !deliveries)
      problems.push("Choose invoices, deliveries or both.");
    if (!reason.trim()) problems.push("Enter a reason.");
    if (problems.length) {
      setErrors(problems);
      return;
    }
    setSubmitting(true);
    setErrors([]);
    setStatus("");
    try {
      const { run_id } = await startMarginRecompute({
        start_date: startDate,
        end_date: endDate,
        stages: [
          ...(invoices ? (["invoice"] as const) : []),
          ...(deliveries ? (["delivery"] as const) : []),
        ],
        only_missing: onlyMissing,
        reason: reason.trim(),
      });
      setStatus("Recompute started");
      setRunId(run_id);
    } catch (e) {
      setErrors(
        e instanceof ApiError && e.status === 409
          ? [RECOMPUTE_RUNNING_MESSAGE]
          : apiFieldErrors(e),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section aria-labelledby="margin-recompute-heading" className="space-y-3">
      <h2 id="margin-recompute-heading" className="text-lg font-semibold">
        Recompute margin
      </h2>
      <p id="margin-recompute-help" className="text-sm text-gray-600 max-w-2xl">
        {RECOMPUTE_HELP}
      </p>
      <form
        onSubmit={onSubmit}
        className="grid grid-cols-2 gap-3 max-w-2xl"
        aria-describedby="margin-recompute-help"
      >
        <label className="text-sm">
          Sale date (as of) from
          <input
            type="date"
            required
            className={inputClass}
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
          />
        </label>
        <label className="text-sm">
          Sale date (as of) to
          <input
            type="date"
            required
            className={inputClass}
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
          />
        </label>
        <fieldset className="text-sm col-span-2 flex gap-4">
          <legend className="sr-only">Stages</legend>
          <label className="inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={invoices}
              onChange={(e) => setInvoices(e.target.checked)}
            />
            Invoices
          </label>
          <label className="inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={deliveries}
              onChange={(e) => setDeliveries(e.target.checked)}
            />
            Deliveries
          </label>
          <label className="inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={onlyMissing}
              onChange={(e) => setOnlyMissing(e.target.checked)}
            />
            Only records with no cost
          </label>
        </fieldset>
        <label className="text-sm col-span-2">
          Reason
          <textarea
            required
            className={inputClass}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={500}
          />
        </label>
        <div
          role="alert"
          className={
            errors.length ? "col-span-2 text-sm text-error" : "sr-only"
          }
        >
          {errors.join(" ")}
        </div>
        <p
          role="status"
          className={status ? "col-span-2 text-sm text-gray-800" : "sr-only"}
        >
          {status}
        </p>
        <div className="col-span-2 flex justify-end">
          <Button type="submit" loading={submitting} disabled={runId !== null}>
            Start recompute
          </Button>
        </div>
      </form>
    </section>
  );
}
