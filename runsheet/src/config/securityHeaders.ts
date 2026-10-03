/**
 * HTTP security headers for every UI route (staging finding F6).
 *
 * Consumed by `next.config.ts`, which resolves this file outside the app
 * bundle, so it must stay pure: no imports and no `@/` aliases.
 *
 * The Content-Security-Policy is sent as `Content-Security-Policy-Report-Only`.
 * Every page (maps, charts, sign-in, invoices, ops) has not been walked in a
 * browser with an enforcing policy, so violations are reported to the browser
 * console instead of breaking a page. HSTS and the other headers are enforcing.
 */

/** One year, covering subdomains. Same value the API sends. */
export const HSTS_VALUE = "max-age=31536000; includeSubDomains";

/** Build-time inputs; the same `NEXT_PUBLIC_*` values the bundle inlines. */
export interface SecurityHeaderEnv {
  apiUrl?: string;
  wsUrl?: string;
  stApiDomain?: string;
  isDev: boolean;
}

/** Defaults used by the app code when the env vars are unset. */
const DEFAULT_API_URL = "http://localhost:8080/api";
const DEFAULT_WS_URL = "ws://localhost:8080";
const DEFAULT_ST_API_DOMAIN = "http://localhost:8080";

/**
 * Return `scheme://host[:port]` for `url`, or `fallback` when `url` is empty
 * or not a valid absolute URL.
 */
export function originOf(url: string | undefined, fallback: string): string {
  const candidate = (url ?? "").trim();
  if (!candidate) return fallback;
  try {
    const parsed = new URL(candidate);
    if (!parsed.host) return fallback;
    return `${parsed.protocol}//${parsed.host}`;
  } catch {
    return fallback;
  }
}

/** The ws(s) twin of an http(s) origin (the hooks derive WS URLs this way). */
function wsTwin(origin: string): string {
  return origin.replace(/^http/i, "ws");
}

/**
 * Build the UI Content-Security-Policy.
 *
 * - script-src needs 'unsafe-inline': App Router emits inline bootstrap
 *   scripts and there is no nonce plumbing. 'unsafe-eval' only in dev (React
 *   Refresh).
 * - connect-src covers the API, its ws(s) twin, the WS origin and the
 *   SuperTokens API domain (SDK auth calls), plus Google Maps.
 * - Google Maps / Charts load from maps.googleapis.com and www.gstatic.com,
 *   fonts from fonts.googleapis.com / fonts.gstatic.com, and use blob: workers.
 * - img-src allows any https: image (map tiles, Unsplash on sign-in).
 */
export function buildContentSecurityPolicy(env: SecurityHeaderEnv): string {
  const apiOrigin = originOf(env.apiUrl, originOf(DEFAULT_API_URL, ""));
  const wsOrigin = originOf(env.wsUrl, DEFAULT_WS_URL);
  const stOrigin = originOf(env.stApiDomain, DEFAULT_ST_API_DOMAIN);

  const connectSrc = Array.from(
    new Set([
      "'self'",
      apiOrigin,
      wsTwin(apiOrigin),
      wsOrigin,
      stOrigin,
      "https://maps.googleapis.com",
      "https://*.googleapis.com",
    ]),
  );

  const scriptSrc = [
    "'self'",
    "'unsafe-inline'",
    ...(env.isDev ? ["'unsafe-eval'"] : []),
    "https://maps.googleapis.com",
    "https://www.gstatic.com",
  ];

  const directives: Array<[string, string[]]> = [
    ["default-src", ["'self'"]],
    ["script-src", scriptSrc],
    [
      "style-src",
      [
        "'self'",
        "'unsafe-inline'",
        "https://fonts.googleapis.com",
        "https://www.gstatic.com",
      ],
    ],
    ["img-src", ["'self'", "data:", "blob:", "https:"]],
    ["font-src", ["'self'", "data:", "https://fonts.gstatic.com"]],
    ["connect-src", connectSrc],
    ["worker-src", ["'self'", "blob:"]],
    ["object-src", ["'none'"]],
    ["base-uri", ["'self'"]],
    ["form-action", ["'self'"]],
    ["frame-ancestors", ["'none'"]],
  ];

  return directives
    .map(([name, sources]) => `${name} ${sources.join(" ")}`)
    .join("; ");
}

/** The full header list applied to every route. */
export function buildSecurityHeaders(
  env: SecurityHeaderEnv,
): Array<{ key: string; value: string }> {
  return [
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "X-Frame-Options", value: "DENY" },
    { key: "X-XSS-Protection", value: "1; mode=block" },
    { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    {
      key: "Permissions-Policy",
      value: "camera=(), microphone=(), geolocation=(self)",
    },
    { key: "Strict-Transport-Security", value: HSTS_VALUE },
    {
      key: "Content-Security-Policy-Report-Only",
      value: buildContentSecurityPolicy(env),
    },
  ];
}
