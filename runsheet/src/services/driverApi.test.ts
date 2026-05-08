/**
 * Unit tests for the driver-facing API client.
 *
 * Covers the three endpoints exposed by `driverApi.ts`:
 *
 *  • {@link presignPodUpload} — request a short-lived presigned PUT URL
 *    for a POD artifact. See Requirement 4.1.3.
 *  • {@link putPresignedFile} — upload the raw bytes directly to the
 *    presigned URL. See Requirement 4.1.5.
 *  • {@link submitPOD} — persist the POD record once every artifact is
 *    uploaded. See Requirements 4.1.4, 4.2.4, 4.2.5.
 *
 * Tests are structured so a failing assertion pinpoints the exact
 * contract boundary that drifted from the backend (`pod_endpoints.py`).
 */

import { ApiError } from "./api";
import {
  type DriverPODRequest,
  type PresignResponse,
  presignPodUpload,
  putPresignedFile,
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
      body: { data: fixture, request_id: "req-1" },
    });

    const result = await presignPodUpload("signature", "image/png");

    expect(result).toEqual(fixture);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/driver/pod/uploads/presign`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      category: "signature",
      content_type: "image/png",
    });
  });

  it("raises ApiError with a helpful message when the server returns 400", async () => {
    mockFetchOnce({
      ok: false,
      status: 400,
      body: { detail: "Content-Type not permitted for POD uploads" },
    });

    await expect(
      presignPodUpload("signature", "image/png"),
    ).rejects.toBeInstanceOf(ApiError);
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

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("https://s3.example.com/upload?sig=xyz");
    expect(options.method).toBe("PUT");
    expect(options.body).toBe(file);
    expect(options.headers["Content-Type"]).toBe("image/png");
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
    ).rejects.toBeInstanceOf(ApiError);
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
    ).rejects.toBeInstanceOf(ApiError);
  });
});

// ─── submitPOD ───────────────────────────────────────────────────────────────

describe("submitPOD", () => {
  const payload: DriverPODRequest = {
    recipient_name: "Jane Doe",
    signature_ref: "tenants/t/signature/x.png",
    photo_refs: ["tenants/t/photo/a.png"],
    meter_ticket_ref: "tenants/t/meter_ticket/b.png",
    geotag: { lat: 40.7128, lng: -74.006 },
    timestamp: "2024-01-15T12:00:00Z",
  };

  it("POSTs to /driver/jobs/{job_id}/pod and forwards the Idempotency-Key", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: {
          pod_id: "pod-1",
          job_id: "job-1",
          recipient_name: "Jane Doe",
          signature_ref: payload.signature_ref,
          photo_refs: payload.photo_refs,
          meter_ticket_ref: payload.meter_ticket_ref,
          delivered_gallons: 100.5,
          delivered_gallons_source: "ocr",
          ocr_confidence: 0.95,
          ocr_requires_manual_review: false,
          geotag: { lat: 40.7128, lon: -74.006 },
          timestamp: "2024-01-15T12:00:00Z",
          otp_verified: false,
          location_mismatch: false,
          status: "submitted",
          tenant_id: "t",
        },
        request_id: "req-2",
      },
    });

    const response = await submitPOD("job-1", payload, "idem-1");

    expect(response.data.pod_id).toBe("pod-1");
    expect(response.data.delivered_gallons_source).toBe("ocr");
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/driver/jobs/job-1/pod`);
    expect(options.method).toBe("POST");
    expect(options.headers["Idempotency-Key"]).toBe("idem-1");
    expect(JSON.parse(options.body)).toEqual(payload);
  });

  it("URL-encodes job ids that contain reserved characters", async () => {
    mockFetchOnce({
      ok: true,
      body: { data: { pod_id: "p", job_id: "job/1" }, request_id: "r" },
    });

    await submitPOD("job/1", payload);

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/driver/jobs/job%2F1/pod`);
  });

  it("translates a 403 cross-tenant response into ApiError(status=403)", async () => {
    mockFetchOnce({
      ok: false,
      status: 403,
      body: { detail: "Cross-tenant file_ref denied" },
    });

    await expect(submitPOD("job-1", payload)).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
    });
  });
});
