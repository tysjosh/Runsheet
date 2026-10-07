/**
 * Time helpers for the board (R2.4–R2.7, R2.9). All wall-clock maths runs in
 * the tenant time zone from the snapshot, never the browser's.
 */
import type { BoardShift } from "../../services/dispatchBoardApi";
import type { ShiftView } from "./viewState";

export const HOUR_MS = 3_600_000;
const LITERS_PER_GALLON = 3.78541;

interface ZonedParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
}

function zonedParts(ms: number, timeZone: string): ZonedParts {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(new Date(ms));
  const get = (type: string) =>
    Number(parts.find((p) => p.type === type)?.value ?? 0);
  return {
    year: get("year"),
    month: get("month"),
    day: get("day"),
    hour: get("hour") % 24,
    minute: get("minute"),
  };
}

/** Offset (ms) of `timeZone` from UTC at instant `ms`. */
function zoneOffset(ms: number, timeZone: string): number {
  const p = zonedParts(ms, timeZone);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute);
  return asUtc - Math.floor(ms / 60_000) * 60_000;
}

/** The instant of `date` (YYYY-MM-DD) at `hhmm` wall-clock time in `timeZone`. */
export function zonedInstant(
  date: string,
  hhmm: string,
  timeZone: string,
  dayOffset = 0,
): number {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = hhmm.split(":").map(Number);
  const guess = Date.UTC(y, m - 1, d + dayOffset, hh || 0, mm || 0);
  try {
    // Two passes settle DST transitions.
    let ms = guess - zoneOffset(guess, timeZone);
    ms = guess - zoneOffset(ms, timeZone);
    return ms;
  } catch {
    return guess;
  }
}

/** Visible time window for the selected shift (R2.4). */
export function shiftWindow(
  serviceDate: string,
  shift: ShiftView,
  shifts: BoardShift[],
  timeZone: string,
): { start: number; end: number } {
  const def = shift === "all" ? null : shifts.find((s) => s.id === shift);
  if (!def) {
    return {
      start: zonedInstant(serviceDate, "00:00", timeZone),
      end: zonedInstant(serviceDate, "00:00", timeZone, 1),
    };
  }
  const start = zonedInstant(serviceDate, def.start, timeZone);
  // A shift ending at or before its start runs into the next day (Night).
  const endDay = def.end <= def.start ? 1 : 0;
  return { start, end: zonedInstant(serviceDate, def.end, timeZone, endDay) };
}

/** "8:00 AM" in the tenant zone; `null` for a missing or bad time. */
export function formatTime(
  iso: string | number | null | undefined,
  timeZone: string,
): string | null {
  if (iso === null || iso === undefined || iso === "") return null;
  const ms = typeof iso === "number" ? iso : Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  try {
    return new Intl.DateTimeFormat("en-US", {
      timeZone,
      hour: "numeric",
      minute: "2-digit",
    }).format(new Date(ms));
  } catch {
    return null;
  }
}

/** "8:00 AM–12:00 PM", or "No window". */
export function formatWindow(
  start: string | null | undefined,
  end: string | null | undefined,
  timeZone: string,
): string {
  const a = formatTime(start, timeZone);
  const b = formatTime(end, timeZone);
  if (a && b) return `${a}–${b}`;
  if (a) return `From ${a}`;
  if (b) return `By ${b}`;
  return "No window";
}

/** The board's window bucket, matching the server's tray filter (K5). */
export function windowBucket(
  start: string | null | undefined,
  end: string | null | undefined,
  serviceDate: string,
  timeZone: string,
  now: number = Date.now(),
): "overdue" | "today" | "later" | null {
  const endMs = end ? Date.parse(end) : Number.NaN;
  if (!Number.isNaN(endMs) && endMs < now) return "overdue";
  const startMs = start ? Date.parse(start) : Number.NaN;
  if (Number.isNaN(startMs)) return null;
  const dayStart = zonedInstant(serviceDate, "00:00", timeZone);
  const dayEnd = zonedInstant(serviceDate, "00:00", timeZone, 1);
  if (startMs >= dayStart && startMs < dayEnd) return "today";
  if (startMs >= dayEnd) return "later";
  return null;
}

const gallonsFormat = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
});

export function formatGallons(gallons: number): string {
  return `${gallonsFormat.format(gallons)} gal`;
}

export function litersToGallons(liters: number): number {
  return liters / LITERS_PER_GALLON;
}
