/**
 * Tests for robots.txt and sitemap.xml environment gating.
 *
 * `config/site.ts` reads `NEXT_PUBLIC_SITE_URL` at module scope, so each case
 * sets the variable and re-imports through `jest.isolateModules` to get a fresh
 * evaluation. Mutating `process.env` alone would not change an already-computed
 * module constant.
 *
 * The assertions are deliberately about ABSENCE on staging — no allow rules, no
 * sitemap pointer, no URLs. Staging previously served `Allow: /` plus a sitemap
 * of its own URLs, and a test that only checked production would have passed
 * against that.
 */

function withSiteUrl<T>(siteUrl: string, load: () => T): T {
  const previous = process.env.NEXT_PUBLIC_SITE_URL;
  process.env.NEXT_PUBLIC_SITE_URL = siteUrl;
  let result: T | undefined;
  jest.isolateModules(() => {
    result = load();
  });
  process.env.NEXT_PUBLIC_SITE_URL = previous;
  return result as T;
}

const PRODUCTION = "https://app.runsheetops.com";
const STAGING = "https://app.staging.runsheetops.com";

describe("robots.txt", () => {
  it("disallows everything on staging", () => {
    const result = withSiteUrl(STAGING, () => require("./robots").default());
    expect(result.rules).toEqual([{ userAgent: "*", disallow: "/" }]);
  });

  it("advertises no sitemap on staging", () => {
    // A sitemap is itself an invitation to crawl, so pointing at one would
    // undercut the disallow above.
    const result = withSiteUrl(STAGING, () => require("./robots").default());
    expect(result.sitemap).toBeUndefined();
    expect(result.host).toBeUndefined();
  });

  it("does not allow any path on staging", () => {
    const result = withSiteUrl(STAGING, () => require("./robots").default());
    const rules = Array.isArray(result.rules) ? result.rules : [result.rules];
    for (const rule of rules) {
      expect(rule.allow).toBeUndefined();
    }
  });

  it("allows the public marketing routes on production", () => {
    const result = withSiteUrl(PRODUCTION, () => require("./robots").default());
    const rules = Array.isArray(result.rules) ? result.rules : [result.rules];
    expect(rules[0].allow).toEqual([
      "/",
      "/request-pilot",
      "/signin",
      "/privacy",
    ]);
  });

  it("keeps the authenticated application out of the index on production", () => {
    const result = withSiteUrl(PRODUCTION, () => require("./robots").default());
    const rules = Array.isArray(result.rules) ? result.rules : [result.rules];
    expect(rules[0].disallow).toEqual([
      "/dashboard",
      "/ops",
      "/commerce",
      "/api",
      "/auth",
    ]);
  });

  it("points at the production sitemap on production", () => {
    const result = withSiteUrl(PRODUCTION, () => require("./robots").default());
    expect(result.sitemap).toBe(`${PRODUCTION}/sitemap.xml`);
  });
});

describe("sitemap.xml", () => {
  it("is empty on staging", () => {
    const result = withSiteUrl(STAGING, () => require("./sitemap").default());
    expect(result).toEqual([]);
  });

  it("never emits a staging URL", () => {
    const result = withSiteUrl(STAGING, () => require("./sitemap").default());
    expect(JSON.stringify(result)).not.toContain("staging");
  });

  it("lists the public routes on production", () => {
    const result = withSiteUrl(PRODUCTION, () =>
      require("./sitemap").default(),
    );
    expect(result.map((entry: { url: string }) => entry.url)).toEqual([
      PRODUCTION,
      `${PRODUCTION}/request-pilot`,
      `${PRODUCTION}/privacy`,
      `${PRODUCTION}/signin`,
    ]);
  });
});
