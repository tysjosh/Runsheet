/**
 * Pilot-request lead capture — POST /api/pilot-request.
 *
 * Replaces the placeholder in `app/request-pilot/page.tsx`, whose
 * `submitPilotRequest` slept 900ms and resolved. Every lead submitted through
 * that page was discarded while the UI told the prospect "our team will reach
 * out at {email}". This route makes that claim true, or reports that it isn't.
 *
 * ---------------------------------------------------------------------------
 * Why a route handler and not the FastAPI backend
 * ---------------------------------------------------------------------------
 * The marketing page is public and unauthenticated. `main.py` passes an exact
 * origin allowlist to CORSMiddleware and puts every route behind the auth gate,
 * so a backend endpoint would mean carving an anonymous hole through that gate
 * for the one caller that must not be authenticated. Here the call is
 * same-origin, so there is no CORS surface at all.
 *
 * This works because the UI runs as a Node server from Next's `standalone`
 * output. It would NOT work under `output: "export"` — the same constraint that
 * already rules a static UI out (see `runsheet/Dockerfile`).
 *
 * ---------------------------------------------------------------------------
 * Configuration is read at REQUEST time, not build time
 * ---------------------------------------------------------------------------
 * Neither variable is `NEXT_PUBLIC_*`, so neither is inlined into the client
 * bundle: they are read from `process.env` per request and can therefore be
 * changed with an ECS task-definition update instead of an image rebuild. That
 * is the opposite of `NEXT_PUBLIC_API_URL` and friends, and the reason the UI
 * task definition's `environment` block is the right home for them.
 *
 *   HUBSPOT_PORTAL_ID   HubSpot Hub ID. Defaults to the known Runsheet portal.
 *   HUBSPOT_FORM_GUID   The form's GUID. NO DEFAULT — absent means 503.
 *
 * Neither is a secret: HubSpot's own embed code puts both in page source, and
 * the submit endpoint is the unauthenticated one, documented by HubSpot as
 * CORS-enabled for direct browser use. So no Secrets Manager entry is needed. We
 * call it server-side anyway, to validate before forwarding and to keep the
 * payload shape under our control rather than the browser's.
 *
 * Validation, field mapping and config live in `./lead` — Next.js rejects the
 * build if a route file exports anything outside its fixed surface.
 */
import { NextResponse } from "next/server";

import {
  HUBSPOT_SUBMIT_BASE,
  HUBSPOT_TIMEOUT_MS,
  resolveHubSpotConfig,
  toHubSpotFields,
  validatePilotPayload,
} from "./lead";

/** Node runtime: this handler makes an outbound fetch and reads process.env. */
export const runtime = "nodejs";
/** Never prerendered or cached — a mutation with per-request configuration. */
export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json(
      { error: "invalid_json", message: "Request body must be JSON." },
      { status: 400 },
    );
  }

  const validated = validatePilotPayload(body);
  if (!validated.ok) {
    return NextResponse.json(
      { error: "validation_failed", fields: validated.errors },
      { status: 400 },
    );
  }

  const config = resolveHubSpotConfig();
  if (!config) {
    // 503, not 200. The lead is NOT captured, and saying otherwise is the
    // original defect. The page turns this into "email us directly".
    console.error(
      "pilot-request: HUBSPOT_FORM_GUID is not set — lead was not captured",
    );
    return NextResponse.json(
      {
        error: "not_configured",
        message: "Lead capture is not configured on this environment.",
      },
      { status: 503 },
    );
  }

  const url = `${HUBSPOT_SUBMIT_BASE}/${config.portalId}/${config.formGuid}`;
  const submission = {
    fields: toHubSpotFields(validated.value),
    context: {
      pageUri: `${new URL(request.url).origin}/request-pilot`,
      pageName: "Request a Pilot",
    },
  };

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(submission),
      signal: AbortSignal.timeout(HUBSPOT_TIMEOUT_MS),
    });
  } catch (cause) {
    console.error("pilot-request: HubSpot unreachable", cause);
    return NextResponse.json(
      { error: "upstream_unreachable", message: "Could not reach HubSpot." },
      { status: 502 },
    );
  }

  if (!upstream.ok) {
    // Logged, not returned. HubSpot's error bodies echo submitted values, and
    // this response goes to an anonymous caller.
    const detail = await upstream.text().catch(() => "<unreadable>");
    console.error(
      `pilot-request: HubSpot rejected the submission (${upstream.status}): ${detail}`,
    );
    return NextResponse.json(
      {
        error: "upstream_rejected",
        message: "HubSpot rejected the submission.",
      },
      { status: 502 },
    );
  }

  return NextResponse.json({ ok: true }, { status: 200 });
}
