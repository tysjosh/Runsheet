/**
 * Helpers for POST /api/csp-report (OI-10).
 *
 * Kept out of `route.ts` because Next.js rejects a route file that exports
 * anything beyond its fixed surface (same split as `pilot-request/lead.ts`).
 *
 * The endpoint is public by design: browsers send CSP violation reports
 * without credentials. Its only effect is a bounded, allowlisted log line, so
 * every input is treated as hostile:
 * - content type allowlist, 16 KiB body cap (checked on `content-length` and
 *   again while streaming), at most 20 reports per request;
 * - fixed-window rate limit per client key and globally;
 * - only allowlisted fields are logged, URLs are reduced to origin (blocked)
 *   or origin + path (document / source), and samples, referrers, headers,
 *   cookies and the client address are never logged.
 */

/** Content types browsers use for CSP reports (legacy and Reporting API). */
export const ACCEPTED_CONTENT_TYPES: ReadonlySet<string> = new Set([
  "application/csp-report",
  "application/reports+json",
  "application/json",
]);

export const MAX_BODY_BYTES = 16 * 1024;
export const MAX_REPORTS_PER_REQUEST = 20;
export const RATE_WINDOW_MS = 60_000;
export const PER_KEY_LIMIT = 30;
export const GLOBAL_LIMIT = 600;

/** Media type of a `content-type` header, lowercased, parameters dropped. */
export function mediaType(header: string | null): string {
  return (header ?? "").split(";")[0].trim().toLowerCase();
}

/**
 * Rate-limit key: the first `x-forwarded-for` hop (the ALB appends the real
 * client), else "unknown". Used only as a map key; never logged.
 */
export function clientKey(headers: Headers): string {
  const first = (headers.get("x-forwarded-for") ?? "").split(",")[0].trim();
  return first || "unknown";
}

/** In-memory fixed-window limiter. Per task, which is fine for a log sink. */
export class FixedWindowLimiter {
  private windowStart = 0;
  private total = 0;
  private perKey = new Map<string, number>();
  private throttleLogged = false;

  constructor(
    private readonly perKeyLimit: number,
    private readonly globalLimit: number,
    private readonly windowMs: number,
    private now: () => number = Date.now,
  ) {}

  /** Swap the clock (tests). */
  setClock(now: () => number): void {
    this.now = now;
  }

  reset(): void {
    this.windowStart = 0;
    this.total = 0;
    this.perKey.clear();
    this.throttleLogged = false;
  }

  /** Count one request for `key`; false when it is over either limit. */
  allow(key: string): boolean {
    const now = this.now();
    if (now - this.windowStart >= this.windowMs) {
      this.windowStart = now;
      this.total = 0;
      this.perKey.clear();
      this.throttleLogged = false;
    }
    // Global check first, so a flood of fresh keys can't grow the map past
    // GLOBAL_LIMIT entries per window.
    const count = this.perKey.get(key) ?? 0;
    if (this.total >= this.globalLimit || count >= this.perKeyLimit) {
      if (!this.throttleLogged) {
        this.throttleLogged = true;
        console.warn(
          "csp_report_throttled",
          JSON.stringify({ window_ms: this.windowMs }),
        );
      }
      return false;
    }
    this.perKey.set(key, count + 1);
    this.total += 1;
    return true;
  }
}

export const rateLimiter = new FixedWindowLimiter(
  PER_KEY_LIMIT,
  GLOBAL_LIMIT,
  RATE_WINDOW_MS,
);

/**
 * Read the body as text, or return null once it exceeds `maxBytes`.
 * `content-length` is checked first; the stream is counted too because the
 * header is optional and can lie.
 */
export async function readBodyCapped(
  request: Request,
  maxBytes: number,
): Promise<string | null> {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maxBytes) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (received > maxBytes) {
      await reader.cancel().catch(() => undefined);
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

type Raw = Record<string, unknown>;

function isObject(value: unknown): value is Raw {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Report bodies from either shape: legacy `{"csp-report": {...}}` or the
 * Reporting API array of `{type: "csp-violation", body: {...}}`. Unparseable
 * JSON yields none. At most MAX_REPORTS_PER_REQUEST are returned.
 */
export function extractReports(text: string): Raw[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return [];
  }
  let reports: Raw[] = [];
  if (isObject(parsed) && isObject(parsed["csp-report"])) {
    reports = [parsed["csp-report"]];
  } else if (Array.isArray(parsed)) {
    reports = parsed
      .filter(
        (item): item is Raw =>
          isObject(item) &&
          item.type === "csp-violation" &&
          isObject(item.body),
      )
      .map((item) => item.body as Raw);
  }
  return reports.slice(0, MAX_REPORTS_PER_REQUEST);
}

/** CSP keywords a browser reports in place of a blocked URL. */
const BLOCKED_KEYWORDS = new Set([
  "inline",
  "eval",
  "wasm-eval",
  "data",
  "blob",
  "self",
  "trusted-types-policy",
  "trusted-types-sink",
]);

function firstString(raw: Raw, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = raw[key];
    if (typeof value === "string" && value) return value;
  }
  return undefined;
}

function boundedInt(
  raw: Raw,
  max: number,
  ...keys: string[]
): number | undefined {
  for (const key of keys) {
    const value = raw[key];
    if (
      typeof value === "number" &&
      Number.isInteger(value) &&
      value >= 0 &&
      value <= max
    ) {
      return value;
    }
  }
  return undefined;
}

/** `blockedURL` → its origin, or a CSP keyword (`inline`, `eval`, `data`, …). */
export function reduceBlocked(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const lowered = value.trim().toLowerCase();
  if (BLOCKED_KEYWORDS.has(lowered)) return lowered;
  try {
    const url = new URL(value);
    if (url.protocol === "data:") return "data";
    if (url.protocol === "blob:") return "blob";
    if (/^(https?|wss?):$/.test(url.protocol)) return url.origin;
    return "other";
  } catch {
    return "other";
  }
}

/** A document or script URL → origin + path; query and fragment dropped. */
export function reducePage(value: string | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    if (!/^(https?):$/.test(url.protocol)) return undefined;
    return `${url.origin}${url.pathname}`.slice(0, 300);
  } catch {
    return undefined;
  }
}

/** The allowlisted fields of one report; everything else is discarded. */
export function sanitizeReport(raw: Raw): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  const directive = firstString(
    raw,
    "effectiveDirective",
    "effective-directive",
    "violated-directive",
  );
  // Legacy `violated-directive` can carry the directive's source list.
  const name = directive?.trim().split(/\s+/)[0];
  if (name && /^[a-z-]{1,40}$/.test(name)) out.directive = name;
  const disposition = firstString(raw, "disposition");
  if (disposition === "enforce" || disposition === "report") {
    out.disposition = disposition;
  }
  const blocked = reduceBlocked(firstString(raw, "blockedURL", "blocked-uri"));
  if (blocked) out.blocked = blocked;
  const page = reducePage(firstString(raw, "documentURL", "document-uri"));
  if (page) out.document = page;
  const source = reducePage(firstString(raw, "sourceFile", "source-file"));
  if (source) out.source = source;
  const line = boundedInt(raw, 10_000_000, "lineNumber", "line-number");
  if (line !== undefined) out.line = line;
  const column = boundedInt(raw, 10_000_000, "columnNumber", "column-number");
  if (column !== undefined) out.column = column;
  const status = boundedInt(raw, 999, "statusCode", "status-code");
  if (status !== undefined) out.status = status;
  return out;
}
