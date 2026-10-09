/**
 * One place for every number, quantity, money, date and product label the
 * staff app shows (UI revamp R5.3, design.md §3.1).
 *
 * - Separators follow the browser locale (or `configureFormat({ locale })`).
 * - Dates and times render in the tenant time zone, which the dashboard shell
 *   publishes through `TenantSettingsContext` → `configureFormat`. Until it is
 *   known they use the browser's zone.
 * - Times are 24 h (dispatch convention).
 * - Missing or non-finite values render as an em dash, never "NaN".
 *
 * New code must not call `toFixed` or `toLocale*String` directly;
 * `format.guard.test.ts` ratchets the existing count down to zero.
 */
import { PRODUCT } from "../styles/tokens";

export const EMPTY = "—";

interface FormatConfig {
  /** BCP 47 locale for separators and month names; undefined = browser. */
  locale?: string;
  /** IANA time zone; undefined = browser. */
  timeZone?: string;
}

const config: FormatConfig = {};

/** Set the process-wide defaults (the shell calls this once it knows the tenant). */
export function configureFormat(next: FormatConfig): void {
  if ("locale" in next) config.locale = next.locale || undefined;
  if ("timeZone" in next) config.timeZone = next.timeZone || undefined;
}

/** Current defaults (for tests and components that build their own Intl objects). */
export function formatConfig(): Readonly<FormatConfig> {
  return { ...config };
}

type NumOpts = { decimals?: number; locale?: string };
type DateOpts = {
  timeZone?: string;
  locale?: string;
  /** 12-hour clock without a leading zero ("9:20 AM"); default 24 h. Additive (3.11). */
  hour12?: boolean;
};
type DateInput = Date | string | number | null | undefined;

const isNum = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v);

function toNumber(v: number | string | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return isNum(n) ? n : null;
}

function toDate(d: DateInput): Date | null {
  if (d === null || d === undefined || d === "") return null;
  const date = d instanceof Date ? d : new Date(d);
  return Number.isNaN(date.getTime()) ? null : date;
}

// ── numbers ──────────────────────────────────────────────────────────────

/** Grouped number with a fixed number of decimals: number(5283.44) → "5,283". */
export function number(
  v: number | string | null | undefined,
  { decimals = 0, locale = config.locale }: NumOpts = {},
): string {
  const n = toNumber(v);
  if (n === null) return EMPTY;
  return new Intl.NumberFormat(locale, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n);
}

/** Gallons, whole by default: gallons(4200) → "4,200 gal". */
export function gallons(
  v: number | string | null | undefined,
  opts: NumOpts = {},
): string {
  const s = number(v, opts);
  return s === EMPTY ? s : `${s} gal`;
}

/** Litres, whole by default: liters(15898.7) → "15,899 L". */
export function liters(
  v: number | string | null | undefined,
  opts: NumOpts = {},
): string {
  const s = number(v, opts);
  return s === EMPTY ? s : `${s} L`;
}

/** Money with 2 decimals: money(1234.5) → "$1,234.50". */
export function money(
  v: number | string | null | undefined,
  {
    currency = "USD",
    locale = config.locale,
    decimals = 2,
  }: { currency?: string; locale?: string; decimals?: number } = {},
): string {
  const n = toNumber(v);
  if (n === null) return EMPTY;
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n);
}

/**
 * Percentage of a value already expressed in percent: pct(12) → "12%".
 * (Pass `fraction: true` for 0–1 ratios: pct(0.12, {fraction: true}) → "12%".)
 */
export function pct(
  v: number | string | null | undefined,
  {
    decimals = 0,
    locale = config.locale,
    fraction = false,
  }: NumOpts & { fraction?: boolean } = {},
): string {
  const n = toNumber(v);
  if (n === null) return EMPTY;
  return new Intl.NumberFormat(locale, {
    style: "percent",
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(fraction ? n : n / 100);
}

/**
 * Parse user-typed numbers using the locale's group and decimal separators:
 * "5,283.4" (en-US) and "5.283,4" (de-DE) both → 5283.4. Returns null for an
 * empty string and NaN for text that is not a number.
 */
export function parseNumber(
  text: string,
  { locale = config.locale }: { locale?: string } = {},
): number | null {
  const raw = text.trim();
  if (raw === "") return null;
  const parts = new Intl.NumberFormat(locale).formatToParts(12345.6);
  const group = parts.find((p) => p.type === "group")?.value ?? ",";
  const decimal = parts.find((p) => p.type === "decimal")?.value ?? ".";
  // Normalise narrow/no-break spaces used as group separators (fr-FR, etc.).
  let s = raw.replace(/[\s\u00a0\u202f]/g, "");
  const groupChar = group.replace(/[\s\u00a0\u202f]/g, "");
  if (groupChar) s = s.split(groupChar).join("");
  if (decimal !== ".") s = s.replace(decimal, ".");
  if (!/^[-+]?(\d+\.?\d*|\.\d+)$/.test(s)) return Number.NaN;
  return Number(s);
}

// ── dates and times ──────────────────────────────────────────────────────

function parts(d: Date, o: Intl.DateTimeFormatOptions, opts: DateOpts) {
  const fmt = new Intl.DateTimeFormat(opts.locale ?? config.locale, {
    timeZone: opts.timeZone ?? config.timeZone,
    ...o,
  });
  const out: Record<string, string> = {};
  for (const p of fmt.formatToParts(d)) out[p.type] = p.value;
  return out;
}

/** "Wed 8 Oct" (weekday, day, month: the dispatch order in every locale). */
export function date(d: DateInput, opts: DateOpts = {}): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  const p = parts(
    v,
    { weekday: "short", day: "numeric", month: "short" },
    opts,
  );
  return `${p.weekday} ${p.day} ${p.month}`;
}

/** "Wed 8 Oct 2026". */
export function dateLong(d: DateInput, opts: DateOpts = {}): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  const p = parts(
    v,
    { weekday: "short", day: "numeric", month: "short", year: "numeric" },
    opts,
  );
  return `${p.weekday} ${p.day} ${p.month} ${p.year}`;
}

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * A calendar date such as an expiry ("2027-01-31" → "Sun 31 Jan 2027").
 * Date-only strings are calendar days, not instants, so they are formatted
 * in UTC: `dateLong` would shift them back a day west of UTC. Timestamps
 * fall through to `dateLong` in the tenant zone.
 */
