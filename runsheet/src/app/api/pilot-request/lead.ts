/**
 * Pilot-request lead logic: validation, HubSpot field mapping, configuration.
 *
 * Separate from `route.ts` because Next.js type-checks route files against a
 * fixed export surface — HTTP verbs plus a handful of config fields — and
 * rejects the build outright on anything else:
 *
 *     Type error: Route "src/app/api/pilot-request/route.ts" does not match
 *     the required types of a Next.js Route.
 *       "validatePilotPayload" is not a valid Route export field.
 *
 * These functions are also the parts worth unit-testing directly, so having
 * them in a plain module is the better shape regardless of the constraint.
 */

/** HubSpot's unauthenticated form-submission endpoint. */
export const HUBSPOT_SUBMIT_BASE =
  "https://api.hsforms.com/submissions/v3/integration/submit";

/** The Hub ID for the Runsheet HubSpot account. */
export const DEFAULT_PORTAL_ID = "246986931";

/** Contact object type in HubSpot's object-type taxonomy. */
const CONTACT_OBJECT_TYPE_ID = "0-1";

/** Outbound call budget. HubSpot is fast; a hung call must not hold a worker. */
export const HUBSPOT_TIMEOUT_MS = 8000;

/**
 * Per-field input ceiling.
 *
 * This endpoint is public and unauthenticated by necessity, so it is the one
 * surface in the UI an anonymous caller can drive. Capping field length bounds
 * what a single request can cost us and what can be forwarded to HubSpot. It is
 * a limit, not spam protection.
 */
export const MAX_FIELD_LENGTH = 2000;

export interface PilotPayload {
  name: string;
  email: string;
  company: string;
  fleetSize: string;
  message: string;
}

export type ValidationResult =
  | { ok: true; value: PilotPayload }
  | { ok: false; errors: Record<string, string> };

function asString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Re-validate on the server, mirroring the client rules in the page.
 *
 * The client checks are for feedback only; anything can POST to the route
 * directly, so these are the checks that actually hold. Deliberately the same
 * predicates as the page's `validate`, so a submission the UI accepts is never
 * rejected here for a different reason.
 */
export function validatePilotPayload(body: unknown): ValidationResult {
  if (typeof body !== "object" || body === null) {
    return { ok: false, errors: { _: "Expected a JSON object" } };
  }
  const raw = body as Record<string, unknown>;
  const value: PilotPayload = {
    name: asString(raw.name),
    email: asString(raw.email),
    company: asString(raw.company),
    fleetSize: asString(raw.fleetSize),
    message: asString(raw.message),
  };

  const errors: Record<string, string> = {};
  if (!value.name) errors.name = "Your name is required";
  if (!value.email) {
    errors.email = "A work email is required";
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.email)) {
    errors.email = "Enter a valid email address";
  }
  if (!value.company) errors.company = "Company is required";
  if (!value.fleetSize) errors.fleetSize = "Select a fleet size";

  for (const [field, text] of Object.entries(value)) {
    if (text.length > MAX_FIELD_LENGTH) {
      errors[field] = `Must be ${MAX_FIELD_LENGTH} characters or fewer`;
    }
  }

  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return { ok: true, value };
}

/**
 * Map the payload onto HubSpot contact properties.
 *
 * Only DEFAULT contact properties are used — `email`, `firstname`, `lastname`,
 * `company`, `message`. Fleet size has no default property, and a submission
 * naming a field the form does not define is rejected outright by HubSpot, which
 * would fail the whole lead. So fleet size is prefixed onto the message text
 * rather than sent as its own field. That holds the required HubSpot setup to
 * five standard fields and removes the most likely misconfiguration. Promote it
 * to a real custom property later if you want to segment on it; the free tier
 * allows ten.
 *
 * `name` is split on the first space: HubSpot models first and last name
 * separately while the form asks for one full name. A single-word name yields an
 * empty lastname, which HubSpot accepts.
 */
export function toHubSpotFields(
  payload: PilotPayload,
): Array<{ objectTypeId: string; name: string; value: string }> {
  const spaceAt = payload.name.indexOf(" ");
  const firstName =
    spaceAt === -1 ? payload.name : payload.name.slice(0, spaceAt);
  const lastName = spaceAt === -1 ? "" : payload.name.slice(spaceAt + 1).trim();

  const messageParts = [`Fleet size: ${payload.fleetSize}`];
  if (payload.message) messageParts.push(payload.message);

  return [
    {
      objectTypeId: CONTACT_OBJECT_TYPE_ID,
      name: "email",
      value: payload.email,
    },
    {
      objectTypeId: CONTACT_OBJECT_TYPE_ID,
      name: "firstname",
      value: firstName,
    },
    { objectTypeId: CONTACT_OBJECT_TYPE_ID, name: "lastname", value: lastName },
    {
      objectTypeId: CONTACT_OBJECT_TYPE_ID,
      name: "company",
      value: payload.company,
    },
    {
      objectTypeId: CONTACT_OBJECT_TYPE_ID,
      name: "message",
      value: messageParts.join("\n\n"),
    },
  ];
}

export interface HubSpotConfig {
  portalId: string;
  formGuid: string;
}

/**
 * Resolve HubSpot configuration, or `null` when the form GUID is absent.
 *
 * There is deliberately no fallback GUID. A wrong one produces a 404 from
 * HubSpot on every submission, which at a glance is indistinguishable from the
 * bug this feature exists to fix. Absent configuration is reported as absent.
 */
export function resolveHubSpotConfig(
  env: NodeJS.ProcessEnv = process.env,
): HubSpotConfig | null {
  const formGuid = (env.HUBSPOT_FORM_GUID ?? "").trim();
  if (!formGuid) return null;
  return {
    portalId: (env.HUBSPOT_PORTAL_ID ?? "").trim() || DEFAULT_PORTAL_ID,
    formGuid,
  };
}
