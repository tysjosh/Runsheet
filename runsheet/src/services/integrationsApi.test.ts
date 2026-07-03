/**
 * Unit tests for the Integration Marketplace API client.
 *
 * Focuses on the pieces the Marketplace UI leans on most heavily:
 *
 *   - URL assembly for each endpoint (correct path + method).
 *   - Error envelope translation (the backend returns
 *     ``{detail: {error_code, message}}``; the client flattens that to
 *     an :class:`ApiError.message`).
 *   - Status derivation (``deriveMarketplaceStatus`` encodes Req 5.6.1
 *     — four visible states plus a "pending" transient).
 *   - Sync-run limit clamping (matches the 50-ceiling enforced by the
 *     backend).
 *
 * Validates: Requirements 5.6.1, 5.6.4, 5.6.5.
 */

import { ApiError } from "./api";
import {
  createIntegrationInstance,
  deleteIntegrationInstance,
  deriveMarketplaceStatus,
  disableIntegrationInstance,
  enableIntegrationInstance,
  findInstanceForProvider,
  getStripePublicConfig,
  type IntegrationInstance,
  listIntegrationInstances,
  listIntegrationProviders,
  listSyncRuns,
  type SyncRun,
  syncIntegrationNow,
  updateIntegrationInstance,
} from "./integrationsApi";

const API_BASE_URL = "http://localhost:8080/api";

// ─── Helpers ─────────────────────────────────────────────────────────────────

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

function instanceFixture(
  overrides: Partial<IntegrationInstance> = {},
): IntegrationInstance {
  return {
    instance_id: "inst-1",
    tenant_id: "tenant-a",
    provider_name: "quickbooks_online",
    category: "accounting",
    status: "connected",
    enabled: true,
    credentials_ref: "cred:tenant-a:qbo:abc",
    credentials_status: "valid",
    schedule_cron: "0 */1 * * *",
    config: {},
    last_sync_at: "2024-01-15T12:00:00Z",
    last_error: null,
    retry_count: 0,
    updated_at: "2024-01-15T12:00:00Z",
    created_at: "2024-01-10T09:00:00Z",
    ...overrides,
  };
}

function syncRunFixture(overrides: Partial<SyncRun> = {}): SyncRun {
  return {
    run_id: "run-1",
    tenant_id: "tenant-a",
    instance_id: "inst-1",
    provider_name: "quickbooks_online",
    operation: "pull",
    started_at: "2024-01-15T12:00:00Z",
    finished_at: "2024-01-15T12:00:05Z",
    status: "success",
    record_counts: { invoices: 4, payments: 2 },
    error_details: null,
    duration_ms: 5000,
    ...overrides,
  };
}

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── listIntegrationProviders ────────────────────────────────────────────────

describe("listIntegrationProviders", () => {
  it("fetches the catalog endpoint and returns the parsed envelope", async () => {
    const envelope = {
      items: [
        {
          provider_name: "quickbooks_online",
          category: "accounting",
          description: "QuickBooks Online.",
          required_credential_fields: ["client_id", "client_secret"],
          doc_url: "https://example.com",
          auth_mode: "oauth2",
          feature_flag_key: null,
          effective_feature_flag_key: "overlay.integration.quickbooks_online",
        },
      ],
      total: 1,
    };
    mockFetchOnce({ ok: true, body: envelope });

    const result = await listIntegrationProviders();

    expect(result).toEqual(envelope);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/integrations/providers`);
  });
});

// ─── listIntegrationInstances ────────────────────────────────────────────────

describe("listIntegrationInstances", () => {
  it("serializes filters as query parameters", async () => {
    mockFetchOnce({
      ok: true,
      body: { items: [], total: 0, page: 1, page_size: 50, has_next: false },
    });

    await listIntegrationInstances({
      provider_name: "quickbooks_online",
      category: "accounting",
      enabled: true,
      status: "connected",
      page: 2,
      page_size: 25,
    });

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    // URL-param ordering is insertion-order; assert each key rather
    // than a strict string match so a future reorder doesn't break
    // the test.
    expect(url).toContain("provider_name=quickbooks_online");
    expect(url).toContain("category=accounting");
    expect(url).toContain("enabled=true");
    expect(url).toContain("status=connected");
    expect(url).toContain("page=2");
    expect(url).toContain("page_size=25");
  });

  it("omits the query string entirely when no filters are provided", async () => {
    mockFetchOnce({
      ok: true,
      body: { items: [], total: 0, page: 1, page_size: 50, has_next: false },
    });

    await listIntegrationInstances();

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/integrations`);
  });
});

