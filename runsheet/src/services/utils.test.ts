/**
 * Unit tests for the shared HTTP scaffolding in ``services/utils.ts``.
 *
 * Focuses on ``buildQueryString`` — a load-bearing shared helper after the UI
 * scaffolding consolidation. It must:
 *
 *  • omit ``undefined`` / ``null`` / empty-string values,
 *  • URL-encode keys and values,
 *  • return ``""`` when no usable params remain,
 *  • produce a ``?key=value&...`` string for valid params,
 *  • stringify numbers and booleans.
 *
 * These are pure-function tests, so no ``fetch`` mocking is required.
 *
 * _Validates: Requirements 2.3, 3.2_
 */

import { buildQueryString } from "./utils";

describe("buildQueryString", () => {
  it("returns an empty string when there are no params", () => {
    expect(buildQueryString({})).toBe("");
  });

  it("returns an empty string when every value is unusable", () => {
    expect(buildQueryString({ a: undefined, b: null, c: "" })).toBe("");
  });

  it("omits undefined / null / empty-string values", () => {
    expect(
      buildQueryString({
        keep: "yes",
        skipUndefined: undefined,
        skipNull: null,
        skipEmpty: "",
      }),
    ).toBe("?keep=yes");
  });

  it("produces a ?key=value&... string for multiple valid params", () => {
    const qs = buildQueryString({ page: 2, size: 50 });
    expect(qs.startsWith("?")).toBe(true);

    const params = new URLSearchParams(qs.slice(1));
    expect(params.get("page")).toBe("2");
    expect(params.get("size")).toBe("50");
  });

  it("stringifies numbers and booleans", () => {
    const params = new URLSearchParams(
      buildQueryString({ count: 0, active: false, ratio: 1.5 }).slice(1),
    );
    expect(params.get("count")).toBe("0");
    expect(params.get("active")).toBe("false");
    expect(params.get("ratio")).toBe("1.5");
  });

  it("URL-encodes values containing spaces and special characters", () => {
    const qs = buildQueryString({ q: "hello world & friends" });
    // The raw query string must not contain a literal space or ampersand
    // that would be parsed as a delimiter.
    expect(qs).not.toContain(" ");

    const params = new URLSearchParams(qs.slice(1));
    expect(params.get("q")).toBe("hello world & friends");
  });

  it("URL-encodes keys containing special characters", () => {
    const qs = buildQueryString({ "weird key": "v" });
    expect(qs).not.toContain(" ");

    const params = new URLSearchParams(qs.slice(1));
    expect(params.get("weird key")).toBe("v");
  });

  it("keeps the literal string 'false' rather than treating it as falsy", () => {
    // Only undefined/null/"" are filtered — the string "false" is a real value.
    expect(buildQueryString({ flag: "false" })).toBe("?flag=false");
  });
});
