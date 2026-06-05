/**
 * Unit tests for the compliance-read and terminal-BOL additions to
 * ``complianceApi.ts``.
 *
 * Covers the endpoints the frontend was missing against the backend
 * compliance + commerce surface:
 *
 *  • K-factor variance / suggestion
 *  • IFTA completeness / fleet-MPG
 *  • single asset-certification read / update
 *  • price-protection contract variance
 *  • terminal-BOL confirm / link
 *
 * We mock ``global.fetch`` to verify URL assembly (including query-string
 * serialization and path encoding), HTTP method, and JSON body handling.
 */

import { ApiError } from "./api";
import {
  confirmTerminalBOL,
  getAssetCertification,
  getFleetMPG,
  getIFTACompleteness,
  getKFactorSuggestion,
  getKFactorVariance,
  getPriceProtectionVariance,
  linkTerminalBOL,
  updateAssetCertification,
} from "./complianceApi";

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

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── K-Factor ────────────────────────────────────────────────────────────────

describe("getKFactorVariance", () => {
  it("passes delivery_id as a query param", async () => {
    mockFetchOnce({
      ok: true,
      body: { data: { tank_id: "tank-1" }, request_id: "r" },
    });

    await getKFactorVariance("tank-1", "del-9");

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(
      `${API_BASE_URL}/compliance/kfactor/tank-1/variance?delivery_id=del-9`,
    );
  });
});

describe("getKFactorSuggestion", () => {
  it("GETs the suggest endpoint and returns the suggestion envelope", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: { tank_id: "tank-1", suggested_kfactor: null },
        request_id: "r",
      },
    });

    const result = await getKFactorSuggestion("tank-1");

    expect(result.data.suggested_kfactor).toBeNull();
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/compliance/kfactor/tank-1/suggest`);
  });
});

// ─── IFTA ──────────────────────────────────────────────────────────────────

describe("getIFTACompleteness", () => {
  it("reads the count/complete envelope shape", async () => {
    mockFetchOnce({
      ok: true,
      body: { data: [], count: 0, complete: true, request_id: "r" },
    });

    const result = await getIFTACompleteness("2026-Q1");

    expect(result.complete).toBe(true);
    expect(result.count).toBe(0);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(
      `${API_BASE_URL}/compliance/ifta/completeness?quarter=2026-Q1`,
    );
  });
});

describe("getFleetMPG", () => {
  it("GETs the fleet-mpg endpoint with the quarter query param", async () => {
    mockFetchOnce({
      ok: true,
      body: { data: { quarter: "2026-Q1", fleet_mpg: 6.4 }, request_id: "r" },
    });

    const result = await getFleetMPG("2026-Q1");

    expect(result.data.fleet_mpg).toBe(6.4);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(
      `${API_BASE_URL}/compliance/ifta/fleet-mpg?quarter=2026-Q1`,
    );
  });
});

// ─── Asset Certification (single) ───────────────────────────────────────────

describe("asset certification read/update", () => {
  it("getAssetCertification GETs the cert by id", async () => {
    mockFetchOnce({
      ok: true,
      body: { data: { cert_id: "cert-1" }, request_id: "r" },
    });

    await getAssetCertification("cert-1");

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/compliance/asset-certifications/cert-1`);
    expect(options?.method).toBeUndefined();
  });

  it("updateAssetCertification PUTs the patch body", async () => {
    mockFetchOnce({
      ok: true,
      body: { data: { cert_id: "cert-1" }, request_id: "r" },
    });

    await updateAssetCertification("cert-1", {
      inspector_name: "J. Smith",
    });

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/compliance/asset-certifications/cert-1`);
    expect(options.method).toBe("PUT");
    expect(JSON.parse(options.body)).toEqual({ inspector_name: "J. Smith" });
  });
});

// ─── Price Protection Variance ──────────────────────────────────────────────

describe("getPriceProtectionVariance", () => {
  it("GETs the contract variance report", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: {
          contract_id: "ctr-1",
          total_variance_cents: 1200,
          total_gallons: 5000,
          delivery_count: 3,
          breakdown: [],
          contract: {},
        },
        request_id: "r",
      },
    });

    const result = await getPriceProtectionVariance("ctr-1");

    expect(result.data.total_variance_cents).toBe(1200);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(
      `${API_BASE_URL}/commerce/price-protection-contracts/ctr-1/variance`,
    );
  });

  it("raises ApiError when the contract is unknown (404)", async () => {
    mockFetchOnce({ ok: false, status: 404, body: { detail: "not found" } });

    await expect(getPriceProtectionVariance("ctr-x")).rejects.toThrow(ApiError);
  });
});

// ─── Terminal BOL confirm / link ────────────────────────────────────────────

describe("terminal BOL confirm/link", () => {
  it("confirmTerminalBOL POSTs only the provided fields", async () => {
    mockFetchOnce({
      ok: true,
      body: { data: { bol_id: "bol-1", status: "ingested" }, request_id: "r" },
    });

    await confirmTerminalBOL("bol-1", {
      load_number: "LN-42",
      net_gallons: 7800,
    });

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/compliance/terminal-bols/bol-1/confirm`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      load_number: "LN-42",
      net_gallons: 7800,
    });
  });

  it("confirmTerminalBOL defaults to an empty body", async () => {
    mockFetchOnce({
      ok: true,
      body: { data: { bol_id: "bol-1", status: "ingested" }, request_id: "r" },
    });

    await confirmTerminalBOL("bol-1");

    const [, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(JSON.parse(options.body)).toEqual({});
  });

  it("linkTerminalBOL POSTs the load_plan_id and reads the summary", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        data: { bol_id: "bol-1", load_plan_id: "lp-9", status: "linked" },
        request_id: "r",
      },
    });

    const result = await linkTerminalBOL("bol-1", { load_plan_id: "lp-9" });

    expect(result.data.status).toBe("linked");
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/compliance/terminal-bols/bol-1/link`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ load_plan_id: "lp-9" });
  });
});
