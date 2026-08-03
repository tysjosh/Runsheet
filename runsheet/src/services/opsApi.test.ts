/**
 * Unit tests for ``opsApi.ts`` — the surviving, ungated ops surface.
 *
 * Covers:
 *
 *  • ``GET /ops/monitoring/{ingestion,indexing,poison-queue}``
 *  • ``GET /ops/metrics/prometheus`` (text, not JSON)
 *  • feature-flag enable / disable / rollback
 *
 * The rider / event / rider-metrics / replay / drift wrappers that used to be
 * tested here were deleted with the rest of the legacy-NG frontend: every one
 * of those routes is behind `require_ops_enabled`, which raises
 * `LEGACY_NG_DELIVERY_DISABLED` while `LEGACY_NG_DELIVERY_ENABLED` is false
 * (the default everywhere). The endpoints exercised below are deliberately
 * exempt from that gate so a disabled surface stays observable and
 * manageable.
 *
 * We mock ``global.fetch`` to verify URL assembly, HTTP method, and JSON
 * body handling. The Prometheus endpoint is checked separately because it
 * reads ``text()`` rather than ``json()``.
 */

import { ApiError } from "./api";
import {
  disableOpsFeatureFlag,
  enableOpsFeatureFlag,
  getIndexingMonitoring,
  getIngestionMonitoring,
  getPoisonQueueMonitoring,
  getPrometheusMetrics,
  rollbackOpsFeatureFlag,
} from "./opsApi";

const API_BASE_URL = "http://localhost:8080/api";

function mockFetchOnce(response: {
  ok: boolean;
  status?: number;
  body?: unknown;
  text?: string;
}) {
  const jsonBody = response.body ?? {};
  global.fetch = jest.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status ?? (response.ok ? 200 : 500),
    json: async () => jsonBody,
    text: async () => response.text ?? "",
  }) as unknown as typeof fetch;
}

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── Monitoring ──────────────────────────────────────────────────────────────

describe("monitoring endpoints", () => {
  it("getIngestionMonitoring GETs the ingestion path", async () => {
    mockFetchOnce({ ok: true, body: { events_received: 5, request_id: "r" } });

    const result = await getIngestionMonitoring();

    expect(result.events_received).toBe(5);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/monitoring/ingestion`);
  });

  it("getIndexingMonitoring GETs the indexing path", async () => {
    mockFetchOnce({
      ok: true,
      body: { documents_indexed: 12, request_id: "r" },
    });

    const result = await getIndexingMonitoring();

    expect(result.documents_indexed).toBe(12);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/monitoring/indexing`);
  });

  it("getPoisonQueueMonitoring GETs the poison-queue path", async () => {
    mockFetchOnce({ ok: true, body: { queue_depth: 0, request_id: "r" } });

    const result = await getPoisonQueueMonitoring();

    expect(result.queue_depth).toBe(0);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/monitoring/poison-queue`);
  });

  it("surfaces a non-2xx monitoring response as ApiError", async () => {
    mockFetchOnce({ ok: false, status: 503, body: { message: "down" } });

    await expect(getIngestionMonitoring()).rejects.toThrow(ApiError);
  });
});

// ─── Prometheus ──────────────────────────────────────────────────────────────

describe("getPrometheusMetrics", () => {
  it("returns the raw text exposition body", async () => {
    mockFetchOnce({ ok: true, text: "# HELP foo\nfoo 1\n" });

    const result = await getPrometheusMetrics();

    expect(result).toBe("# HELP foo\nfoo 1\n");
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/metrics/prometheus`);
  });

  it("raises ApiError on a non-2xx response", async () => {
    mockFetchOnce({ ok: false, status: 503, text: "unavailable" });

    await expect(getPrometheusMetrics()).rejects.toThrow(ApiError);
  });
});

// ─── Feature flags ────────────────────────────────────────────────────────────

describe("feature flag admin", () => {
  it("enableOpsFeatureFlag POSTs to the enable path", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: { tenant_id: "tenant-a", status: "enabled" },
        request_id: "r",
      },
    });

    await enableOpsFeatureFlag("tenant-a");

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/admin/feature-flags/tenant-a/enable`);
    expect(options.method).toBe("POST");
  });

  it("disableOpsFeatureFlag POSTs to the disable path", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: { tenant_id: "tenant-a", status: "disabled" },
        request_id: "r",
      },
    });

    await disableOpsFeatureFlag("tenant-a");

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(
      `${API_BASE_URL}/ops/admin/feature-flags/tenant-a/disable`,
    );
    expect(options.method).toBe("POST");
  });

  it("rollbackOpsFeatureFlag appends purge_data as a query param", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: { tenant_id: "tenant-a", status: "rolled_back" },
        request_id: "r",
      },
    });

    await rollbackOpsFeatureFlag("tenant-a", true);

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(
      `${API_BASE_URL}/ops/admin/feature-flags/tenant-a/rollback?purge_data=true`,
    );
    expect(options.method).toBe("POST");
  });
});
