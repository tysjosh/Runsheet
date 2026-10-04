/**
 * Unit tests for the shared API error helpers (F-1).
 *
 * The extractor must handle every error body shape the backend produces and
 * must never yield "[object Object]".
 */
import { ApiError } from "./api";
import {
  apiErrorFromResponse,
  classifyLoadError,
  extractApiErrorCode,
  extractApiErrorMessage,
} from "./apiErrors";

const FALLBACK = "HTTP error! status: 500";

function expectClean(result: string) {
  expect(result).not.toContain("[object Object]");
}

describe("extractApiErrorMessage", () => {
  const cases: Array<[string, unknown, string]> = [
    ["plain string body", "Upstream timeout", "Upstream timeout"],
    ["string detail", { detail: "Terminal not found" }, "Terminal not found"],
    [
      "object detail with message",
      {
        detail: {
          error_code: "CUSTOMERS_DISABLED",
          message: "Commerce customers module is not enabled for this tenant",
        },
      },
      "Commerce customers module is not enabled for this tenant",
    ],
    [
      "object detail with only error_code",
      { detail: { error_code: "INVOICING_DISABLED" } },
      "API error: INVOICING_DISABLED",
    ],
    [
      "object detail with code alias",
      { detail: { code: "QUOTA_EXCEEDED" } },
      "API error: QUOTA_EXCEEDED",
    ],
    ["object detail with msg", { detail: { msg: "bad thing" } }, "bad thing"],
    [
      "single validation error",
      {
        detail: [
          { loc: ["body", "email"], msg: "field required", type: "missing" },
        ],
      },
      "email: field required",
    ],
    [
      "multiple validation errors",
      {
        detail: [
          { loc: ["body", "email"], msg: "field required", type: "missing" },
          { loc: ["body", "name"], msg: "too short", type: "string_too_short" },
          { msg: "no location" },
        ],
      },
      "email: field required; name: too short; no location",
    ],
    [
      "AppException envelope",
      {
        error_code: "INSUFFICIENT_ROLE",
        message: "Caller lacks a required role for this operation",
        details: { required: ["platform_admin"] },
        request_id: "req-1",
      },
      "Caller lacks a required role for this operation",
    ],
    ["top-level message", { message: "Nope" }, "Nope"],
    ["error string", { error: "Bad gateway" }, "Bad gateway"],
    ["error object", { error: { message: "Rate limited" } }, "Rate limited"],
    [
      "top-level code only",
      { error_code: "SOMETHING_ODD" },
      "API error: SOMETHING_ODD",
    ],
    ["whitespace detail falls through", { detail: "   ", message: "m" }, "m"],
  ];

  it.each(cases)("%s", (_name, body, expected) => {
    const result = extractApiErrorMessage(body, FALLBACK);
    expect(result).toBe(expected);
    expectClean(result);
  });

  const fallbackCases: Array<[string, unknown]> = [
    ["null", null],
    ["undefined", undefined],
    ["empty object", {}],
    ["number", 42],
    ["empty array", []],
    ["empty string", ""],
    ["empty detail object", { detail: {} }],
    ["nested non-string message", { detail: { message: { nested: 1 } } }],
    ["validation array with no msg", { detail: [{ loc: ["x"], type: "t" }] }],
    ["object message", { message: { a: 1 } }],
    ["object error with object message", { error: { message: { a: 1 } } }],
  ];

  it.each(fallbackCases)("%s falls back", (_name, body) => {
    const result = extractApiErrorMessage(body, FALLBACK);
    expect(result).toBe(FALLBACK);
    expectClean(result);
  });
});

