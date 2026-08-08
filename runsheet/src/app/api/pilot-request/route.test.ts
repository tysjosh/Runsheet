/**
 * @jest-environment node
 *
 * Tests for POST /api/pilot-request.
 *
 * The node environment (not the suite-wide jsdom) is required because this is a
 * route handler: it needs the real `Request`/`Response` globals and `fetch`,
 * which jsdom does not provide.
 *
 * The assertions that matter most are the NEGATIVE ones. The defect being fixed
 * was a handler that reported success without capturing anything, so tests that
 * only prove the happy path would have passed against the broken placeholder
 * too. Each failure mode below asserts a non-2xx status.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  HONEYPOT_FIELD,
  isHoneypotTripped,
  resolveHubSpotConfig,
  toHubSpotFields,
  validatePilotPayload,
} from "./lead";
import { POST } from "./route";

const VALID = {
  name: "Jordan Rivera",
  email: "jordan@distributor.com",
  company: "Acme Fuel Co",
  fleetSize: "11–50 trucks",
  message: "cutting runouts across 40 retail stations",
};

function post(body: unknown): Request {
  return new Request("https://app.staging.runsheetops.com/api/pilot-request", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

const ORIGINAL_ENV = process.env;

beforeEach(() => {
  process.env = { ...ORIGINAL_ENV, HUBSPOT_FORM_GUID: "test-guid" };
  jest.spyOn(console, "error").mockImplementation(() => {});
  jest.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  process.env = ORIGINAL_ENV;
  jest.restoreAllMocks();
});

describe("validatePilotPayload", () => {
  it("accepts a complete payload and trims whitespace", () => {
    const result = validatePilotPayload({
      ...VALID,
      name: "  Jordan Rivera  ",
    });
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.name).toBe("Jordan Rivera");
  });

  it("treats an optional message as present-but-empty, not invalid", () => {
    const result = validatePilotPayload({ ...VALID, message: "" });
    expect(result.ok).toBe(true);
  });

  it.each(["name", "company", "fleetSize"] as const)(
    "rejects a missing %s",
    (field) => {
      const result = validatePilotPayload({ ...VALID, [field]: "" });
      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.errors[field]).toBeDefined();
    },
  );

  it("rejects a malformed email", () => {
    const result = validatePilotPayload({ ...VALID, email: "not-an-email" });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.errors.email).toBeDefined();
  });

  it("rejects a field beyond the length ceiling", () => {
    const result = validatePilotPayload({
      ...VALID,
      message: "x".repeat(2001),
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.errors.message).toBeDefined();
  });

  it("rejects a non-object body", () => {
    expect(validatePilotPayload("nope").ok).toBe(false);
    expect(validatePilotPayload(null).ok).toBe(false);
  });
});

describe("toHubSpotFields", () => {
  it("splits a full name into firstname and lastname", () => {
    const fields = toHubSpotFields({ ...VALID, name: "Jordan Van Rivera" });
    const byName = Object.fromEntries(fields.map((f) => [f.name, f.value]));
    expect(byName.firstname).toBe("Jordan");
    expect(byName.lastname).toBe("Van Rivera");
  });

  it("leaves lastname empty for a single-word name", () => {
    const fields = toHubSpotFields({ ...VALID, name: "Jordan" });
    const byName = Object.fromEntries(fields.map((f) => [f.name, f.value]));
    expect(byName.firstname).toBe("Jordan");
    expect(byName.lastname).toBe("");
  });

  it("folds fleet size into the message so no custom property is required", () => {
    const fields = toHubSpotFields(VALID);
    const byName = Object.fromEntries(fields.map((f) => [f.name, f.value]));
    expect(byName.message).toContain("Fleet size: 11–50 trucks");
    expect(byName.message).toContain(VALID.message);
    // A field HubSpot's form does not define would fail the whole submission.
    expect(fields.map((f) => f.name)).not.toContain("fleetSize");
  });

  it("sends only default HubSpot contact properties", () => {
    expect(
      toHubSpotFields(VALID)
        .map((f) => f.name)
        .sort(),
    ).toEqual(["company", "email", "firstname", "lastname", "message"]);
  });
});

describe("resolveHubSpotConfig", () => {
  it("returns null when the form GUID is absent, rather than guessing one", () => {
    expect(resolveHubSpotConfig({} as NodeJS.ProcessEnv)).toBeNull();
    expect(
      resolveHubSpotConfig({ HUBSPOT_FORM_GUID: "   " } as NodeJS.ProcessEnv),
    ).toBeNull();
  });

  it("defaults the portal id but never the form guid", () => {
    const config = resolveHubSpotConfig({
      HUBSPOT_FORM_GUID: "abc",
    } as NodeJS.ProcessEnv);
    expect(config).toEqual({ portalId: "246986931", formGuid: "abc" });
  });

  it("lets the environment override the portal id", () => {
    const config = resolveHubSpotConfig({
      HUBSPOT_FORM_GUID: "abc",
      HUBSPOT_PORTAL_ID: "999",
    } as NodeJS.ProcessEnv);
    expect(config?.portalId).toBe("999");
  });
});

describe("POST", () => {
  it("forwards a valid lead to HubSpot and returns 200", async () => {
    const fetchMock = jest
      .spyOn(global, "fetch")
      .mockResolvedValue(new Response("{}", { status: 200 }));

    const response = await POST(post(VALID));

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ ok: true });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.hsforms.com/submissions/v3/integration/submit/246986931/test-guid",
    );
    const sent = JSON.parse(String(init?.body));
    expect(sent.fields).toEqual(toHubSpotFields(VALID));
    expect(sent.context.pageUri).toBe(
      "https://app.staging.runsheetops.com/request-pilot",
    );
  });

  it("returns 400 and does not call HubSpot when validation fails", async () => {
    const fetchMock = jest.spyOn(global, "fetch");
    const response = await POST(post({ ...VALID, email: "bad" }));
    expect(response.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns 400 for a body that is not JSON", async () => {
    const request = new Request(
      "https://app.staging.runsheetops.com/api/pilot-request",
      {
        method: "POST",
        body: "{not json",
      },
    );
    expect((await POST(request)).status).toBe(400);
  });

  it("returns 503 when the form GUID is unset, never a false success", async () => {
    delete process.env.HUBSPOT_FORM_GUID;
    const fetchMock = jest.spyOn(global, "fetch");

    const response = await POST(post(VALID));

    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toMatchObject({
      error: "not_configured",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns 502 when HubSpot rejects the submission", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ message: "Form not found" }), {
        status: 404,
      }),
    );
    const response = await POST(post(VALID));
    expect(response.status).toBe(502);
  });

  it("returns 502 when HubSpot is unreachable", async () => {
    jest.spyOn(global, "fetch").mockRejectedValue(new Error("ETIMEDOUT"));
    const response = await POST(post(VALID));
    expect(response.status).toBe(502);
  });

  it("does not echo HubSpot's error body to the caller", async () => {
    // HubSpot error bodies quote the submitted values back; this response goes
    // to an anonymous caller, so the detail belongs in the log only.
    jest
      .spyOn(global, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({ message: `Invalid email: ${VALID.email}` }),
          { status: 400 },
        ),
      );
    const response = await POST(post(VALID));
    expect(JSON.stringify(await response.json())).not.toContain(VALID.email);
  });
});

describe("honeypot", () => {
  it("is not tripped by an absent or empty field", () => {
    expect(isHoneypotTripped(VALID)).toBe(false);
    expect(isHoneypotTripped({ ...VALID, [HONEYPOT_FIELD]: "" })).toBe(false);
    expect(isHoneypotTripped({ ...VALID, [HONEYPOT_FIELD]: "   " })).toBe(
      false,
    );
  });

  it("is tripped by any real content", () => {
    expect(
      isHoneypotTripped({ ...VALID, [HONEYPOT_FIELD]: "http://spam.example" }),
    ).toBe(true);
  });

  it("tolerates a non-object body", () => {
    expect(isHoneypotTripped(null)).toBe(false);
    expect(isHoneypotTripped("nope")).toBe(false);
  });

  it("drops the submission without forwarding it to HubSpot", async () => {
    const fetchMock = jest.spyOn(global, "fetch");

    const response = await POST(
      post({ ...VALID, [HONEYPOT_FIELD]: "http://spam.example" }),
    );

    // 200 so a bot learns nothing, but nothing reaches the CRM.
    expect(response.status).toBe(200);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("is checked before validation, so a bot cannot fingerprint it", async () => {
    // Invalid payload AND honeypot filled: the response must be the honeypot's
    // 200, not the 400 an ordinary invalid submission gets. Otherwise comparing
    // the two responses reveals which field is the trap.
    const fetchMock = jest.spyOn(global, "fetch");
    const response = await POST(
      post({ ...VALID, email: "bad", [HONEYPOT_FIELD]: "spam" }),
    );
    expect(response.status).toBe(200);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never forwards the honeypot field to HubSpot", () => {
    expect(toHubSpotFields(VALID).map((f) => f.name)).not.toContain(
      HONEYPOT_FIELD,
    );
  });

  it("uses the same field name as the form renders", () => {
    // The page hardcodes the name rather than importing it, to keep the server
    // module out of the client bundle. That duplication is only safe if
    // something fails when the two drift apart.
    const page = readFileSync(
      join(__dirname, "..", "..", "request-pilot", "page.tsx"),
      "utf8",
    );
    expect(page).toContain(`const HONEYPOT_FIELD = "${HONEYPOT_FIELD}"`);
  });
});
