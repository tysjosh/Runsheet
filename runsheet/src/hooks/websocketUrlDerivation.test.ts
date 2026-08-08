/**
 * Regression guard for how every WebSocket hook derives its URL from
 * ``NEXT_PUBLIC_API_URL``.
 *
 * The deployed backend origin is ``https://api.runsheetops.com/api`` — a host that
 * itself begins with "api". That is what makes this worth a test: an unanchored
 * ``.replace("/api", "")`` matches the "/api" inside "//api.runsheetops.com" before
 * it ever reaches the trailing path, yielding ``wss:/.runsheetops.com/api``. The
 * URL parses far enough to look plausible and then fails at connect time, on one
 * socket only, while every other socket on the page works.
 *
 * Each module computes its constant at IMPORT time from ``process.env``, so every
 * case sets the variable and re-imports in isolation.
 */

const PROD_API = "https://api.runsheetops.com/api";
const EXPECTED_HOST = "api.runsheetops.com";

/** Import a module fresh with NEXT_PUBLIC_API_URL set, and return the export. */
async function withApiUrl<T>(
  apiUrl: string,
  load: () => Promise<T>,
): Promise<T> {
  const previous = process.env.NEXT_PUBLIC_API_URL;
  process.env.NEXT_PUBLIC_API_URL = apiUrl;
  jest.resetModules();
  try {
    return await load();
  } finally {
    process.env.NEXT_PUBLIC_API_URL = previous;
  }
}

/**
 * Assert a derived socket URL is a usable wss:// URL on the real host.
 *
 * Checking ``hostname`` matters as much as the protocol: the broken form kept the
 * ``wss:`` scheme and only corrupted the authority, so a protocol-only assertion
 * would have passed on the bug this test exists to catch.
 */
function expectUsableSocketUrl(url: string): void {
  const parsed = new URL(url);
  expect(parsed.protocol).toBe("wss:");
  expect(parsed.hostname).toBe(EXPECTED_HOST);
  expect(url).not.toContain("//api.runsheetops.com/api/ws");
}

describe("WebSocket URL derivation against an api-prefixed https origin", () => {
  it("derives wss://api.runsheetops.com for plan execution", async () => {
    // Reads the constant the module actually computes. An earlier draft fell back
    // to recomputing the expression when the export was missing, which made the
    // test pass regardless of what the source did — vacuous exactly where it
    // mattered most.
    const url = await withApiUrl(PROD_API, async () => {
      const mod = await import("./usePlanExecutionSocket");
      return mod.PLAN_EXECUTION_WS_BASE_URL;
    });
    expect(url).toBe("wss://api.runsheetops.com/ws/plan-execution");
    expectUsableSocketUrl(url);
  });

  it("agrees across every derivation style used in the app", () => {
    const anchored = PROD_API.replace(/\/api$/, "").replace("http", "ws");
    const keepsApiPath = PROD_API.replace("http", "ws");

    // The shape used by ops/scheduling/orders/agent/inventory/notification and,
    // after the fix, plan-execution.
    expectUsableSocketUrl(`${anchored}/ws/ops`);
    // services/api.ts keeps the /api prefix for /fleet/live, which is correct
    // because that route is mounted under /api on the backend.
    expectUsableSocketUrl(`${keepsApiPath}/fleet/live`);
  });

  it("rejects the unanchored strip that broke plan execution", () => {
    // The exact expression that shipped. Kept as an explicit negative so nobody
    // reintroduces it believing it is equivalent to the anchored form.
    const broken = PROD_API.replace("http", "ws").replace("/api", "");
    expect(broken).toBe("wss:/.runsheetops.com/api");
    expect(new URL(broken).hostname).not.toBe(EXPECTED_HOST);
  });

  it("still works for a host that does not begin with api", () => {
    const alb =
      "http://runsheet-staging-alb-1966675674.us-east-2.elb.amazonaws.com/api";
    const anchored = alb.replace(/\/api$/, "").replace("http", "ws");
    const parsed = new URL(`${anchored}/ws/ops`);
    expect(parsed.protocol).toBe("ws:");
    expect(parsed.hostname).toBe(
      "runsheet-staging-alb-1966675674.us-east-2.elb.amazonaws.com",
    );
  });
});
