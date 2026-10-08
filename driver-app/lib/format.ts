/**
 * Dates, times and product labels, with the same rules as the staff app's
 * `runsheet/src/lib/format.ts` (UI revamp R13.9, design.md §3.1). It is a port,
 * not a shared module (D15: no shared component package), so keep the two in
 * step; `__tests__/format.test.ts` pins the outputs both apps must produce.
 *
 * - Times are 24 h and never show seconds ("08:30").
 * - Dates read weekday, day, month ("Wed 8 Oct").
 * - Missing or unparseable values render as an em dash, never "Invalid Date".
 * - The time zone is the device's (the driver's local day) unless
 *   `configureFormat({ timeZone })` sets one.
 *
 * Gallons, miles and money keep using `lib/units.ts`, which owns the unit
 * labels (R16.18, R16.19).
 */

import { PRODUCT } from './tokens';

export const EMPTY = '—';

interface FormatConfig {
  /** BCP 47 locale for month names; undefined = device. */
  locale?: string;
  /** IANA time zone; undefined = device. */
  timeZone?: string;
}

const config: FormatConfig = {};

export function configureFormat(next: FormatConfig): void {
  if ('locale' in next) config.locale = next.locale || undefined;
  if ('timeZone' in next) config.timeZone = next.timeZone || undefined;
}

type DateOpts = { timeZone?: string; locale?: string };
type DateInput = Date | string | number | null | undefined;

function toDate(d: DateInput): Date | null {
  if (d === null || d === undefined || d === '') return null;
  const value = d instanceof Date ? d : new Date(d);
  return Number.isNaN(value.getTime()) ? null : value;
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * Date parts via `Intl`. If the engine can't honour an option (older Hermes
 * builds without full time zone data), fall back to the device clock in
 * English so the screen still renders without seconds.
 */
function parts(d: Date, o: Intl.DateTimeFormatOptions, opts: DateOpts): Record<string, string> {
  try {
    const fmt = new Intl.DateTimeFormat(opts.locale ?? config.locale, {
      timeZone: opts.timeZone ?? config.timeZone,
      ...o,
    });
    const out: Record<string, string> = {};
    for (const p of fmt.formatToParts(d)) out[p.type] = p.value;
    return out;
  } catch {
    const pad = (n: number) => String(n).padStart(2, '0');
    return {
      weekday: WEEKDAYS[d.getDay()],
      day: String(d.getDate()),
      month: o.month === '2-digit' ? pad(d.getMonth() + 1) : MONTHS[d.getMonth()],
      year: String(d.getFullYear()),
      hour: pad(d.getHours()),
      minute: pad(d.getMinutes()),
    };
  }
}

/** "Wed 8 Oct". */
export function date(d: DateInput, opts: DateOpts = {}): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  const p = parts(v, { weekday: 'short', day: 'numeric', month: 'short' }, opts);
  return `${p.weekday} ${p.day} ${p.month}`;
}

/** "Wed 8 Oct 2026". */
export function dateLong(d: DateInput, opts: DateOpts = {}): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  const p = parts(v, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }, opts);
  return `${p.weekday} ${p.day} ${p.month} ${p.year}`;
}

/** "08:30" (24 h, no seconds). */
export function time(d: DateInput, opts: DateOpts = {}): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  const p = parts(v, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }, opts);
  return `${p.hour}:${p.minute}`;
}

/** "Wed 8 Oct, 08:30". */
export function dateTime(d: DateInput, opts: DateOpts = {}): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  return `${date(v, opts)}, ${time(v, opts)}`;
}

function dayKey(d: Date, opts: DateOpts): string {
  const p = parts(d, { year: 'numeric', month: '2-digit', day: '2-digit' }, { ...opts, locale: 'en-CA' });
  return `${p.year}-${p.month}-${p.day}`;
}

/**
 * A delivery window: "08:30–10:30" on one day; across days
 * "Wed 8 Oct, 22:00 – Thu 9 Oct, 02:00"; one open end "from 08:30" / "until 10:30".
 */
export function timeWindow(a: DateInput, b: DateInput, opts: DateOpts = {}): string {
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

/** "just now", "12 min ago", "3 h ago", "in 20 min"; beyond a day, `date()`. */
export function relative(
  d: DateInput,
  { now = Date.now(), ...opts }: DateOpts & { now?: number } = {},
): string {
  const v = toDate(d);
  if (!v) return EMPTY;
  const diffMs = v.getTime() - now;
  const abs = Math.abs(diffMs);
  const future = diffMs > 0;
  const say = (n: number, unit: string) => (future ? `in ${n} ${unit}` : `${n} ${unit} ago`);
  if (abs < 45_000) return 'just now';
  if (abs < 3_600_000) return say(Math.max(1, Math.round(abs / 60_000)), 'min');
  if (abs < 86_400_000) return say(Math.round(abs / 3_600_000), 'h');
  return date(v, opts);
}

/** "JET_A" → "Jet a". */
export function humanize(code: string): string {
  const s = code.replace(/[_-]+/g, ' ').trim().toLowerCase();
  return s ? s[0].toUpperCase() + s.slice(1) : code;
}

/** Readable product name for a catalog code; unknown codes are humanised. */
export function productName(code: string | null | undefined): string {
  if (!code) return EMPTY;
  const token = PRODUCT[code as keyof typeof PRODUCT];
  return token?.name ?? humanize(code);
}
