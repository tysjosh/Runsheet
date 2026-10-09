/**
 * @jest-environment node
 */
/**
 * Tests for the UI security headers (staging finding F6): HSTS on every route
 * and a report-only Content-Security-Policy whose connect-src follows the
 * build-time API / WS / SuperTokens origins.
 */
import nextConfig from "../../next.config";
import {
  buildContentSecurityPolicy,
  buildSecurityHeaders,
  HSTS_VALUE,
  originOf,
} from "./securityHeaders";

function directive(policy: string, name: string): string[] {
  const entry = policy
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name} `));
  return entry ? entry.split(/\s+/).slice(1) : [];
}

describe("HSTS_VALUE", () => {
  it("is one year including subdomains", () => {
    expect(HSTS_VALUE).toBe("max-age=31536000; includeSubDomains");
  });
});

describe("originOf", () => {
  it("strips path and keeps scheme, host and port", () => {
    expect(originOf("https://api.example.com/api", "x")).toBe(
      "https://api.example.com",
    );
    expect(originOf("ws://localhost:8080/ws", "x")).toBe("ws://localhost:8080");
  });

  it.each([undefined, "", "   ", "not a url"])("falls back for %p", (value) => {
    expect(originOf(value, "http://fallback:1")).toBe("http://fallback:1");
  });
});

describe("buildContentSecurityPolicy", () => {
  it("allows the staging API and its wss twin", () => {
    const policy = buildContentSecurityPolicy({
      apiUrl: "https://api.staging.runsheetops.com/api",
      wsUrl: "wss://api.staging.runsheetops.com",
      stApiDomain: "https://api.staging.runsheetops.com",
      isDev: false,
    });
    const connect = directive(policy, "connect-src");
    expect(connect).toContain("https://api.staging.runsheetops.com");
    expect(connect).toContain("wss://api.staging.runsheetops.com");
    expect(connect).toContain("https://maps.googleapis.com");
  });

  it("defaults to the localhost:8080 origins", () => {
    const connect = directive(
      buildContentSecurityPolicy({ isDev: false }),
      "connect-src",
    );
    expect(connect).toEqual(
      expect.arrayContaining([
        "'self'",
        "http://localhost:8080",
        "ws://localhost:8080",
      ]),
    );
  });

  it("allows 'unsafe-eval' only in development", () => {
    const dev = directive(
      buildContentSecurityPolicy({ isDev: true }),
      "script-src",
    );
    const prod = directive(
      buildContentSecurityPolicy({ isDev: false }),
      "script-src",
    );
    expect(dev).toContain("'unsafe-eval'");
    expect(prod).not.toContain("'unsafe-eval'");
    expect(prod).toContain("https://maps.googleapis.com");
    expect(prod).toContain("https://www.gstatic.com");
  });

  it("reports violations to /api/csp-report (OI-10)", () => {
    const policy = buildContentSecurityPolicy({ isDev: false });
    expect(directive(policy, "report-uri")).toEqual(["/api/csp-report"]);
    expect(directive(policy, "report-to")).toEqual(["csp-endpoint"]);
  });

  it("forbids framing and plugins", () => {
    const policy = buildContentSecurityPolicy({ isDev: false });
    expect(directive(policy, "frame-ancestors")).toEqual(["'none'"]);
    expect(directive(policy, "object-src")).toEqual(["'none'"]);
  });
});

describe("Stripe origins for the portal pay page (UI-5, PD25)", () => {
  it.each([false, true])(
    "allows Stripe.js, its frames and API (isDev=%p)",
    (isDev) => {
      const policy = buildContentSecurityPolicy({ isDev });
      expect(directive(policy, "script-src")).toContain(
        "https://js.stripe.com",
      );
      expect(directive(policy, "frame-src")).toEqual([
        "https://js.stripe.com",
        "https://hooks.stripe.com",
        "https://*.stripe.com",
      ]);
      expect(directive(policy, "connect-src")).toEqual(
        expect.arrayContaining([
          "https://api.stripe.com",
          "https://*.stripe.com",
        ]),
      );
    },
  );

  it("adds Stripe without dropping the existing sources", () => {
    const policy = buildContentSecurityPolicy({
      apiUrl: "https://api.staging.example/api",
      isDev: false,
    });
    expect(directive(policy, "connect-src")).toEqual(
      expect.arrayContaining([
        "'self'",
        "https://api.staging.example",
        "wss://api.staging.example",
        "https://maps.googleapis.com",
      ]),
    );
    expect(directive(policy, "script-src")).toEqual(
      expect.arrayContaining([
        "'self'",
        "https://maps.googleapis.com",
        "https://www.gstatic.com",
      ]),
    );
    expect(directive(policy, "frame-ancestors")).toEqual(["'none'"]);
  });

  it("is still sent Report-Only, with the OI-10 collector", () => {
    const byKey = Object.fromEntries(
      buildSecurityHeaders({ isDev: false }).map((h) => [h.key, h.value]),
    );
    expect(byKey["Content-Security-Policy"]).toBeUndefined();
    const policy = byKey["Content-Security-Policy-Report-Only"];
    expect(policy).toContain("https://js.stripe.com");
    expect(policy).toContain("report-uri /api/csp-report");
  });
});

describe("buildSecurityHeaders", () => {
  const headers = buildSecurityHeaders({ isDev: false });
  const byKey = Object.fromEntries(headers.map((h) => [h.key, h.value]));

  it("ships the CSP as report-only, not enforcing", () => {
    expect(byKey["Content-Security-Policy-Report-Only"]).toContain(
      "default-src 'self'",
    );
    expect(byKey["Content-Security-Policy"]).toBeUndefined();
  });

  it("stays report-only while collecting reports (OI-10)", () => {
    expect(byKey["Content-Security-Policy-Report-Only"]).toContain(
      "report-uri /api/csp-report",
    );
    expect(byKey["Content-Security-Policy-Report-Only"]).toContain(
      "report-to csp-endpoint",
    );
    expect(Object.keys(byKey)).not.toContain("Content-Security-Policy");
    expect(byKey["Reporting-Endpoints"]).toBe('csp-endpoint="/api/csp-report"');
  });

  it("sends HSTS", () => {
    expect(byKey["Strict-Transport-Security"]).toBe(HSTS_VALUE);
  });

  it("keeps the existing headers unchanged", () => {
    expect(byKey["X-Content-Type-Options"]).toBe("nosniff");
    expect(byKey["X-Frame-Options"]).toBe("DENY");
    expect(byKey["X-XSS-Protection"]).toBe("1; mode=block");
    expect(byKey["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
    expect(byKey["Permissions-Policy"]).toBe(
      "camera=(), microphone=(), geolocation=(self)",
    );
  });
});

describe("next.config headers()", () => {
  it("applies HSTS and the report-only CSP to every route", async () => {
    const rules = await nextConfig.headers?.();
    expect(rules?.[0]?.source).toBe("/:path*");
    const keys = (rules?.[0]?.headers ?? []).map((h) => h.key);
    expect(keys).toContain("Strict-Transport-Security");
    expect(keys).toContain("Content-Security-Policy-Report-Only");
  });
});
