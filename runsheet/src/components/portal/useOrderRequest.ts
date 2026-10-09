"use client";

/**
 * Request-a-delivery logic (R4.6, R4.7, PD9, PD10), moved out of the old
 * `OrderRequestForm` unchanged in behaviour (R14.11):
 *
 * - the `client_event_id` is created when the dialog opens and kept across
 *   manual retries, so a retry is idempotent; a 409 IDEMPOTENCY_CONFLICT asks
 *   for a fresh one;
 * - a 409 ORDER_INTAKE_DISABLED is an expected state, not an error: every
 *   value is kept, submit turns `aria-disabled`, and nothing is retried;
 * - a 429 shows "Too many requests. Try again in N seconds." and the form
 *   stays usable;
 * - a 404 on the tank says the tank isn't available any more and asks the
 *   page to reload its tank list;
 * - the date is bounded to today … today + 60 days and the times are
 *   wall-clock times in the tenant's zone, the zone every portal time is
 *   shown in (portal-fixes A5); a date alone is midnight to the next
 *   midnight there. The server stays authoritative;
 * - gallons: at least 25 (or the whole tank when it's smaller), at most the
 *   capacity, and at most the room left when the latest reading is fresh
 *   (portal-fixes B2, the same rule the server applies).
 */
import { useCallback, useMemo, useRef, useState } from "react";
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
import { newUuid } from "./ids";
import { number, zonedInstant, zonedIsoDate } from "./portalFormat";

/** PD9 horizon: today to today + 60 days. */
export const MAX_DAYS_AHEAD = 60;
export const PO_MAX = 64;
export const NOTES_MAX = 500;
/** Smallest gallons request (server: MIN_REQUEST_GALLONS). */
export const MIN_GALLONS = 25;

/** The most a request may ask for: the room left at a fresh reading, else the capacity. */
export function maxGallons(tank: PortalTank): number {
  if (!tank.reading_stale) {
    return Math.max(
      0,
      Math.floor(tank.capacity_gallons - tank.current_level_gallons),
    );
  }
  return tank.capacity_gallons;
}

/** The fewest a request may ask for: 25, or the whole tank when smaller. */
export function minGallons(tank: PortalTank | null): number {
  return tank ? Math.min(MIN_GALLONS, tank.capacity_gallons) : MIN_GALLONS;
}

export type RequestField =
  | "form"
  | "tank"
  | "gallons"
  | "date"
  | "time"
  | "po_number"
  | "notes";

export type RequestErrors = Partial<Record<RequestField, string>>;

export const FIELD_ORDER: RequestField[] = [
  "form",
  "tank",
  "gallons",
  "date",
  "time",
  "po_number",
  "notes",
];

export interface RequestValues {
  tankId: string;
  mode: "fill_to_full" | "gallons";
  gallons: number | null;
  date: string;
  startTime: string;
  endTime: string;
  poNumber: string;
  notes: string;
}

export function emptyValues(tankId = ""): RequestValues {
  return {
    tankId,
    mode: "fill_to_full",
    gallons: null,
    date: "",
    startTime: "",
    endTime: "",
    poNumber: "",
    notes: "",
  };
}

function parseTime(value: string): [number, number] | null {
  const m = /^(\d{2}):(\d{2})$/.exec(value);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

/** The request window as ISO-8601 instants (wall clock in the portal zone). */
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
      ? zonedInstant(y, m, d, start[0], start[1])
      : zonedInstant(y, m, d);
  const to =
    start && end
      ? zonedInstant(y, m, d, end[0], end[1])
      : zonedInstant(y, m, d + 1);
  return { window_start: from.toISOString(), window_end: to.toISOString() };
}

export function validateRequest(
  v: RequestValues,
  tank: PortalTank | null,
  minDate: string,
  maxDate: string,
): RequestErrors {
  const next: RequestErrors = {};
  if (!tank) next.tank = "Choose a tank.";
  if (v.mode === "gallons") {
    const g = v.gallons;
    if (g === null || Number.isNaN(g) || g <= 0) {
      next.gallons = "Enter a number of gallons greater than 0.";
    } else if (g < minGallons(tank)) {
      next.gallons = `Request at least ${number(minGallons(tank))} gallons, or choose Fill to full.`;
    } else if (tank && g > tank.capacity_gallons) {
      next.gallons = `Enter no more than ${number(tank.capacity_gallons)} gallons, the tank's capacity.`;
    } else if (tank && g > maxGallons(tank)) {
      next.gallons = `The tank has room for about ${number(maxGallons(tank))} gallons at its latest reading. Request that much or less, or choose Fill to full.`;
    }
  }
  if (!v.date) {
    next.date = "Choose a delivery date.";
  } else if (v.date < minDate || v.date > maxDate) {
    next.date = `Choose a date from today to ${MAX_DAYS_AHEAD} days ahead.`;
  }
  if (Boolean(v.startTime) !== Boolean(v.endTime)) {
    next.time = "Enter both a start and an end time, or leave both empty.";
  } else if (v.startTime && v.endTime && v.endTime <= v.startTime) {
    next.time = "The end time must be after the start time.";
  }
  if (v.poNumber.trim().length > PO_MAX) {
    next.po_number = `Use ${PO_MAX} characters or fewer for the PO number.`;
  }
  if (v.notes.trim().length > NOTES_MAX) {
    next.notes = `Use ${NOTES_MAX} characters or fewer for the notes.`;
  }
  return next;
}

