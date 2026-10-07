/**
 * Board view state (design K14.1, R2.2, R4.4).
 *
 * The URL is the source of truth so a view can be shared; local storage holds
 * the user's last shift, zoom, density, search and filters and fills in any
 * the URL leaves out. The service date is never persisted: without `?date=`
 * the board opens today in the tenant time zone.
 */

export type Zoom = "timeline" | "sequence";
export type Density = "comfortable" | "compact";
export type ShiftView = "all" | "day" | "night";

export interface BoardFilters {
  call_type: string[];
  product: string[];
  priority: string[];
  window: string[];
  status: string[];
  has_warnings: boolean;
}

export interface BoardView {
  /** `YYYY-MM-DD`, or null for "today in the tenant zone". */
  date: string | null;
  shift: ShiftView;
  zoom: Zoom;
  density: Density;
  search: string;
  filters: BoardFilters;
  /** Deep link: lane to scroll to and order to select (R1.6). */
  truck: string | null;
  order: string | null;
}

export const VIEW_STORAGE_KEY = "runsheet.dispatchBoard.view.v1";
export const DAYS_BACK = 7;
export const DAYS_AHEAD = 14;

export const EMPTY_FILTERS: BoardFilters = {
  call_type: [],
  product: [],
  priority: [],
  window: [],
  status: [],
  has_warnings: false,
};

export const DEFAULT_VIEW: BoardView = {
  date: null,
  shift: "all",
  zoom: "timeline",
  density: "comfortable",
  search: "",
  filters: EMPTY_FILTERS,
  truck: null,
  order: null,
};

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/;
const LIST_KEYS = [
  "call_type",
  "product",
  "priority",
  "window",
  "status",
] as const;

function oneOf<T extends string>(
  value: unknown,
  allowed: readonly T[],
): T | undefined {
  return typeof value === "string" &&
    (allowed as readonly string[]).includes(value)
    ? (value as T)
    : undefined;
}

function list(value: unknown): string[] {
  const raw = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(",")
      : [];
  return raw
    .filter((v): v is string => typeof v === "string")
    .map((v) => v.trim())
    .filter((v) => v.length > 0 && v.length <= 64);
}

function isRealDate(value: string): boolean {
  if (!DATE_RE.test(value)) return false;
  const d = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === value;
}

type Persisted = Partial<Omit<BoardView, "date" | "truck" | "order">>;

/** Reads the persisted part of the view; bad or missing data gives `{}`. */
export function readStoredView(storage: Storage | null | undefined): Persisted {
  if (!storage) return {};
  try {
    const parsed = JSON.parse(storage.getItem(VIEW_STORAGE_KEY) ?? "null");
    if (!parsed || typeof parsed !== "object") return {};
    const filters = parsed.filters ?? {};
    return {
      shift: oneOf(parsed.shift, ["all", "day", "night"] as const),
      zoom: oneOf(parsed.zoom, ["timeline", "sequence"] as const),
      density: oneOf(parsed.density, ["comfortable", "compact"] as const),
      search:
        typeof parsed.search === "string"
          ? parsed.search.slice(0, 100)
          : undefined,
      filters: {
        call_type: list(filters.call_type),
        product: list(filters.product),
        priority: list(filters.priority),
        window: list(filters.window),
        status: list(filters.status),
        has_warnings: filters.has_warnings === true,
      },
    };
  } catch {
    return {};
  }
}

export function writeStoredView(
  storage: Storage | null | undefined,
  view: BoardView,
): void {
  if (!storage) return;
  const { shift, zoom, density, search, filters } = view;
  try {
    storage.setItem(
      VIEW_STORAGE_KEY,
      JSON.stringify({ shift, zoom, density, search, filters }),
    );
  } catch {
    // Storage full or blocked: the URL still carries the view.
  }
}

/** URL params win; local storage fills the gaps; defaults fill the rest. */
export function parseView(
  params: URLSearchParams,
  stored: Persisted = {},
): BoardView {
  const date = params.get("date");
  const truck = params.get("truck");
  const order = params.get("order");
  const hasAnyFilter =
    LIST_KEYS.some((k) => params.has(k)) || params.has("warnings");
  const filters: BoardFilters = hasAnyFilter
    ? {
        call_type: list(params.get("call_type")),
        product: list(params.get("product")),
        priority: list(params.get("priority")),
        window: list(params.get("window")),
        status: list(params.get("status")),
        has_warnings: params.get("warnings") === "1",
      }
    : (stored.filters ?? EMPTY_FILTERS);
  return {
    date: date && isRealDate(date) ? date : null,
    shift:
      oneOf(params.get("shift"), ["all", "day", "night"] as const) ??
      stored.shift ??
      DEFAULT_VIEW.shift,
    zoom:
      oneOf(params.get("zoom"), ["timeline", "sequence"] as const) ??
      stored.zoom ??
      DEFAULT_VIEW.zoom,
    density:
      oneOf(params.get("density"), ["comfortable", "compact"] as const) ??
      stored.density ??
      DEFAULT_VIEW.density,
    search: params.has("q")
      ? (params.get("q") ?? "").slice(0, 100)
      : (stored.search ?? ""),
    filters,
    truck: truck && ID_RE.test(truck) ? truck : null,
    order: order && ID_RE.test(order) ? order : null,
  };
}