// ─── Create / Update / Delete ────────────────────────────────────────────────

describe("createIntegrationInstance", () => {
  it("POSTs the payload and returns the parsed instance", async () => {
    const instance = instanceFixture();
    mockFetchOnce({ ok: true, status: 201, body: instance });

    const result = await createIntegrationInstance({
      provider_name: "quickbooks_online",
      category: "accounting",
      enabled: true,
      credentials: { client_id: "x", client_secret: "y" },
    });

    expect(result).toEqual(instance);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/integrations`);
    expect(options.method).toBe("POST");
    const body = JSON.parse(options.body);
    expect(body.provider_name).toBe("quickbooks_online");
    // Credentials payload must be forwarded on the wire so the server
    // can unwrap it into the vault. We only verify it's *sent* — the
    // server is responsible for discarding plaintext after vaulting.
    expect(body.credentials).toEqual({ client_id: "x", client_secret: "y" });
  });

  it("translates a structured error envelope to ApiError.message", async () => {
    mockFetchOnce({
      ok: false,
      status: 503,
      body: {
        detail: {
          error_code: "credentials_vault_unavailable",
          message: "Credentials vault is not configured.",
        },
      },
    });

    await expect(
      createIntegrationInstance({
        provider_name: "quickbooks_online",
        category: "accounting",
        credentials: { client_id: "x" },
      }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      message: "Credentials vault is not configured.",
    });
  });

  it("falls back to the error_code when no message is supplied", async () => {
    mockFetchOnce({
      ok: false,
      status: 403,
      body: { detail: { error_code: "cross_tenant_access_denied" } },
    });

    await expect(
      createIntegrationInstance({
        provider_name: "quickbooks_online",
        category: "accounting",
      }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      message: "API error: cross_tenant_access_denied",
    });
  });
});

describe("updateIntegrationInstance", () => {
  it("PATCHes the instance-specific endpoint and URL-encodes ids", async () => {
    const instance = instanceFixture({ instance_id: "abc/123" });
    mockFetchOnce({ ok: true, body: instance });

    await updateIntegrationInstance("abc/123", { enabled: false });

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/integrations/abc%2F123`);
    expect(options.method).toBe("PATCH");
    expect(JSON.parse(options.body)).toEqual({ enabled: false });
  });
});

describe("deleteIntegrationInstance", () => {
  it("DELETEs the instance and swallows the 204 response body", async () => {
    mockFetchOnce({ ok: true, status: 204 });

    await expect(deleteIntegrationInstance("inst-1")).resolves.toBeUndefined();

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/integrations/inst-1`);
    expect(options.method).toBe("DELETE");
  });
});

describe("enableIntegrationInstance / disableIntegrationInstance", () => {
  it.each([
    ["enable", enableIntegrationInstance] as const,
    ["disable", disableIntegrationInstance] as const,
  ])("POSTs the %s endpoint", async (label, handler) => {
    mockFetchOnce({ ok: true, body: instanceFixture() });
    await handler("inst-1");
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/integrations/inst-1/${label}`);
    expect(options.method).toBe("POST");
  });
});

// ─── Sync runs ───────────────────────────────────────────────────────────────

