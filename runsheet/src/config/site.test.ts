/**
 * Tests for the indexability decision.
 *
 * The case that matters is `app.staging.runsheetops.com`. A substring check for
 * "runsheetops.com" — the obvious implementation — matches it, which would leave
 * staging indexable and reintroduce the exact bug this gate exists to fix. That
 * host is asserted explicitly below rather than left to a general "unknown hosts
 * are excluded" case.
 */
import { INDEXABLE_HOSTS, isIndexableSite } from "./site";

describe("isIndexableSite", () => {
  it.each([
    "https://app.runsheetops.com",
    "https://runsheetops.com",
    "https://www.runsheetops.com",
    "https://app.runsheetops.com/",
  ])("allows the production host %s", (url) => {
    expect(isIndexableSite(url)).toBe(true);
  });

  it("rejects the staging host, which a substring match would allow", () => {
    expect(isIndexableSite("https://app.staging.runsheetops.com")).toBe(false);
    // Guard the reasoning, not just the result: prove the substring approach
    // really would have been wrong here.
    expect("app.staging.runsheetops.com".includes("runsheetops.com")).toBe(
      true,
    );
  });

  it.each([
    "http://localhost:3000",
    "https://runsheet-git-feature.vercel.app",
    "https://api.staging.runsheetops.com",
    "https://runsheetops.com.evil.example",
    "https://notrunsheetops.com",
  ])("rejects the non-production host %s", (url) => {
    expect(isIndexableSite(url)).toBe(false);
  });

  it("ignores host casing", () => {
    expect(isIndexableSite("https://APP.RUNSHEETOPS.COM")).toBe(true);
  });

  it("fails closed on an unparseable value", () => {
    expect(isIndexableSite("")).toBe(false);
    expect(isIndexableSite("not a url")).toBe(false);
    expect(isIndexableSite("app.runsheetops.com")).toBe(false);
  });

  it("does not treat a port as part of the production host", () => {
    expect(isIndexableSite("https://app.runsheetops.com:8443")).toBe(false);
  });

  it("lists the production hosts explicitly, as an allowlist", () => {
    expect(INDEXABLE_HOSTS).toEqual([
      "app.runsheetops.com",
      "runsheetops.com",
      "www.runsheetops.com",
    ]);
  });
});
