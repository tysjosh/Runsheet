/**
 * CSP violation reports — POST /api/csp-report (OI-10).
 *
 * The UI sends `Content-Security-Policy-Report-Only` with `report-uri` and
 * `report-to` pointing here, so violations land in the UI task's log stream
 * (CloudWatch) instead of only in each visitor's console. The policy stays
 * report-only; enforcing is a follow-up after a few days of clean reports.
 *
 * A Next.js route handler rather than a FastAPI endpoint, for the reason given
 * in `../pilot-request/route.ts`: the backend puts every route behind the auth
 * gate with an exact CORS allowlist, while browsers send these reports
 * same-origin and without credentials. Staging routes `app.<domain>` wholly to
 * the UI, so this path reaches Next.
 *
 * Unauthenticated by design; the only effect is a bounded log line. Limits,
 * parsing and the field allowlist live in `./report`.
 */
import {
  ACCEPTED_CONTENT_TYPES,
  clientKey,
  extractReports,
  MAX_BODY_BYTES,
  mediaType,
  rateLimiter,
  readBodyCapped,
  sanitizeReport,
} from "./report";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function empty(status: number): Response {
  return new Response(null, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

export async function POST(request: Request): Promise<Response> {
  if (
    !ACCEPTED_CONTENT_TYPES.has(mediaType(request.headers.get("content-type")))
  ) {
    return empty(415);
  }
  if (!rateLimiter.allow(clientKey(request.headers))) {
    return empty(429);
  }
  const text = await readBodyCapped(request, MAX_BODY_BYTES);
  if (text === null) {
    return empty(413);
  }
  for (const report of extractReports(text)) {
    console.warn("csp_violation", JSON.stringify(sanitizeReport(report)));
  }
  return empty(204);
}