/** Writes the view into `base` (other params such as `tab` are kept). */
export function viewToParams(
  view: BoardView,
  base: URLSearchParams,
): URLSearchParams {
  const p = new URLSearchParams(base.toString());
  const set = (key: string, value: string | null, fallback?: string) => {
    if (value === null || value === "" || value === fallback) p.delete(key);
    else p.set(key, value);
  };
  set("date", view.date);
  set("shift", view.shift, DEFAULT_VIEW.shift);
  set("zoom", view.zoom, DEFAULT_VIEW.zoom);
  set("density", view.density, DEFAULT_VIEW.density);
  set("q", view.search);
  for (const key of LIST_KEYS) set(key, view.filters[key].join(","));
  set("warnings", view.filters.has_warnings ? "1" : null);
  set("truck", view.truck);
  set("order", view.order);
  return p;
}

/** Last tenant zone seen in a snapshot, so "today" is right before the first load. */
export const ZONE_STORAGE_KEY = "runsheet.dispatchBoard.timezone.v1";

export function readStoredZone(
  storage: Storage | null | undefined,
): string | null {
  try {
    const zone = storage?.getItem(ZONE_STORAGE_KEY) ?? null;
    if (!zone || zone.length > 64) return null;
    // Rejects anything that isn't a valid IANA zone.
    new Intl.DateTimeFormat("en-CA", { timeZone: zone });
    return zone;
  } catch {
    return null;
  }
}

export function writeStoredZone(
  storage: Storage | null | undefined,
  zone: string,
): void {
  try {
    storage?.setItem(ZONE_STORAGE_KEY, zone);
  } catch {
    // Storage full or blocked: the browser zone is used until the snapshot loads.
  }
}

/** Today's calendar date in an IANA zone (falls back to the browser zone). */
export function todayIn(
  timeZone: string | null | undefined,
  now: Date = new Date(),
): string {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: timeZone || undefined,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(now);
  } catch {
    return new Intl.DateTimeFormat("en-CA", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(now);
  }
}

export function addDays(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** Clamps a date into today − 7 … today + 14 (R2.2). */
export function clampDate(date: string, today: string): string {
  const min = addDays(today, -DAYS_BACK);
  const max = addDays(today, DAYS_AHEAD);
  if (date < min) return min;
  if (date > max) return max;
  return date;
}

/** Zone abbreviation for the header ("CDT"), R2.7. */
export function zoneAbbreviation(
  timeZone: string,
  now: Date = new Date(),
): string {
  try {
    const part = new Intl.DateTimeFormat("en-US", {
      timeZone,
      timeZoneName: "short",
    })
      .formatToParts(now)
      .find((p) => p.type === "timeZoneName");
    return part?.value ?? timeZone;
  } catch {
    return timeZone;
  }
}

export function activeFilterCount(filters: BoardFilters): number {
  return (
    LIST_KEYS.reduce((n, k) => n + filters[k].length, 0) +
    (filters.has_warnings ? 1 : 0)
  );
}

/**
 * The three server tray filters (K5) for a request, from the local filter
 * chips. The server accepts one value each; with several chips selected the
 * server param is left out and the client narrows locally.
 */
export function serverTrayFilters(filters: BoardFilters): {
  call_type?: "keep_full" | "auto_fill" | "will_call" | "one_off";
  product?: string;
  window?: "overdue" | "today" | "later";
} {
  const single = (values: string[]) =>
    values.length === 1 ? values[0] : undefined;
  return {
    call_type: oneOf(single(filters.call_type), [
      "keep_full",
      "auto_fill",
      "will_call",
      "one_off",
    ] as const),
    product: (() => {
      const p = single(filters.product);
      return p && /^[A-Za-z0-9_-]{1,32}$/.test(p) ? p : undefined;
    })(),
    window: oneOf(single(filters.window), [
      "overdue",
      "today",
      "later",
    ] as const),
  };
}
