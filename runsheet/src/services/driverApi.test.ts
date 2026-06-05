/**
 * Unit tests for the upload helpers in `driverApi.ts`.
 *
 * POD submission moved to the native driver app; the web dispatcher
 * UI only uses the two presigned-upload primitives below. Tests stay
 * here (rather than moving to a generic `uploadsApi.test.ts`) because
 * the helpers still target the `/api/driver` router and share the POD
 * allowlist for category / content-type.
 *
 *  • {@link presignPodUpload} — request a short-lived presigned PUT URL
 *    for a POD artifact. See Requirement 4.1.3.
 *  • {@link putPresignedFile} — upload the raw bytes directly to the
 *    presigned URL. See Requirement 4.1.5.
 *
 * Tests are structured so a failing assertion pinpoints the exact
 * contract boundary that drifted from the backend (`pod_endpoints.py`).
 */

import { ApiError } from "./api";
import {
  listJobMessages,
  type PresignResponse,
  presignPodUpload,
  putPresignedFile,
  type ReportExceptionPayload,
  reportException,
  type SendMessagePayload,
  type SubmitPODPayload,
  sendJobMessage,
  submitPOD,
} from "./driverApi";

// ─── Test fixtures ───────────────────────────────────────────────────────────

const API_BASE_URL = "http://localhost:8000/api";

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

function mockFetchReject(error: Error) {
  global.fetch = jest
    .fn()
    .mockRejectedValueOnce(error) as unknown as typeof fetch;
}

function presignFixture(
  overrides: Partial<PresignResponse> = {},
): PresignResponse {
  return {
    file_ref: "tenants/tenant-a/signature/2024/01/15/abc.png",
    upload_url: "https://s3.example.com/upload?sig=xyz",
    expires_at: "2024-01-15T12:15:00Z",
    content_type: "image/png",
    max_file_bytes: 10 * 1024 * 1024,
    ...overrides,
  };
}

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── presignPodUpload ────────────────────────────────────────────────────────

describe("presignPodUpload", () => {
  it("unwraps the `data` envelope and returns the inner presign payload", async () => {
    const fixture = presignFixture();
    mockFetchOnce({
      ok: true,
      body: { data: fixture, request_id: "req-123" },
    });

    const result = await presignPodUpload("signature", "image/png");

    expect(result).toEqual(fixture);
    expect(global.fetch).toHaveBeenCalledWith(
      `${API_BASE_URL}/driver/pod/uploads/presign`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          category: "signature",
          content_type: "image/png",
        }),
      }),
    );
  });

  it("raises ApiError with a helpful message when the server returns 400", async () => {
    mockFetchOnce({
      ok: false,
      status: 400,
      body: { detail: "unsupported_content_type" },
    });

    await expect(presignPodUpload("photo", "image/jpeg")).rejects.toThrow(
      ApiError,
    );
  });
});

// ─── putPresignedFile ────────────────────────────────────────────────────────

describe("putPresignedFile", () => {
  it("sends the file as a PUT with the Content-Type matching the presign", async () => {
    mockFetchOnce({ ok: true });
    const file = new Blob(["hello"], { type: "image/png" });

    await putPresignedFile(
      "https://s3.example.com/upload?sig=xyz",
      file,
      "image/png",
    );

    expect(global.fetch).toHaveBeenCalledWith(
      "https://s3.example.com/upload?sig=xyz",
      expect.objectContaining({
        method: "PUT",
        body: file,
        headers: expect.objectContaining({ "Content-Type": "image/png" }),
      }),
    );
  });

  it("raises ApiError when S3 returns a non-2xx status", async () => {
    mockFetchOnce({ ok: false, status: 403 });
    const file = new Blob(["hello"], { type: "image/png" });

    await expect(
      putPresignedFile(
        "https://s3.example.com/upload?sig=xyz",
        file,
        "image/png",
      ),
    ).rejects.toThrow(ApiError);
  });

  it("wraps an underlying network error in ApiError", async () => {
    mockFetchReject(new TypeError("network down"));
    const file = new Blob(["hello"], { type: "image/png" });

    await expect(
      putPresignedFile(
        "https://s3.example.com/upload?sig=xyz",
        file,
        "image/png",
      ),
    ).rejects.toThrow(ApiError);
  });
});

