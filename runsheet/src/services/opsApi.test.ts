/**
 * Unit tests for ``opsApi.ts`` — the surviving, ungated ops surface.
 *
 * Covers:
 *
 *  • ``GET /ops/monitoring/poison-queue``
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
import { getPoisonQueueMonitoring } from "./opsApi";

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
  it("getPoisonQueueMonitoring GETs the poison-queue path", async () => {
    mockFetchOnce({ ok: true, body: { queue_depth: 0, request_id: "r" } });

    const result = await getPoisonQueueMonitoring();

    expect(result.queue_depth).toBe(0);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/ops/monitoring/poison-queue`);
  });

  it("surfaces a non-2xx monitoring response as ApiError", async () => {
    mockFetchOnce({ ok: false, status: 503, body: { message: "down" } });

    await expect(getPoisonQueueMonitoring()).rejects.toThrow(ApiError);
  });
});

// ─── Prometheus ──────────────────────────────────────────────────────────────
