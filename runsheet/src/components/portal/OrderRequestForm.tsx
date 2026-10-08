"use client";

/**
 * Delivery request form (R4.6, R4.7, PD9, PD10; design §10.2).
 *
 * - Every input has a label; field errors are linked with `aria-describedby`
 *   and summarized in a `role="alert"` region that takes focus on submit.
 * - The date is a native date input bounded to local today … today + 60 days
 *   (the server stays authoritative); the optional window is two time inputs.
 *   A date alone is sent as local midnight to the next local midnight.
 * - While ordering is unavailable, or after a 409 ORDER_INTAKE_DISABLED, the
 *   PD10 message is shown in a `role="status"` region, submit is
 *   `aria-disabled`, every value is kept and nothing is retried. The handler
 *   returns early while disabled, because `aria-disabled` doesn't block
 *   activation. `client_event_id` is kept, so a later manual retry is
 *   idempotent.
 */

import Link from "next/link";
import { useId, useMemo, useRef, useState } from "react";
import { ApiError } from "../../services/api";
import {
  createPortalOrder,
  hasErrorCode,
  isRateLimited,
  type PortalOrder,
  type PortalOrderRequest,
  type PortalTank,
  rateLimitMessage,
} from "../../services/portalApi";
import { formatNumber } from "./format";
import { localIsoDate, newUuid } from "./ids";
import LiveRegion from "./LiveRegion";
import { ORDERING_UNAVAILABLE_MESSAGE } from "./messages";
import {
  fieldError,
  fieldHint,
  fieldInput,
  fieldLabel,
  primaryButton,
  secondaryButton,
  textLink,
} from "./styles";

export { ORDERING_UNAVAILABLE_MESSAGE };

/** PD9 horizon: today to today + 60 days. */
export const MAX_DAYS_AHEAD = 60;

type Field =
  | "tank"
  | "gallons"
  | "date"
  | "time"
  | "po_number"
  | "notes"
  | "form";

type Errors = Partial<Record<Field, string>>;

const FIELD_ORDER: Field[] = [
  "form",
  "tank",
  "gallons",
  "date",
  "time",
  "po_number",
  "notes",
];

