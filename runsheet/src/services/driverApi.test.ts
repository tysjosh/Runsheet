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
  type PresignResponse,
  presignPodUpload,
  putPresignedFile,
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
