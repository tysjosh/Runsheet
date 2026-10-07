/**
 * @jest-environment node
 *
 * Tests for POST /api/csp-report (OI-10).
 *
 * The node environment is needed for the real `Request`/`Response` globals.
 * The negative assertions matter most: this endpoint is public, so a report
 * must never echo a query string, a script sample or the client address into
 * the log, and oversized, mistyped or flooding requests must be refused.
 */
import {
  extractReports,
  PER_KEY_LIMIT,
  RATE_WINDOW_MS,
  rateLimiter,
  reduceBlocked,
  sanitizeReport,
} from "./report";
import { POST } from "./route";

const URL_ = "https://app.staging.runsheetops.com/api/csp-report";
const CLIENT_IP = "203.0.113.77";

function post(
  body: string,
  contentType = "application/csp-report",
  ip = CLIENT_IP,
): Request {
  return new Request(URL_, {
    method: "POST",
    headers: {
      "Content-Type": contentType,
      "X-Forwarded-For": `${ip}, 10.0.0.1`,
    },
    body,
  });
}

const LEGACY = {
  "csp-report": {
    "document-uri":
      "https://app.staging.runsheetops.com/dashboard/orders?customer=jane%40example.com#x",
    referrer:
      "https://app.staging.runsheetops.com/signin?email=jane@example.com",
    "violated-directive": "script-src-elem",
    "effective-directive": "script-src-elem",
    "original-policy": "default-src 'self'; report-uri /api/csp-report",
    disposition: "report",
    "blocked-uri": "https://evil.example.net/x.js?token=secret",
    "line-number": 12,
    "column-number": 4,
    "source-file":
      "https://app.staging.runsheetops.com/_next/static/chunk.js?v=1",
    "status-code": 200,
    "script-sample": "alert('jane@example.com')",
  },
};

let warn: jest.SpyInstance;
let clock = 0;

beforeEach(() => {
  clock = 1_000_000;
  rateLimiter.reset();
  rateLimiter.setClock(() => clock);
  warn = jest.spyOn(console, "warn").mockImplementation(() => undefined);
});

afterEach(() => {
  warn.mockRestore();
  rateLimiter.setClock(Date.now);
  rateLimiter.reset();
});

function violationLines(): Array<Record<string, unknown>> {
  return warn.mock.calls
    .filter((call) => call[0] === "csp_violation")
    .map((call) => JSON.parse(call[1] as string));
}

it("logs a legacy report with only the allowlisted, reduced fields", async () => {
  const res = await POST(post(JSON.stringify(LEGACY)));
  expect(res.status).toBe(204);
  const lines = violationLines();
  expect(lines).toEqual([
    {
      directive: "script-src-elem",
      disposition: "report",
      blocked: "https://evil.example.net",
      document: "https://app.staging.runsheetops.com/dashboard/orders",
      source: "https://app.staging.runsheetops.com/_next/static/chunk.js",
      line: 12,
      column: 4,
      status: 200,
    },
  ]);
  const logged = JSON.stringify(warn.mock.calls);
  for (const leaked of [
    "?",
    "jane",
    "secret",
    "alert",
    CLIENT_IP,
    "original-policy",
  ]) {
    expect(logged).not.toContain(leaked);
  }
});

it("logs one line per Reporting API report", async () => {
  const body = JSON.stringify([
    {
      type: "csp-violation",
      url: "https://app.staging.runsheetops.com/dashboard",
      body: {
        documentURL: "https://app.staging.runsheetops.com/dashboard?q=1",
        effectiveDirective: "connect-src",
        blockedURL: "wss://other.example.org/ws",
        disposition: "report",
        sample: "secret-sample",
      },
    },
    {
      type: "csp-violation",
      body: { effectiveDirective: "script-src", blockedURL: "inline" },
    },
    { type: "deprecation", body: { id: "x" } },
  ]);
  const res = await POST(post(body, "application/reports+json"));
  expect(res.status).toBe(204);
  expect(violationLines()).toEqual([
    {
      directive: "connect-src",
      disposition: "report",
      blocked: "wss://other.example.org",
      document: "https://app.staging.runsheetops.com/dashboard",
    },
    { directive: "script-src", blocked: "inline" },
  ]);
  expect(JSON.stringify(warn.mock.calls)).not.toContain("secret-sample");
});

it("refuses an unexpected content type with 415", async () => {
  const res = await POST(post(JSON.stringify(LEGACY), "text/plain"));
  expect(res.status).toBe(415);
  expect(violationLines()).toEqual([]);
});

it("refuses a 20 KiB body with 413", async () => {
  const big = JSON.stringify({
    "csp-report": {
      "violated-directive": "img-src",
      pad: "x".repeat(20 * 1024),
    },
  });
  const res = await POST(post(big));
  expect(res.status).toBe(413);
  expect(violationLines()).toEqual([]);
});

it("refuses an oversized body even when content-length is absent", async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("x".repeat(20 * 1024)));
      controller.close();
    },
  });
  const req = new Request(URL_, {
    method: "POST",
    headers: { "Content-Type": "application/csp-report" },
    body: stream,
    // @ts-expect-error -- Node's fetch needs duplex for a stream body.
    duplex: "half",
  });
  expect(req.headers.get("content-length")).toBeNull();
  const res = await POST(req);
  expect(res.status).toBe(413);
});

it("rate-limits one client to 30 requests a minute", async () => {
  const body = JSON.stringify(LEGACY);
  for (let i = 0; i < PER_KEY_LIMIT; i += 1) {
    expect((await POST(post(body))).status).toBe(204);
  }
  expect((await POST(post(body))).status).toBe(429);
  // Another client is unaffected.
  expect(
    (await POST(post(body, "application/csp-report", "198.51.100.1"))).status,
  ).toBe(204);
  // One throttle line per window, and it carries no client address.
  const throttled = warn.mock.calls.filter(
    (c) => c[0] === "csp_report_throttled",
  );
  expect(throttled).toHaveLength(1);
  expect(JSON.stringify(throttled)).not.toContain(CLIENT_IP);
  // The next window starts fresh.
  clock += RATE_WINDOW_MS;
  expect((await POST(post(body))).status).toBe(204);
});

it("answers malformed JSON with 204 and logs nothing", async () => {
  const res = await POST(post("{not json"));
  expect(res.status).toBe(204);
  expect(warn).not.toHaveBeenCalled();
});

it("ignores reports beyond 20 in one request", () => {
  const many = Array.from({ length: 25 }, () => ({
    type: "csp-violation",
    body: { effectiveDirective: "img-src" },
  }));
  expect(extractReports(JSON.stringify(many))).toHaveLength(20);
});

it("reduces blocked URLs to an origin or keyword", () => {
  expect(reduceBlocked("data:image/png;base64,AAAA")).toBe("data");
  expect(reduceBlocked("eval")).toBe("eval");
  expect(reduceBlocked("chrome-extension://abc/x.js")).toBe("other");
  expect(
    sanitizeReport({
      "violated-directive": "style-src 'self' 'unsafe-inline'",
    }),
  ).toEqual({
    directive: "style-src",
  });
});