export const TANK_GONE_MESSAGE = "That tank isn't available any more.";

export function useOrderRequest({
  tanks,
  orderingAvailable,
  onTankGone,
}: {
  tanks: PortalTank[];
  orderingAvailable: boolean;
  /** 404 on the tank: the page reloads its tank list. */
  onTankGone?: () => void;
}) {
  const [values, setValues] = useState<RequestValues>(emptyValues());
  const [initial, setInitial] = useState<RequestValues>(emptyValues());
  const [clientEventId, setClientEventId] = useState(newUuid);
  const [errors, setErrors] = useState<RequestErrors>({});
  const [intakeDisabled, setIntakeDisabled] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [created, setCreated] = useState<PortalOrder | null>(null);
  const inFlight = useRef(false);

  const minDate = useMemo(() => zonedIsoDate(0), []);
  const maxDate = useMemo(() => zonedIsoDate(MAX_DAYS_AHEAD), []);
  const tank = tanks.find((t) => t.customer_tank_id === values.tankId) ?? null;
  const unavailable = !orderingAvailable || intakeDisabled;
  const disabled = unavailable || submitting;

  /** A fresh request (the dialog opened): new client_event_id, new values. */
  const reset = useCallback((tankId: string) => {
    const start = emptyValues(tankId);
    setValues(start);
    setInitial(start);
    setClientEventId(newUuid());
    setErrors({});
    setNotice(null);
    setCreated(null);
    setSubmitting(false);
    inFlight.current = false;
  }, []);

  const set = useCallback(
    <K extends keyof RequestValues>(field: K, value: RequestValues[K]) => {
      setValues((prev) => ({ ...prev, [field]: value }));
    },
    [],
  );

  const dirty =
    !created &&
    (Object.keys(values) as (keyof RequestValues)[]).some(
      (k) => !Object.is(values[k], initial[k]),
    );

  /** Validates and sends. Returns the errors to show (empty when sent). */
  const submit = useCallback(async (): Promise<RequestErrors> => {
    if (disabled || inFlight.current) return {};
    setNotice(null);
    const next = validateRequest(values, tank, minDate, maxDate);
    if (Object.keys(next).length > 0) {
      setErrors(next);
      return next;
    }
    setErrors({});
    if (!tank) return {};
    const body: PortalOrderRequest = {
      client_event_id: clientEventId,
      customer_tank_id: tank.customer_tank_id,
      quantity:
        values.mode === "gallons" && values.gallons !== null
          ? { mode: "gallons", gallons: values.gallons }
          : { mode: "fill_to_full" },
      ...buildWindow(values.date, values.startTime, values.endTime),
      ...(values.poNumber.trim() ? { po_number: values.poNumber.trim() } : {}),
      ...(values.notes.trim() ? { notes: values.notes.trim() } : {}),
    };
    inFlight.current = true;
    setSubmitting(true);
    let shown: RequestErrors = {};
    try {
      const response = await createPortalOrder(body);
      setCreated(response.data);
    } catch (error) {
      if (hasErrorCode(error, "ORDER_INTAKE_DISABLED")) {
        // PD10: an expected state, not an error. Keep every value; no retry.
        setIntakeDisabled(true);
      } else if (isRateLimited(error)) {
        setNotice(rateLimitMessage(error));
      } else if (hasErrorCode(error, "IDEMPOTENCY_CONFLICT")) {
        // The server asks for a new reference; the next submit uses one.
        setClientEventId(newUuid());
        shown = {
          form:
            error instanceof ApiError
              ? error.message
              : "Please submit the request again.",
        };
      } else if (error instanceof ApiError && error.status === 404) {
        shown = { tank: TANK_GONE_MESSAGE };
        onTankGone?.();
      } else if (error instanceof ApiError && error.status === 422) {
        shown = { form: error.message };
      } else {
        shown = { form: "We couldn't send your request. Please try again." };
      }
      setErrors(shown);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
    return shown;
  }, [disabled, values, tank, minDate, maxDate, clientEventId, onTankGone]);

  return {
    values,
    set,
    errors,
    tank,
    minDate,
    maxDate,
    unavailable,
    disabled,
    submitting,
    notice,
    created,
    dirty,
    reset,
    submit,
  };
}
