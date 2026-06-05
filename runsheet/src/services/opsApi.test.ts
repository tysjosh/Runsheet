/**
 * Unit tests for the rider/event/replay/drift/feature-flag and Prometheus
 * additions to ``opsApi.ts``.
 *
 * Covers the ops-intelligence endpoints the frontend was missing:
 *
 *  • ``GET /ops/riders`` / ``GET /ops/riders/:id``
 *  • ``GET /ops/events``
 *  • ``GET /ops/metrics/riders``
 *  • ``GET /ops/metrics/prometheus`` (text, not JSON)
 *  • ``POST /ops/replay/trigger`` / ``GET /ops/replay/status/:id``
 *  • ``POST /ops/drift/run``
 *  • feature-flag enable / disable / rollback
 *
 * We mock ``global.fetch`` to verify URL assembly, HTTP method, and JSON
 * body handling. The Prometheus endpoint is checked separately because it
 * reads ``text()`` rather than ``json()``.
 */

import { ApiError } from "./api";
import {
  disableOpsFeatureFlag,
  enableOpsFeatureFlag,
  getEvents,
  getPrometheusMetrics,
  getReplayStatus,
  getRiderById,
  getRiderMetrics,
  getRiders,
  rollbackOpsFeatureFlag,
  runDriftDetection,
  triggerReplay,
} from "./opsApi";

const API_BASE_URL = "http://localhost:8000/api";

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

// ─── Riders ──────────────────────────────────────────────────────────────────

describe("getRiders", () => {
  it("serializes status/page/size filters", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: [],
        pagination: { page: 1, size: 20, total: 0, total_pages: 1 },
        request_id: "r",
      },
    });

    await getRiders({ status: "active", page: 2, size: 10 });

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toContain(`${API_BASE_URL}/ops/riders?`);
    expect(url).toContain("status=active");
    expect(url).toContain("page=2");
    expect(url).toContain("size=10");
  });
});

describe("getRiderById", () => {
  it("GETs the rider and returns assigned shipments", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: { rider_id: "rider-1", assigned_shipments: [] },
        request_id: "r",
      },
    });

    const result = await getRiderById("rider-1");

    expect(result.data.rider_id).toBe("rider-1");
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/riders/rider-1`);
  });
});

// ─── Events ────────────────────────────────────────────────────────────────

describe("getEvents", () => {
  it("serializes shipment_id and event_type filters", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: [],
        pagination: { page: 1, size: 20, total: 0, total_pages: 1 },
        request_id: "r",
      },
    });

    await getEvents({ shipment_id: "ship-1", event_type: "delivered" });

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toContain("shipment_id=ship-1");
    expect(url).toContain("event_type=delivered");
  });
});

// ─── Rider metrics ───────────────────────────────────────────────────────────

describe("getRiderMetrics", () => {
  it("GETs the rider metrics endpoint with bucket filter", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: [],
        bucket: "daily",
        start_date: "",
        end_date: "",
        request_id: "r",
      },
    });

    await getRiderMetrics({ bucket: "daily" });

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/metrics/riders?bucket=daily`);
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

// ─── Replay ────────────────────────────────────────────────────────────────

describe("replay", () => {
  it("triggerReplay POSTs the tenant + time range", async () => {
    mockFetchOnce({
      ok: true,
      body: { data: { job_id: "job-1", status: "running" }, request_id: "r" },
    });

    const payload = {
      tenant_id: "tenant-a",
      start_time: "2026-01-01T00:00:00Z",
      end_time: "2026-01-02T00:00:00Z",
    };
    const result = await triggerReplay(payload);

    expect(result.data.job_id).toBe("job-1");
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/replay/trigger`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual(payload);
  });

  it("getReplayStatus GETs the job by id", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: { job_id: "job-1", status: "completed" },
        request_id: "r",
      },
    });

    await getReplayStatus("job-1");

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/replay/status/job-1`);
  });
});

// ─── Drift ─────────────────────────────────────────────────────────────────

describe("runDriftDetection", () => {
  it("POSTs the drift run payload", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: { tenant_id: "tenant-a", drift_percentage: 0 },
        request_id: "r",
      },
    });

    await runDriftDetection({ tenant_id: "tenant-a" });

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/drift/run`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ tenant_id: "tenant-a" });
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