describe("syncIntegrationNow", () => {
  it("POSTs sync-now and returns the run", async () => {
    const run = syncRunFixture();
    mockFetchOnce({ ok: true, body: run });

    const result = await syncIntegrationNow("inst-1");

    expect(result).toEqual(run);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/integrations/inst-1/sync-now`);
    expect(options.method).toBe("POST");
  });

  it("translates HTTP 400 instance_disabled to ApiError(400)", async () => {
    mockFetchOnce({
      ok: false,
      status: 400,
      body: {
        detail: {
          error_code: "instance_disabled",
          message: "Instance is disabled.",
        },
      },
    });

    await expect(syncIntegrationNow("inst-1")).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      message: "Instance is disabled.",
    });
  });
});

describe("listSyncRuns", () => {
  it("clamps the requested limit to the 1–50 server ceiling", async () => {
    mockFetchOnce({ ok: true, body: { items: [], total: 0 } });

    await listSyncRuns("inst-1", 999);

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/integrations/inst-1/sync-runs?limit=50`);
  });

  it("floors the requested limit to at least 1", async () => {
    mockFetchOnce({ ok: true, body: { items: [], total: 0 } });

    await listSyncRuns("inst-1", 0);

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/integrations/inst-1/sync-runs?limit=1`);
  });
});

// ─── Stripe ──────────────────────────────────────────────────────────────────

describe("getStripePublicConfig", () => {
  it("returns the publishable_key on a 200 response", async () => {
    mockFetchOnce({
      ok: true,
      body: { publishable_key: "pk_test_123" },
    });

    const result = await getStripePublicConfig();

    expect(result).toEqual({ publishable_key: "pk_test_123" });
  });

  it("returns null on a 404 so the UI can show a neutral state", async () => {
    mockFetchOnce({ ok: false, status: 404, body: { detail: "Not found" } });

    const result = await getStripePublicConfig();

    expect(result).toBeNull();
  });

  it("propagates non-404 errors so they surface as errors in the UI", async () => {
    mockFetchOnce({
      ok: false,
      status: 500,
      body: { detail: { error_code: "stripe_envelope_corrupt" } },
    });

    await expect(getStripePublicConfig()).rejects.toBeInstanceOf(ApiError);
  });
});

// ─── Status derivation ───────────────────────────────────────────────────────

describe("deriveMarketplaceStatus", () => {
  it("returns 'available' when no instance is configured", () => {
    expect(deriveMarketplaceStatus(null)).toBe("available");
  });

  it("returns 'error' when the rolling status is error", () => {
    expect(
      deriveMarketplaceStatus(
        instanceFixture({ status: "error", last_error: "boom" }),
      ),
    ).toBe("error");
  });

  it("returns 'disabled' when enabled=false even if status=connected", () => {
    expect(
      deriveMarketplaceStatus(
        instanceFixture({ enabled: false, status: "connected" }),
      ),
    ).toBe("disabled");
  });

  it("returns 'connected' on the happy path", () => {
    expect(
      deriveMarketplaceStatus(
        instanceFixture({ enabled: true, status: "connected" }),
      ),
    ).toBe("connected");
  });

  it("returns 'pending' for pending or disconnected rolling statuses", () => {
    expect(
      deriveMarketplaceStatus(
        instanceFixture({ enabled: true, status: "pending" }),
      ),
    ).toBe("pending");
    expect(
      deriveMarketplaceStatus(
        instanceFixture({ enabled: true, status: "disconnected" }),
      ),
    ).toBe("pending");
  });
});

// ─── findInstanceForProvider ─────────────────────────────────────────────────

describe("findInstanceForProvider", () => {
  it("returns the matching instance when one exists", () => {
    const a = instanceFixture({ provider_name: "quickbooks_online" });
    const b = instanceFixture({
      provider_name: "stripe",
      instance_id: "inst-2",
    });
    expect(findInstanceForProvider([a, b], "stripe")).toBe(b);
  });

  it("returns null when no matching instance exists", () => {
    expect(findInstanceForProvider([], "stripe")).toBeNull();
    expect(
      findInstanceForProvider(
        [instanceFixture({ provider_name: "quickbooks_online" })],
        "stripe",
      ),
    ).toBeNull();
  });
});
