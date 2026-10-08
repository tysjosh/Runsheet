/**
 * Portal display formatting (PD23): times in the browser's zone with the zone
 * abbreviation, delivery dates as calendar dates, money in USD from integer
 * cents, volumes in the tenant's unit.
 */

const MONEY = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

const NUMBER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

/** "$1,240.50" from 124050. */
export function formatMoney(cents: number | null | undefined): string {
  if (typeof cents !== "number" || !Number.isFinite(cents)) return "—";
  return MONEY.format(cents / 100);
}

/** "1,240" (one decimal at most). */
export function formatNumber(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return NUMBER.format(value);
}

/** "1,240 gal" in the tenant's volume unit. */
export function formatVolume(
  value: number | null | undefined,
  unit = "gal",
): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${NUMBER.format(value)} ${unit}`;
}

/** Local date and time with the zone abbreviation, e.g. "Oct 9, 2026, 2:15 PM EDT". */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  // `dateStyle`/`timeStyle` can't be combined with `timeZoneName`, so the
  // zone abbreviation is appended from a second formatter.
  const text = new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
  return `${text} ${zoneAbbreviation(date)}`.trim();
}

function zoneAbbreviation(date: Date): string {
  const part = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
    .formatToParts(date)
    .find((p) => p.type === "timeZoneName");
  return part?.value ?? "";
}

/**
 * A calendar date ("Oct 9, 2026"). A bare `YYYY-MM-DD` is shown as that day,
 * never shifted by the browser's zone; a timestamp is shown in local time.
 */
export function formatCalendarDate(value: string | null | undefined): string {
  if (!value) return "—";
  const bare = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const date = bare
    ? new Date(Number(bare[1]), Number(bare[2]) - 1, Number(bare[3]))
    : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-US", { dateStyle: "medium" }).format(date);
}

/** A delivery window: "Oct 9, 2026, 8:00 AM – 12:00 PM EDT" or a single day. */
export function formatWindow(
  start: string | null | undefined,
  end: string | null | undefined,
): string {
  if (!start) return "Not scheduled";
  const s = new Date(start);
  const e = end ? new Date(end) : null;
  if (Number.isNaN(s.getTime())) return "Not scheduled";
  if (!e || Number.isNaN(e.getTime())) return formatDateTime(start);
  const isWholeDay =
    s.getHours() === 0 &&
    s.getMinutes() === 0 &&
    e.getTime() - s.getTime() >= 23 * 3600_000 &&
    e.getHours() === 0 &&
    e.getMinutes() === 0;
  if (isWholeDay) {
    return new Intl.DateTimeFormat("en-US", { dateStyle: "medium" }).format(s);
  }
  const time = new Intl.DateTimeFormat("en-US", { timeStyle: "short" });
  const day = new Intl.DateTimeFormat("en-US", { dateStyle: "medium" });
  return `${day.format(s)}, ${time.format(s)} – ${time.format(e)} ${zoneAbbreviation(e)}`;
}