export function calendarDate(d: DateInput, opts: DateOpts = {}): string {
  if (typeof d === "string" && DATE_ONLY.test(d))
    return dateLong(`${d}T00:00:00Z`, { ...opts, timeZone: "UTC" });
  return dateLong(d, opts);
}

/** "08:30" (24 h), or "9:20 AM" with `hour12: true`. */
export function time(d: DateInput, opts: DateOpts = {}): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  if (opts.hour12) {
    const q = parts(
      v,
      { hour: "numeric", minute: "2-digit", hour12: true },
      { ...opts, locale: "en-US" },
    );
    return `${q.hour}:${q.minute} ${q.dayPeriod}`;
  }
  const p = parts(
    v,
    { hour: "2-digit", minute: "2-digit", hourCycle: "h23" },
    opts,
  );
  return `${p.hour}:${p.minute}`;
}

/** "Wed 8 Oct, 08:30". */
export function dateTime(d: DateInput, opts: DateOpts = {}): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  return `${date(v, opts)}, ${time(v, opts)}`;
}

/** The zone's short name for an instant ("CDT"); "" when unknown. Additive (3.11). */
export function zoneName(d: DateInput, opts: DateOpts = {}): string {
  const v = toDate(d);
  if (!v) return "";
  return parts(v, { timeZoneName: "short" }, opts).timeZoneName ?? "";
}

function dayKey(d: Date, opts: DateOpts): string {
  const p = parts(
    d,
    { year: "numeric", month: "2-digit", day: "2-digit" },
    { ...opts, locale: "en-CA" },
  );
  return `${p.year}-${p.month}-${p.day}`;
}

/**
 * A time window: "08:30–10:30" on one day; across days
 * "Wed 8 Oct, 22:00 – Thu 9 Oct, 02:00". One open end renders "from 08:30" /
 * "until 10:30".
 */
export function window(
  a: DateInput,
  b: DateInput,
  opts: DateOpts = {},
): string {
  const start = toDate(a);
  const end = toDate(b);
  if (!start && !end) return EMPTY;
  if (start && !end) return `from ${time(start, opts)}`;
  if (!start && end) return `until ${time(end, opts)}`;
  const s = start as Date;
  const e = end as Date;
  if (dayKey(s, opts) === dayKey(e, opts)) {
    return `${time(s, opts)}–${time(e, opts)}`;
  }
  return `${dateTime(s, opts)} – ${dateTime(e, opts)}`;
}

/**
 * "just now", "12 min ago", "3 h ago", "in 20 min"; beyond a day it falls back
 * to `date()`. `now` is injectable for tests.
 */
export function relative(
  d: DateInput,
  { now = Date.now(), ...opts }: DateOpts & { now?: number } = {},
): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  const diffMs = v.getTime() - now;
  const abs = Math.abs(diffMs);
  const future = diffMs > 0;
  const say = (n: number, unit: string) =>
    future ? `in ${n} ${unit}` : `${n} ${unit} ago`;
  if (abs < 45_000) return "just now";
  if (abs < 3_600_000) return say(Math.max(1, Math.round(abs / 60_000)), "min");
  if (abs < 86_400_000) return say(Math.round(abs / 3_600_000), "h");
  return date(v, opts);
}

/**
 * An elapsed span in seconds as a short human duration, two units at most:
 * "45 s", "12 min", "3 h 20 min", "1 d", "2 d 4 h". Negative or missing
 * values render the empty dash.
 */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return EMPTY;
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const mins = Math.round(s / 60);
  if (mins < 60) return `${mins} min`;
  const totalMin = Math.round(s / 60);
  const h = Math.floor(totalMin / 60);
  if (h < 24) {
    const m = totalMin % 60;
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  const totalH = Math.round(s / 3600);
  const d = Math.floor(totalH / 24);
  const rh = totalH % 24;
  return rh ? `${number(d)} d ${rh} h` : `${number(d)} d`;
}

/** "1 day" / "4 days". Additive (3.11). */
export function days(n: number): string {
  return `${number(n)} ${n === 1 ? "day" : "days"}`;
}

// ── products ─────────────────────────────────────────────────────────────

/** "JET_A" → "Jet a"; "kerosene_k1" → "Kerosene k1". */
export function humanize(code: string): string {
  const s = code.replace(/[_-]+/g, " ").trim().toLowerCase();
  return s ? s[0].toUpperCase() + s.slice(1) : code;
}

/** Readable product name for a catalog code; unknown codes are humanised. */
export function productName(code: string | null | undefined): string {
  if (!code) return EMPTY;
  const token = PRODUCT[code as keyof typeof PRODUCT];
  return token?.name ?? humanize(code);
}

/** Namespace-style import: `import { format } from "@/lib/format"`. */
export const format = {
  number,
  gallons,
  liters,
  money,
  pct,
  parseNumber,
  date,
  dateLong,
  calendarDate,
  time,
  dateTime,
  window,
  relative,
  duration,
  days,
  zoneName,
  productName,
  humanize,
};
