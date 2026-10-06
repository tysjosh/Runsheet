/**
 * Unit tests for the CSV export client (`exportApi.ts`).
 *
 * `global.fetch` is mocked so URL assembly, credentials, the Blob download
 * and error mapping are checked without a real HTTP client.
 */
import { ApiError } from "./api";
import {
  downloadCsvExport,
  EXPORT_PATHS,
  type ExportType,
  filenameFromContentDisposition,
} from "./exportApi";

// A 401/403 triggers one session refresh and a retry; refresh succeeds here,
// so a role 403 is returned again by the retry and surfaces to the caller.
jest.mock("supertokens-auth-react/recipe/session", () => ({
  __esModule: true,
  default: { attemptRefreshingSession: jest.fn(async () => true) },
}));

const API_BASE_URL = "http://localhost:8080/api";

interface MockResponseInit {
  ok: boolean;
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
  blob?: () => Promise<Blob>;
}

function mockFetchOnce(init: MockResponseInit): jest.Mock {
  const headers = new Map(
    Object.entries(init.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]),
  );
  const fn = jest.fn().mockResolvedValue({
    ok: init.ok,
    status: init.status ?? (init.ok ? 200 : 500),
    headers: { get: (name: string) => headers.get(name.toLowerCase()) ?? null },
    json: async () => init.body ?? {},
    blob: init.blob ?? (async () => new Blob(["a,b\r\n"])),
  });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

let createObjectURL: jest.Mock;
let revokeObjectURL: jest.Mock;
let clickSpy: jest.SpyInstance;
let downloads: string[];

beforeEach(() => {
  downloads = [];
  createObjectURL = jest.fn(() => "blob:mock");
  revokeObjectURL = jest.fn();
  Object.assign(URL, { createObjectURL, revokeObjectURL });
  clickSpy = jest
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push(this.download);
    });
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe("downloadCsvExport", () => {
  it.each(Object.entries(EXPORT_PATHS) as [ExportType, string][])(
    "requests %s at its export path",
    async (type, path) => {
      const fetchMock = mockFetchOnce({ ok: true });
      await downloadCsvExport(type, {});
      expect(fetchMock.mock.calls[0][0]).toBe(`${API_BASE_URL}${path}`);
    },
  );

  it("drops empty params and sends credentials and Accept: text/csv", async () => {
    const fetchMock = mockFetchOnce({ ok: true });
    await downloadCsvExport("orders", {
      status: "placed",
      customer_id: "",
      driver_id: undefined,
      q: null,
      min: 0,
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/orders/export?status=placed&min=0`);
    expect(init.credentials).toBe("include");
    expect(init.method).toBe("GET");
    expect(init.headers).toEqual(
      expect.objectContaining({ Accept: "text/csv" }),
    );
  });

  it("saves the Blob under the server filename", async () => {
    mockFetchOnce({
      ok: true,
      headers: {
        "Content-Disposition":
          'attachment; filename="orders_demo-tenant_20261004.csv"',
      },
    });
    const result = await downloadCsvExport("orders", {});
    expect(result.filename).toBe("orders_demo-tenant_20261004.csv");
    expect(downloads).toEqual(["orders_demo-tenant_20261004.csv"]);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock");
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(document.querySelectorAll("a[download]")).toHaveLength(0);
  });

  it("falls back to <type>_export.csv without the header", async () => {
    mockFetchOnce({ ok: true });
    const result = await downloadCsvExport("jobs", {});
    expect(result.filename).toBe("jobs_export.csv");
  });

  it.each([413, 429, 403, 500])(
    "throws ApiError with status %i",
    async (status) => {
      mockFetchOnce({
        ok: false,
        status,
        body: { error_code: "X", message: "nope" },
      });
      await expect(downloadCsvExport("invoices", {})).rejects.toMatchObject({
        name: "ApiError",
        status,
      });
      expect(clickSpy).not.toHaveBeenCalled();
    },
  );

  it("maps a body read that fails after headers to ApiError status 0", async () => {
    mockFetchOnce({
      ok: true,
      blob: async () => {
        throw new TypeError("network error");
      },
    });
    const error = await downloadCsvExport("orders", {}).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(clickSpy).not.toHaveBeenCalled();
  });

  it("maps a network failure to ApiError status 0", async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValueOnce(
        new TypeError("Failed to fetch"),
      ) as unknown as typeof fetch;
    const error = await downloadCsvExport("orders", {}).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
  });
});

describe("filenameFromContentDisposition", () => {
  it("reads quoted and unquoted filenames", () => {
    expect(
      filenameFromContentDisposition(
        'attachment; filename="a_b_20261004.csv"',
        "x",
      ),
    ).toBe("a_b_20261004.csv");
    expect(
      filenameFromContentDisposition("attachment; filename=a.csv", "x"),
    ).toBe("a.csv");
  });

  it("falls back when absent", () => {
    expect(filenameFromContentDisposition(null, "fallback.csv")).toBe(
      "fallback.csv",
    );
    expect(filenameFromContentDisposition("attachment", "fallback.csv")).toBe(
      "fallback.csv",
    );
  });
});