function parseTime(value: string): [number, number] | null {
  const m = /^(\d{2}):(\d{2})$/.exec(value);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

/** The request window as ISO-8601 instants (the browser's offset applied). */
export function buildWindow(
  date: string,
  startTime: string,
  endTime: string,
): { window_start: string; window_end: string } {
  const [y, m, d] = date.split("-").map(Number);
  const start = parseTime(startTime);
  const end = parseTime(endTime);
  const from =
    start && end
      ? new Date(y, m - 1, d, start[0], start[1])
      : new Date(y, m - 1, d);
  const to =
    start && end
      ? new Date(y, m - 1, d, end[0], end[1])
      : new Date(y, m - 1, d + 1);
  return { window_start: from.toISOString(), window_end: to.toISOString() };
}

export default function OrderRequestForm({
  tanks,
  orderingAvailable,
  volumeUnit = "gal",
  onCreated,
}: {
  tanks: PortalTank[];
  orderingAvailable: boolean;
  volumeUnit?: string;
  onCreated?: (order: PortalOrder) => void;
}) {
  const uid = useId();
  const ids = {
    tank: `${uid}-tank`,
    fill: `${uid}-fill`,
    gallonsMode: `${uid}-gallons-mode`,
    gallons: `${uid}-gallons`,
    date: `${uid}-date`,
    start: `${uid}-start`,
    end: `${uid}-end`,
    po: `${uid}-po`,
    notes: `${uid}-notes`,
    summary: `${uid}-summary`,
  };
  const errorId = (f: Field) => `${uid}-${f}-error`;

  const [tankId, setTankId] = useState(tanks[0]?.customer_tank_id ?? "");
  const [mode, setMode] = useState<"fill_to_full" | "gallons">("fill_to_full");
  const [gallons, setGallons] = useState("");
  const [date, setDate] = useState("");
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [poNumber, setPoNumber] = useState("");
  const [notes, setNotes] = useState("");
  const [clientEventId, setClientEventId] = useState(newUuid);

  const [errors, setErrors] = useState<Errors>({});
  const [intakeDisabled, setIntakeDisabled] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [created, setCreated] = useState<PortalOrder | null>(null);
  const summaryRef = useRef<HTMLDivElement>(null);
  const createdRef = useRef<HTMLHeadingElement>(null);
  const inFlight = useRef(false);

  const minDate = useMemo(() => localIsoDate(0), []);
  const maxDate = useMemo(() => localIsoDate(MAX_DAYS_AHEAD), []);
  const tank = tanks.find((t) => t.customer_tank_id === tankId) ?? null;

  const unavailable = !orderingAvailable || intakeDisabled;
  const disabled = unavailable || submitting;
  const statusMessage = unavailable ? ORDERING_UNAVAILABLE_MESSAGE : notice;

  function validate(): Errors {
    const next: Errors = {};
    if (!tank) next.tank = "Choose a tank.";
    if (mode === "gallons") {
      const value = Number(gallons);
      if (!gallons.trim() || !Number.isFinite(value) || value <= 0) {
        next.gallons = "Enter a number of gallons greater than 0.";
      } else if (tank && value > tank.capacity_gallons) {
        next.gallons = `Enter no more than ${formatNumber(tank.capacity_gallons)} gallons, the tank's capacity.`;
      }
    }
    if (!date) {
      next.date = "Choose a delivery date.";
    } else if (date < minDate || date > maxDate) {
      next.date = `Choose a date from today to ${MAX_DAYS_AHEAD} days ahead.`;
    }
    if (Boolean(startTime) !== Boolean(endTime)) {
      next.time = "Enter both a start and an end time, or leave both empty.";
    } else if (startTime && endTime && endTime <= startTime) {
      next.time = "The end time must be after the start time.";
    }
    if (poNumber.trim().length > 64) {
      next.po_number = "Use 64 characters or fewer for the PO number.";
    }
    if (notes.trim().length > 500) {
      next.notes = "Use 500 characters or fewer for the notes.";
    }
    return next;
  }

  function showErrors(next: Errors) {
    setErrors(next);
    // Let the summary render, then move focus to it.
    setTimeout(() => summaryRef.current?.focus(), 0);
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled || inFlight.current) return;
    setNotice(null);
    const next = validate();
    if (Object.keys(next).length > 0) {
      showErrors(next);
      return;
    }
    setErrors({});
    if (!tank) return;

    const body: PortalOrderRequest = {
      client_event_id: clientEventId,
      customer_tank_id: tank.customer_tank_id,
      quantity:
        mode === "gallons"
          ? { mode: "gallons", gallons: Number(gallons) }
          : { mode: "fill_to_full" },
      ...buildWindow(date, startTime, endTime),
      ...(poNumber.trim() ? { po_number: poNumber.trim() } : {}),
      ...(notes.trim() ? { notes: notes.trim() } : {}),
    };

    inFlight.current = true;
    setSubmitting(true);
    try {
      const response = await createPortalOrder(body);
      setCreated(response.data);
      onCreated?.(response.data);
      setTimeout(() => createdRef.current?.focus(), 0);
    } catch (error) {
      if (hasErrorCode(error, "ORDER_INTAKE_DISABLED")) {
        // PD10: an expected state, not an error. Keep every value; no retry.
        setIntakeDisabled(true);
      } else if (isRateLimited(error)) {
        setNotice(rateLimitMessage(error));
      } else if (hasErrorCode(error, "IDEMPOTENCY_CONFLICT")) {
        // The server asks for a new reference; the next submit uses one.
        setClientEventId(newUuid());
        showErrors({
          form:
            error instanceof ApiError
              ? error.message
              : "Please submit the request again.",
        });
      } else if (error instanceof ApiError && error.status === 404) {
        showErrors({
          tank: "That tank isn't available for online ordering. Choose another tank.",
        });
      } else if (error instanceof ApiError && error.status === 422) {
        showErrors({ form: error.message });
      } else {
        showErrors({
          form: "We couldn't send your request. Please try again.",
        });
      }
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  function startAnother() {
    setCreated(null);
    setClientEventId(newUuid());
    setGallons("");
    setDate("");
    setStartTime("");
    setEndTime("");
    setPoNumber("");
    setNotes("");
  }

  if (created) {
    return (
      <section aria-labelledby={`${uid}-created`} className="space-y-3">
        <h2
          id={`${uid}-created`}
          ref={createdRef}
          tabIndex={-1}
          className="text-lg font-semibold text-gray-900 focus:outline-none"
        >
          Request sent
        </h2>
        <p className="text-sm text-gray-800">
          Status: {created.status_label}. Your supplier will confirm the
          delivery.
        </p>
        <div className="flex flex-wrap gap-3">
          <Link href="/portal/orders" className={textLink}>
            View your orders
          </Link>
          <button
            type="button"
            className={secondaryButton}
            onClick={startAnother}
          >
            Request another delivery
          </button>
        </div>
      </section>
    );
  }

  const errorEntries = FIELD_ORDER.filter((f) => errors[f]).map(
    (f) => [f, errors[f] as string] as const,
  );
  const describe = (f: Field, hint?: string) =>
    [hint, errors[f] ? errorId(f) : undefined].filter(Boolean).join(" ") ||
    undefined;
  const anchorFor: Record<Field, string> = {
    form: ids.tank,
    tank: ids.tank,
    gallons: ids.gallons,
    date: ids.date,
    time: ids.start,
    po_number: ids.po,
    notes: ids.notes,
  };

  return (
    <form noValidate onSubmit={handleSubmit} className="space-y-5">
      <LiveRegion message={statusMessage} />

      {errorEntries.length > 0 && (
        <div
          ref={summaryRef}
          id={ids.summary}
          role="alert"
          tabIndex={-1}
          className="rounded-lg border border-error-light bg-error-light px-4 py-3 focus:outline-none focus-visible:ring-2 focus-visible:ring-error-dark"
        >
          <p className="text-sm font-semibold text-error-dark">
            Please fix the following:
          </p>
          <ul className="mt-1 list-disc pl-5 text-sm text-error-dark">
            {errorEntries.map(([field, message]) => (
              <li key={field}>
                <a href={`#${anchorFor[field]}`} className="underline">
                  {message}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <label htmlFor={ids.tank} className={fieldLabel}>
          Tank
        </label>
        <select
          id={ids.tank}
          value={tankId}
          onChange={(e) => setTankId(e.target.value)}
          aria-invalid={errors.tank ? true : undefined}
          aria-describedby={describe("tank")}
          className={fieldInput}
        >
          {tanks.length === 0 && <option value="">No tanks available</option>}
          {tanks.map((t) => (
            <option key={t.customer_tank_id} value={t.customer_tank_id}>
              {`${t.label} — ${t.product_code}, ${formatNumber(t.capacity_gallons)} ${volumeUnit}`}
            </option>
          ))}
        </select>
        {errors.tank && (
          <p id={errorId("tank")} className={fieldError}>
            {errors.tank}
          </p>
        )}
      </div>

      <fieldset>
        <legend className={fieldLabel}>Quantity</legend>
        <div className="mt-2 flex flex-wrap gap-4">
          <label
            htmlFor={ids.fill}
            className="inline-flex min-h-6 items-center gap-2 text-sm text-gray-900"
          >
            <input
              id={ids.fill}
              type="radio"
              name={`${uid}-mode`}
              value="fill_to_full"
              checked={mode === "fill_to_full"}
              onChange={() => setMode("fill_to_full")}
              className="h-5 w-5"
            />
            Fill to full
          </label>
          <label
            htmlFor={ids.gallonsMode}
            className="inline-flex min-h-6 items-center gap-2 text-sm text-gray-900"
          >
            <input
              id={ids.gallonsMode}
              type="radio"
              name={`${uid}-mode`}
              value="gallons"
              checked={mode === "gallons"}
              onChange={() => setMode("gallons")}
              className="h-5 w-5"
            />
            Gallons
          </label>
        </div>
        {mode === "gallons" && (
          <div className="mt-3">
            <label htmlFor={ids.gallons} className={fieldLabel}>
              Gallons requested
            </label>
            <input
              id={ids.gallons}
              type="number"
              inputMode="decimal"
              min={0}
              step="any"
              value={gallons}
              onChange={(e) => setGallons(e.target.value)}
              aria-invalid={errors.gallons ? true : undefined}
              aria-describedby={describe(
                "gallons",
                tank ? `${ids.gallons}-hint` : undefined,
              )}
              className={fieldInput}
            />
            {tank && (
              <p id={`${ids.gallons}-hint`} className={fieldHint}>
                Up to {formatNumber(tank.capacity_gallons)} {volumeUnit}.
              </p>
            )}
            {errors.gallons && (
              <p id={errorId("gallons")} className={fieldError}>
                {errors.gallons}
              </p>
            )}
          </div>
        )}
      </fieldset>

      <div>
        <label htmlFor={ids.date} className={fieldLabel}>
          Delivery date
        </label>
        <input
          id={ids.date}
          type="date"
          min={minDate}
          max={maxDate}
          value={date}
          onChange={(e) => setDate(e.target.value)}
          aria-invalid={errors.date ? true : undefined}
          aria-describedby={describe("date", `${ids.date}-hint`)}
          className={fieldInput}
        />
        <p id={`${ids.date}-hint`} className={fieldHint}>
          From today up to {MAX_DAYS_AHEAD} days ahead.
        </p>
        {errors.date && (
          <p id={errorId("date")} className={fieldError}>
            {errors.date}
          </p>
        )}
      </div>

      <fieldset>
        <legend className={fieldLabel}>Delivery window (optional)</legend>
        <p id={`${ids.start}-hint`} className={fieldHint}>
          Leave both empty for any time that day.
        </p>
        <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor={ids.start} className={fieldLabel}>
              From
            </label>
            <input
              id={ids.start}
              type="time"
              value={startTime}
              onChange={(e) => setStartTime(e.target.value)}
              aria-invalid={errors.time ? true : undefined}
              aria-describedby={describe("time", `${ids.start}-hint`)}
              className={fieldInput}
            />
          </div>
          <div>
            <label htmlFor={ids.end} className={fieldLabel}>
              To
            </label>
            <input
              id={ids.end}
              type="time"
              value={endTime}
              onChange={(e) => setEndTime(e.target.value)}
              aria-invalid={errors.time ? true : undefined}
              aria-describedby={describe("time", `${ids.start}-hint`)}
              className={fieldInput}
            />
          </div>
        </div>
        {errors.time && (
          <p id={errorId("time")} className={fieldError}>
            {errors.time}
          </p>
        )}
      </fieldset>

      <div>
        <label htmlFor={ids.po} className={fieldLabel}>
          PO number (optional)
        </label>
        <input
          id={ids.po}
          type="text"
          maxLength={64}
          value={poNumber}
          onChange={(e) => setPoNumber(e.target.value)}
          aria-invalid={errors.po_number ? true : undefined}
          aria-describedby={describe("po_number")}
          className={fieldInput}
        />
        {errors.po_number && (
          <p id={errorId("po_number")} className={fieldError}>
            {errors.po_number}
          </p>
        )}
      </div>

      <div>
        <label htmlFor={ids.notes} className={fieldLabel}>
          Notes (optional)
        </label>
        <textarea
          id={ids.notes}
          rows={3}
          maxLength={500}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          aria-invalid={errors.notes ? true : undefined}
          aria-describedby={describe("notes", `${ids.notes}-hint`)}
          className={`${fieldInput} py-2`}
        />
        <p id={`${ids.notes}-hint`} className={fieldHint}>
          Up to 500 characters.
        </p>
        {errors.notes && (
          <p id={errorId("notes")} className={fieldError}>
            {errors.notes}
          </p>
        )}
      </div>

      <button
        type="submit"
        aria-disabled={disabled ? true : undefined}
        className={primaryButton}
      >
        {submitting ? "Sending request…" : "Send request"}
      </button>
    </form>
  );
}
