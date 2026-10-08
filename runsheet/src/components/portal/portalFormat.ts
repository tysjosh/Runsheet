/**
 * Portal formatting (R14.4, design §11.7, PD23): a thin, portal-local layer
 * over `lib/format`.
 *
 * Numbers, gallons, money and product names come straight from `lib/format`.
 * The portal adds what the shared module doesn't have yet (follow-up: move
 * `hour12`, `zoneName` and `days()` into `lib/format`, design §11.3):
 *
 * - times in the browser's zone, 12-hour, no leading zero ("9:20 AM");
 * - the zone abbreviation once, at the end of a window ("… – 3:20 PM CDT");
 * - dates as "Thu 9 Oct", with the year only when it isn't the current one;
 * - a bare `YYYY-MM-DD` (due dates) shown as that calendar day, never shifted;
 * - "1 day" / "4 days".
 *
 * Nothing here calls `toFixed` or `toLocale*String` (guard test).
 */
import {
  EMPTY,
  gallons as formatGallons,
  money as formatMoneyValue,
  number as formatNumberValue,
  productName,
} from "../../lib/format";

export { EMPTY, productName };

type DateInput = Date | string | number | null | undefined;

const BARE_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** A Date, or null. A bare `YYYY-MM-DD` becomes UTC midnight of that day. */
function toDate(d: DateInput): { date: Date; bare: boolean } | null {
  if (d === null || d === undefined || d === "") return null;
  if (typeof d === "string") {
    const m = BARE_DATE.exec(d);
    if (m) {
      return {
        date: new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))),
        bare: true,
      };
    }
  }
  const date = d instanceof Date ? d : new Date(d);
  return Number.isNaN(date.getTime()) ? null : { date, bare: false };
}

function parts(
  date: Date,
  options: Intl.DateTimeFormatOptions,
  bare = false,
): Record<string, string> {
  const fmt = new Intl.DateTimeFormat("en-US", {
    ...options,
    ...(bare ? { timeZone: "UTC" } : {}),
  });
  const out: Record<string, string> = {};
  for (const p of fmt.formatToParts(date)) out[p.type] = p.value;
  return out;
}

function yearOf(date: Date, bare: boolean): string {
  return parts(date, { year: "numeric" }, bare).year;
}

/** "Thu 9 Oct", or "Thu 9 Oct 2025" outside the current year. */
export function date(d: DateInput, now: Date = new Date()): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  const p = parts(
    v.date,
    { weekday: "short", day: "numeric", month: "short", year: "numeric" },
    v.bare,
  );
  const base = `${p.weekday} ${p.day} ${p.month}`;
  return p.year === yearOf(now, false) ? base : `${base} ${p.year}`;
}

/** "9:20 AM" in the browser's zone. */
export function time(d: DateInput): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  const p = parts(v.date, {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  });
  return `${p.hour}:${p.minute} ${p.dayPeriod}`;
}

/** The browser zone's short name for an instant ("CDT"). */
export function zoneName(d: DateInput): string {
  const v = toDate(d);
  if (!v) return "";
  return parts(v.date, { timeZoneName: "short" }).timeZoneName ?? "";
}

/** "Thu 9 Oct, 9:20 AM". */
export function dateTime(d: DateInput, now: Date = new Date()): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  return `${date(v.date, now)}, ${time(v.date)}`;
}

function dayKey(d: Date): string {
  const p = parts(d, { year: "numeric", month: "2-digit", day: "2-digit" });
  return `${p.year}-${p.month}-${p.day}`;
}

function isLocalMidnight(d: Date): boolean {
  const p = parts(d, { hour: "numeric", minute: "2-digit", hour12: false });
  return (
    (p.hour === "0" || p.hour === "00" || p.hour === "24") && p.minute === "00"
  );
}

/**
 * A delivery window, with the zone named once at the end:
 * "Thu 9 Oct, 9:20 AM – 3:20 PM CDT". A whole local day (midnight to
 * midnight, the "any time that day" request) is just "Thu 9 Oct"; windows
 * that cross days show both dates; no start reads "Not scheduled".
 */
export function window(
  a: DateInput,
  b: DateInput,
  now: Date = new Date(),
): string {
  const start = toDate(a)?.date;
  const end = toDate(b)?.date;
  if (!start) return "Not scheduled";
  if (!end) return `${dateTime(start, now)} ${zoneName(start)}`.trim();
  const hours = (end.getTime() - start.getTime()) / 3_600_000;
  if (
    isLocalMidnight(start) &&
    isLocalMidnight(end) &&
    hours >= 23 &&
    hours <= 25
  ) {
    return date(start, now);
  }
  const zone = zoneName(end);
  if (dayKey(start) === dayKey(end)) {
    return `${date(start, now)}, ${time(start)} – ${time(end)} ${zone}`.trim();
  }
  return `${dateTime(start, now)} – ${dateTime(end, now)} ${zone}`.trim();
}

/** "$1,240.50" from integer cents. */
export function money(cents: number | null | undefined): string {
  if (typeof cents !== "number" || !Number.isFinite(cents)) return EMPTY;
  return formatMoneyValue(cents / 100, { locale: "en-US" });
}

/**
 * A unit price at its stored precision (PE3), from the API's decimal string:
 * "2.9193" → "$2.9193", "2.91" → "$2.91". The decimals shown are the ones the
 * server sent (at least 2), so "1,187.4 gal × $2.9193" matches the subtotal.
 */
export function unitPrice(dollars: string | null | undefined): string {
  if (!dollars || !/^-?\d+(\.\d+)?$/.test(dollars)) return EMPTY;
  const decimals = Math.max(2, (dollars.split(".")[1] ?? "").length);
  return formatMoneyValue(Number(dollars), { locale: "en-US", decimals });
}

/** "1,240" (whole) or "1,187.4" with `decimals: 1`. */
export function number(v: number | null | undefined, decimals = 0): string {
  return formatNumberValue(v, { decimals, locale: "en-US" });
}

/** "1,240 gal" in the tenant's unit (whole by default). */
export function volume(
  v: number | null | undefined,
  unit = "gal",
  decimals = 0,
): string {
  if (unit === "gal") return formatGallons(v, { decimals, locale: "en-US" });
  const s = number(v, decimals);
  return s === EMPTY ? s : `${s} ${unit}`;
}

/** A delivered quantity: one decimal only when it isn't whole. */
export function deliveredVolume(
  v: number | null | undefined,
  unit = "gal",
): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return EMPTY;
  return volume(v, unit, Number.isInteger(v) ? 0 : 1);
}

/** "1 day" / "4 days" (PF11). */
export function days(n: number): string {
  return `${n} ${n === 1 ? "day" : "days"}`;
}

/** Whole percent as design §11.5 writes it: "32 %". */
export function percent(v: number | null | undefined): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return EMPTY;
  return `${number(Math.round(v))} %`;
}

export const portalFormat = {
  date,
  time,
  dateTime,
  window,
  zoneName,
  money,
  unitPrice,
  number,
  volume,
  deliveredVolume,
  days,
  percent,
  productName,
};
