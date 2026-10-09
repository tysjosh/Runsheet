/**
 * getMarginAvailability (Phase 3, owner-reported staging bug): staging runs
 * with COMMERCE_MARGIN_FEED_ENABLED off, so every margin route answers 404
 * COMMERCE_DISABLED (confirmed against api.staging for admin and driver).
 * Only that code means "disabled"; 403 is "forbidden"; anything else is
 * "unknown" so the pages keep showing their own errors.
 */
jest.mock("./api", () => {
  const actual = jest.requireActual("./api");
  return { ...actual, fetchWithSession: jest.fn() };
});

import { fetchWithSession } from "./api";
import { getMarginAvailability, isMarginDisabledError } from "./marginApi";

const mockFetch = fetchWithSession as jest.Mock;

function respond(status: number, body: unknown) {
  // jsdom has no Response; the client only reads ok, status and json().
  mockFetch.mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
}

afterEach(() => jest.resetAllMocks());

it("is enabled when settings load", async () => {
  respond(200, { data: { feed_activated_at: null } });
  await expect(getMarginAvailability()).resolves.toBe("enabled");
});

it("is disabled on 404 COMMERCE_DISABLED", async () => {
  respond(404, {
    error_code: "COMMERCE_DISABLED",
    message: "Margin feed is not enabled",
    details: {},
  });
  await expect(getMarginAvailability()).resolves.toBe("disabled");
});

it("is forbidden on 403", async () => {
  respond(403, {
    error_code: "INSUFFICIENT_ROLE",
    message: "Caller lacks a required role for this operation",
    details: {},
  });
  await expect(getMarginAvailability()).resolves.toBe("forbidden");
});

it.each([
  [404, "NOT_FOUND"],
  [500, "INTERNAL_ERROR"],
])("is unknown on %s %s (not a blanket 404)", async (status, code) => {
  respond(status, { error_code: code, message: "x", details: {} });
  await expect(getMarginAvailability()).resolves.toBe("unknown");
});

it("matches only status 404 with the COMMERCE_DISABLED code", () => {
  expect(
    isMarginDisabledError({ status: 404, code: "COMMERCE_DISABLED" }),
  ).toBe(true);
  expect(isMarginDisabledError({ status: 404, code: "NOT_FOUND" })).toBe(false);
  expect(
    isMarginDisabledError({ status: 403, code: "COMMERCE_DISABLED" }),
  ).toBe(false);
  expect(isMarginDisabledError(null)).toBe(false);
});