describe("extractApiErrorCode", () => {
  it("reads nested detail.error_code", () => {
    expect(
      extractApiErrorCode({ detail: { error_code: "CUSTOMERS_DISABLED" } }),
    ).toBe("CUSTOMERS_DISABLED");
  });
  it("reads nested detail.code", () => {
    expect(extractApiErrorCode({ detail: { code: "X" } })).toBe("X");
  });
  it("reads top-level error_code", () => {
    expect(extractApiErrorCode({ error_code: "INSUFFICIENT_ROLE" })).toBe(
      "INSUFFICIENT_ROLE",
    );
  });
  it("prefers nested over top-level", () => {
    expect(
      extractApiErrorCode({ detail: { error_code: "A" }, error_code: "B" }),
    ).toBe("A");
  });
  it("ignores non-string codes and non-objects", () => {
    expect(extractApiErrorCode({ detail: { error_code: 5 } })).toBeUndefined();
    expect(extractApiErrorCode(null)).toBeUndefined();
    expect(extractApiErrorCode("text")).toBeUndefined();
    expect(extractApiErrorCode({ detail: "string" })).toBeUndefined();
  });
});

describe("apiErrorFromResponse", () => {
  function fakeResponse(status: number, json: () => Promise<unknown>) {
    return { status, ok: false, json } as unknown as Response;
  }

  it("builds an ApiError with message, status and code", async () => {
    const err = await apiErrorFromResponse(
      fakeResponse(404, async () => ({
        detail: {
          error_code: "CUSTOMERS_DISABLED",
          message: "Commerce customers module is not enabled for this tenant",
        },
      })),
    );
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.code).toBe("CUSTOMERS_DISABLED");
    expect(err.message).toBe(
      "Commerce customers module is not enabled for this tenant",
    );
  });

  it("falls back to the HTTP status message for non-JSON bodies", async () => {
    const err = await apiErrorFromResponse(
      fakeResponse(502, async () => {
        throw new SyntaxError("Unexpected token <");
      }),
    );
    expect(err.status).toBe(502);
    expect(err.code).toBeUndefined();
    expect(err.message).toBe("HTTP error! status: 502");
  });
});

describe("classifyLoadError", () => {
  it("maps a plain 404 to not_found", () => {
    const f = classifyLoadError(
      new ApiError("Terminal not found", 404, "terminal_not_found"),
      "fallback",
    );
    expect(f.kind).toBe("not_found");
    expect(f.message).toBe("Terminal not found");
  });

  it("maps known *_DISABLED codes to module_disabled", () => {
    expect(
      classifyLoadError(new ApiError("x", 404, "CUSTOMERS_DISABLED"), "f"),
    ).toMatchObject({ kind: "module_disabled", moduleName: "Customers" });
    expect(
      classifyLoadError(new ApiError("x", 404, "INVOICING_DISABLED"), "f"),
    ).toMatchObject({ kind: "module_disabled", moduleName: "Invoicing" });
  });

  it("derives a module name for unknown *_DISABLED codes", () => {
    expect(
      classifyLoadError(new ApiError("x", 404, "FUEL_OPS_DISABLED"), "f"),
    ).toMatchObject({ kind: "module_disabled", moduleName: "Fuel Ops" });
  });

  it("maps 403 to forbidden", () => {
    expect(
      classifyLoadError(new ApiError("no", 403, "INSUFFICIENT_ROLE"), "f").kind,
    ).toBe("forbidden");
  });

  it("maps other statuses to error with the message", () => {
    expect(classifyLoadError(new ApiError("boom", 500), "f")).toMatchObject({
      kind: "error",
      message: "boom",
      status: 500,
    });
  });

  it("uses the message of a plain Error", () => {
    expect(classifyLoadError(new Error("network down"), "f")).toMatchObject({
      kind: "error",
      message: "network down",
    });
  });

  it("falls back for non-Errors and [object Object] messages", () => {
    expect(classifyLoadError("oops", "Failed to load")).toEqual({
      kind: "error",
      message: "Failed to load",
      status: undefined,
      code: undefined,
    });
    expect(
      classifyLoadError(new Error("[object Object]"), "Failed to load").message,
    ).toBe("Failed to load");
  });
});