// ─── reportException ───────────────────────────────────────────────────────

describe("reportException", () => {
  it("POSTs the exception payload to the job-scoped endpoint", async () => {
    const exceptionDoc = {
      exception_id: "exc-1",
      job_id: "job-7",
      exception_type: "road_closure",
      severity: "high",
      note: "Bridge out on Route 9",
      location: { lat: 30.1, lng: -97.7 },
      media_refs: [],
      tenant_id: "tenant-a",
      timestamp: "2026-01-15T12:00:00Z",
    };
    mockFetchOnce({
      ok: true,
      body: { data: exceptionDoc, request_id: "req-1" },
    });

    const payload: ReportExceptionPayload = {
      exception_type: "road_closure",
      severity: "high",
      note: "Bridge out on Route 9",
      location: { lat: 30.1, lng: -97.7 },
    };
    const result = await reportException("job-7", payload);

    expect(result.data).toEqual(exceptionDoc);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/driver/jobs/job-7/exceptions`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual(payload);
  });

  it("raises ApiError when the driver is not assigned (403)", async () => {
    mockFetchOnce({
      ok: false,
      status: 403,
      body: { detail: "Assignment revoked" },
    });

    await expect(
      reportException("job-7", {
        exception_type: "other",
        severity: "low",
        note: "n/a",
      }),
    ).rejects.toThrow(ApiError);
  });
});

// ─── sendJobMessage / listJobMessages ──────────────────────────────────────

describe("job messaging", () => {
  it("sendJobMessage POSTs the message body to the thread endpoint", async () => {
    const messageDoc = {
      message_id: "msg-1",
      job_id: "job-7",
      sender_id: "driver-3",
      sender_role: "driver",
      body: "On my way",
      timestamp: "2026-01-15T12:01:00Z",
      tenant_id: "tenant-a",
    };
    mockFetchOnce({ ok: true, body: { data: messageDoc, request_id: "r" } });

    const payload: SendMessagePayload = {
      body: "On my way",
      sender_id: "driver-3",
      sender_role: "driver",
    };
    const result = await sendJobMessage("job-7", payload);

    expect(result.data).toEqual(messageDoc);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/driver/jobs/job-7/messages`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual(payload);
  });

  it("listJobMessages serializes page/size and reads the paginated envelope", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: [],
        pagination: { page: 2, size: 25, total: 0, total_pages: 1 },
        request_id: "r",
      },
    });

    const result = await listJobMessages("job-7", { page: 2, size: 25 });

    expect(result.pagination.page).toBe(2);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(
      `${API_BASE_URL}/driver/jobs/job-7/messages?page=2&size=25`,
    );
  });

  it("listJobMessages omits the query string when no params are supplied", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: [],
        pagination: { page: 1, size: 50, total: 0, total_pages: 1 },
        request_id: "r",
      },
    });

    await listJobMessages("job-7");

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/driver/jobs/job-7/messages`);
  });
});

// ─── submitPOD ─────────────────────────────────────────────────────────────

describe("submitPOD", () => {
  it("POSTs the POD payload with file_refs to the job endpoint", async () => {
    const podDoc = {
      pod_id: "pod-1",
      job_id: "job-7",
      recipient_name: "Jane Doe",
      geotag: { lat: 30.1, lon: -97.7 },
      timestamp: "2026-01-15T12:05:00Z",
      status: "submitted",
      refused_delivery: false,
      tenant_id: "tenant-a",
    };
    mockFetchOnce({ ok: true, body: { data: podDoc, request_id: "r" } });

    const payload: SubmitPODPayload = {
      recipient_name: "Jane Doe",
      signature_ref: "tenants/tenant-a/signature/abc.png",
      geotag: { lat: 30.1, lng: -97.7 },
      timestamp: "2026-01-15T12:05:00Z",
    };
    const result = await submitPOD("job-7", payload);

    expect(result.data.pod_id).toBe("pod-1");
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/driver/jobs/job-7/pod`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual(payload);
  });
});
