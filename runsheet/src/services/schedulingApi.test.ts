/**
 * Unit tests for the driver-action and reroute additions to
 * ``schedulingApi.ts``.
 *
 * Covers the endpoints the frontend was missing against the backend
 * scheduling surface:
 *
 *  • ``POST /scheduling/jobs/:id/ack``    — {@link ackJob}
 *  • ``POST /scheduling/jobs/:id/accept`` — {@link acceptJob}
 *  • ``POST /scheduling/jobs/:id/reject`` — {@link rejectJob}
 *  • ``POST /api/v1/scheduling/jobs/:id/reroute`` — {@link rerouteJob}
 *
 * ``rerouteJob`` is exercised separately because it targets the
 * ``/api/v1`` prefix rather than the shared ``/api`` base, so the URL
 * assembly is a distinct contract boundary.
 *
 * We mock ``global.fetch`` to verify URL assembly, HTTP method, and JSON
 * body handling without a real HTTP client.
 */

import { ApiError } from "./api";
import { acceptJob, ackJob, rejectJob, rerouteJob } from "./schedulingApi";

const API_BASE_URL = "http://localhost:8080/api";

function mockFetchOnce(response: {
  ok: boolean;
  status?: number;
  body?: unknown;
}) {
  const jsonBody = response.body ?? {};
  global.fetch = jest.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status ?? (response.ok ? 200 : 500),
    json: async () => jsonBody,
  }) as unknown as typeof fetch;
}

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── ackJob ────────────────────────────────────────────────────────────────

describe("ackJob", () => {
  it("POSTs the device_id and returns the action result", async () => {
    const action = {
      job_id: "job-1",
      action: "ack",
      actor_id: "driver-3",
      timestamp: "2026-01-15T12:00:00Z",
    };
    mockFetchOnce({ ok: true, body: { data: action, request_id: "r" } });

    const result = await ackJob("job-1", "device-9");

    expect(result.data).toEqual(action);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/scheduling/jobs/job-1/ack`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ device_id: "device-9" });
  });

  it("sends device_id: null when no device is supplied", async () => {
    mockFetchOnce({ ok: true, body: { data: {}, request_id: "r" } });

    await ackJob("job-1");

    const [, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(JSON.parse(options.body)).toEqual({ device_id: null });
  });
});

// ─── acceptJob ───────────────────────────────────────────────────────────────

describe("acceptJob", () => {
  it("POSTs an empty body to the accept endpoint", async () => {
    mockFetchOnce({ ok: true, body: { data: {}, request_id: "r" } });

    await acceptJob("job-2");

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/scheduling/jobs/job-2/accept`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({});
  });
});

// ─── rejectJob ───────────────────────────────────────────────────────────────

describe("rejectJob", () => {
  it("POSTs the rejection reason", async () => {
    mockFetchOnce({ ok: true, body: { data: {}, request_id: "r" } });

    await rejectJob("job-3", "vehicle issue");

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/scheduling/jobs/job-3/reject`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ reason: "vehicle issue" });
  });

  it("raises ApiError on a 409 conflict", async () => {
    mockFetchOnce({
      ok: false,
      status: 409,
      body: { detail: "invalid status transition" },
    });

    await expect(rejectJob("job-3", "nope")).rejects.toThrow(ApiError);
  });
});

// ─── rerouteJob ──────────────────────────────────────────────────────────────

describe("rerouteJob", () => {
  it("targets the /api/v1 prefix (not the shared /api base)", async () => {
    const job = { job_id: "job-4", destination: "New Depot" };
    mockFetchOnce({ ok: true, body: { data: job, request_id: "r" } });

    const result = await rerouteJob("job-4", {
      new_destination: "New Depot",
      reason: "customer moved",
    });

    expect(result.data).toEqual(job);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(
      "http://localhost:8080/api/v1/scheduling/jobs/job-4/reroute",
    );
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      new_destination: "New Depot",
      reason: "customer moved",
    });
  });

  it("raises ApiError when the reroute is rejected", async () => {
    mockFetchOnce({
      ok: false,
      status: 422,
      body: { detail: "job not in flight" },
    });

    await expect(rerouteJob("job-4", { new_destination: "X" })).rejects.toThrow(
      ApiError,
    );
  });
});
