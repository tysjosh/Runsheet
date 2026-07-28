/**
 * Unit tests for the Fuel Ops Hardening additions to ``fuelApi.ts``.
 *
 * Focuses on the typed clients introduced by task 11.10 of the
 * ``fuel-ops-hardening`` spec: products + destinations catalog,
 * compartment load-eligibility, priority clusters, combinable groups,
 * POD hash-proof / hash-chain verify, terminal wait reports, and
 * storm-mode road restrictions.
 *
 * We mock ``global.fetch`` rather than wiring a real HTTP client so the
 * tests verify URL assembly, query-string serialization, HTTP method
 * selection, and JSON body handling. Return values are shallow-compared
 * against the mocked envelope to ensure the helpers pass responses
 * through without silently reshaping them.
 *
 * Validates: Requirements 1.6.4 (indirect), 3.2.4, 3.4.3, 4.5.3, 4.5.4,
 * 6.1.3, 6.2.4, 7.2.5, 8.4.2, 9.3.3, 9.3.5.
 */

import {
  checkCompartmentLoadEligibility,
  configureCompartments,
  createStation,
  getPodHashProof,
  LITERS_PER_GALLON,
  listCombinableGroups,
  listDeliveryDestinations,
  listFuelProducts,
  listPriorityClusters,
  listStormRoadRestrictions,
  litersToGallons,
  recordConsumption,
  recordRefill,
  submitTerminalWaitReport,
  updateStation,
  uploadStormRoadRestriction,
  verifyPodHashChain,
} from "./fuelApi";

const API_BASE_URL = "http://localhost:8080/api";

// ─── Test helpers ────────────────────────────────────────────────────────────

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

// ─── listFuelProducts (Req 6.1.3) ────────────────────────────────────────────

describe("listFuelProducts", () => {
  it("GETs /fuel/products without query params and returns the envelope", async () => {
    const envelope = {
      region: "US",
      items: [
        {
          product_code: "DIESEL_2",
          display_name: "Diesel #2",
          category: "diesel",
          density_lbs_per_gallon: 7.079,
          tax_class: "federal_highway",
          aliases: ["ULSD"],
          region_availability: ["US"],
        },
      ],
      total: 1,
    };
    mockFetchOnce({ ok: true, body: envelope });

    const result = await listFuelProducts();

    expect(result).toEqual(envelope);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/fuel/products`);
    // ``fuelRequest`` defaults to GET when no method is supplied.
    expect(options.method).toBeUndefined();
  });
});

// ─── listDeliveryDestinations (Req 6.2.4) ────────────────────────────────────

describe("listDeliveryDestinations", () => {
  it("serializes the supplied filters as query parameters", async () => {
    const envelope = { items: [], total: 0 };
    mockFetchOnce({ ok: true, body: envelope });

    await listDeliveryDestinations({
      destination_type: "customer_tank",
      fuel_product: "PROPANE",
      zip_code: "01020",
    });

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toContain(`${API_BASE_URL}/fuel/destinations?`);
    expect(url).toContain("destination_type=customer_tank");
    expect(url).toContain("fuel_product=PROPANE");
    expect(url).toContain("zip_code=01020");
  });

  it("omits the query string entirely when no filters are supplied", async () => {
    mockFetchOnce({ ok: true, body: { items: [], total: 0 } });

    await listDeliveryDestinations();

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/fuel/destinations`);
  });
});

// ─── checkCompartmentLoadEligibility (Req 7.2.5) ─────────────────────────────

describe("checkCompartmentLoadEligibility", () => {
  it("URL-encodes the path segment and serializes product_code", async () => {
    const envelope = {
      compartment_id: "comp/1",
      proposed_product: "DIESEL_2",
      previous_product: null,
      decision: "allowed",
      reason: null,
      governing_rule: "allowed",
      compartment_state: {
        compartment_id: "comp/1",
        truck_id: "truck-1",
        state: "clean",
        last_loaded_product: null,
        last_loaded_at: null,
        last_cleaned_at: null,
      },
    };
    mockFetchOnce({ ok: true, body: envelope });

    const result = await checkCompartmentLoadEligibility("comp/1", "DIESEL_2");

    expect(result).toEqual(envelope);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    // Path segment slash is URL-encoded to ``%2F``.
    expect(url).toBe(
      `${API_BASE_URL}/fuel/mvp/compartments/comp%2F1/load-eligibility?product_code=DIESEL_2`,
    );
  });
});

// ─── listPriorityClusters (Req 3.4.3) ────────────────────────────────────────

describe("listPriorityClusters", () => {
  it("serializes eps_miles + min_samples onto the query string", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        run_id: "run-1",
        eps_miles: 4.5,
        min_samples: 3,
        items: [],
        total: 0,
      },
    });

    await listPriorityClusters({ eps_miles: 4.5, min_samples: 3 });

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toContain("eps_miles=4.5");
    expect(url).toContain("min_samples=3");
  });

  it("omits undefined query params", async () => {
    mockFetchOnce({
      ok: true,
      body: {
        run_id: null,
        eps_miles: 3.0,
        min_samples: 2,
        items: [],
        total: 0,
      },
    });

    await listPriorityClusters();

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/fuel/mvp/priority-clusters`);
  });
});

// ─── listCombinableGroups (Req 3.2.4) ────────────────────────────────────────

describe("listCombinableGroups", () => {
  it("passes filter + pagination params through", async () => {
    mockFetchOnce({
      ok: true,
      body: { items: [], total: 0, page: 1, page_size: 20, has_next: false },
    });

    await listCombinableGroups({
      run_id: "run-7",
      fuel_grade: "DIESEL_2",
      min_members: 3,
      page: 2,
      size: 50,
    });

    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toContain("run_id=run-7");
    expect(url).toContain("fuel_grade=DIESEL_2");
    expect(url).toContain("min_members=3");
    expect(url).toContain("page=2");
    expect(url).toContain("size=50");
  });
});

// ─── POD hash-proof + hash-chain verify (Req 4.5.3, 4.5.4) ───────────────────

describe("getPodHashProof", () => {
  it("URL-encodes pod_id and returns the envelope", async () => {
    const envelope = {
      pod_id: "pod/42",
      tenant_id: "tenant-a",
      pod_hash: "a".repeat(64),
      previous_pod_hash: "0".repeat(64),
      canonical_payload: { pod_id: "pod/42" },
      canonical_payload_bytes: '{"pod_id":"pod/42"}',
    };
    mockFetchOnce({ ok: true, body: envelope });

    const result = await getPodHashProof("pod/42");

    expect(result).toEqual(envelope);
    const [url] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/fuel/pod/pod%2F42/hash-proof`);
  });
});

describe("verifyPodHashChain", () => {
  it("POSTs the body and returns the envelope", async () => {
    const envelope = {
      tenant_id: "tenant-a",
      verified_count: 5,
      total_requested: 5,
      valid: true,
      first_mismatch: null,
      pod_ids_checked: ["p1", "p2", "p3", "p4", "p5"],
    };
    mockFetchOnce({ ok: true, body: envelope });

    const result = await verifyPodHashChain({
      from_pod_id: "p1",
      to_pod_id: "p5",
      limit: 50,
    });

    expect(result).toEqual(envelope);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/fuel/pod/hash-chain/verify`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      from_pod_id: "p1",
      to_pod_id: "p5",
      limit: 50,
    });
  });
});

// ─── Terminal wait reports (Req 8.4.2) ───────────────────────────────────────

describe("submitTerminalWaitReport", () => {
  it("POSTs to the nested wait-reports path with the JSON body", async () => {
    const persisted = {
      report_id: "twr-1",
      tenant_id: "tenant-a",
      terminal_id: "term-1",
      wait_minutes: 35,
      source: "driver_report",
      reporter_id: "driver-99",
      truck_id: null,
      observed_at: "2025-01-01T10:00:00Z",
      retrieved_at: "2025-01-01T10:00:01Z",
      updated_at: null,
      created_at: null,
    };
    mockFetchOnce({ ok: true, status: 201, body: persisted });

    const result = await submitTerminalWaitReport("term-1", {
      wait_minutes: 35,
      source: "driver_report",
      reporter_id: "driver-99",
      observed_at: "2025-01-01T10:00:00Z",
    });

    expect(result).toEqual(persisted);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/fuel/terminals/term-1/wait-reports`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body).source).toBe("driver_report");
  });
});

// ─── Storm-mode road restrictions (Req 9.3.3, 9.3.5) ────────────────────────

describe("uploadStormRoadRestriction", () => {
  it("POSTs a GeoJSON polygon and returns the persisted row", async () => {
    const polygon = {
      type: "Polygon",
      coordinates: [
        [
          [-72.5, 42.1],
          [-72.4, 42.1],
          [-72.4, 42.2],
          [-72.5, 42.2],
          [-72.5, 42.1],
        ],
      ],
    };
    const persisted = {
      restriction_id: "srr-1",
      tenant_id: "tenant-a",
      polygon,
      effective_from: "2025-01-01T00:00:00Z",
      effective_to: null,
      source: "manual",
      severity: "severe" as const,
      reason: "flooded underpass",
      updated_at: null,
      created_at: null,
    };
    mockFetchOnce({ ok: true, status: 201, body: persisted });

    const result = await uploadStormRoadRestriction({
      polygon,
      effective_from: "2025-01-01T00:00:00Z",
      source: "manual",
      severity: "severe",
      reason: "flooded underpass",
    });

    expect(result).toEqual(persisted);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/fuel/storm-mode/road-restrictions`);
    expect(options.method).toBe("POST");
    const body = JSON.parse(options.body);
    expect(body.polygon).toEqual(polygon);
    expect(body.severity).toBe("severe");
  });
});

describe("listStormRoadRestrictions", () => {
  it("GETs the road-restrictions list endpoint", async () => {
    mockFetchOnce({ ok: true, body: { items: [], total: 0 } });

    const result = await listStormRoadRestrictions();

    expect(result).toEqual({ items: [], total: 0 });
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/fuel/storm-mode/road-restrictions`);
    expect(options.method).toBeUndefined();
  });
});

// ─── Volume unit boundary: gallons in the UI, liters on the wire ─────────────
//
// The station-CRUD, fuel-event, and compartment-config endpoints still declare
// their volumes in liters (``fuel.models.CreateFuelStation.capacity_liters``,
// ``fuel.models.ConsumptionEvent.quantity_liters``,
// ``Agents.support.mvp_endpoints.CompartmentConfig.capacity_liters``). These
// tests pin the conversion to the service layer so components stay gallons-only
// and the wire field names keep matching what the backend accepts.

describe("volume unit boundary", () => {
  it("createStation converts gallons to the liters fields the endpoint requires", async () => {
    mockFetchOnce({ ok: true, body: { station_id: "FS-1" } });

    await createStation(
      {
        station_id: "FS-1",
        name: "Houston Main",
        fuel_type: "DIESEL_2",
        capacity_gallons: 1000,
        initial_stock_gallons: 500,
        alert_threshold_pct: 20,
      },
      "tenant-a",
    );

    const [, options] = (global.fetch as jest.Mock).mock.calls[0];
    const body = JSON.parse(options.body);
    expect(body.capacity_liters).toBeCloseTo(1000 * LITERS_PER_GALLON, 6);
    expect(body.initial_stock_liters).toBeCloseTo(500 * LITERS_PER_GALLON, 6);
    // No gallons keys leak onto the wire; the backend would ignore them.
    expect(body).not.toHaveProperty("capacity_gallons");
    expect(body).not.toHaveProperty("initial_stock_gallons");
    expect(body.fuel_type).toBe("DIESEL_2");
  });

  it("updateStation omits capacity entirely when the caller did not change it", async () => {
    mockFetchOnce({ ok: true, body: { station_id: "FS-1" } });

    await updateStation("FS-1", { name: "Renamed" }, "tenant-a");

    const [, options] = (global.fetch as jest.Mock).mock.calls[0];
    const body = JSON.parse(options.body);
    expect(body).toEqual({ name: "Renamed" });
  });

  it("updateStation converts a changed capacity to capacity_liters", async () => {
    mockFetchOnce({ ok: true, body: { station_id: "FS-1" } });

    await updateStation("FS-1", { capacity_gallons: 250 }, "tenant-a");

    const [, options] = (global.fetch as jest.Mock).mock.calls[0];
    const body = JSON.parse(options.body);
    expect(body.capacity_liters).toBeCloseTo(250 * LITERS_PER_GALLON, 6);
    expect(body).not.toHaveProperty("capacity_gallons");
  });

  it("recordConsumption sends quantity_liters", async () => {
    mockFetchOnce({ ok: true, body: { data: {}, request_id: "r1" } });

    await recordConsumption({
      station_id: "FS-1",
      fuel_type: "DIESEL_2",
      quantity_gallons: 40,
      asset_id: "TRK-1",
      operator_id: "op-1",
    });

    const [, options] = (global.fetch as jest.Mock).mock.calls[0];
    const body = JSON.parse(options.body);
    expect(body.quantity_liters).toBeCloseTo(40 * LITERS_PER_GALLON, 6);
    expect(body).not.toHaveProperty("quantity_gallons");
  });

  it("recordRefill sends quantity_liters", async () => {
    mockFetchOnce({ ok: true, body: { data: {}, request_id: "r1" } });

    await recordRefill({
      station_id: "FS-1",
      fuel_type: "GASOLINE_REG",
      quantity_gallons: 8000,
      supplier: "Global Fuels",
      operator_id: "op-1",
    });

    const [, options] = (global.fetch as jest.Mock).mock.calls[0];
    const body = JSON.parse(options.body);
    expect(body.quantity_liters).toBeCloseTo(8000 * LITERS_PER_GALLON, 6);
    expect(body).not.toHaveProperty("quantity_gallons");
  });

  it("configureCompartments maps capacity_gallons to capacity_liters per row", async () => {
    mockFetchOnce({
      ok: true,
      body: { truck_id: "TNK-1", compartments_configured: 2, status: "ok" },
    });

    await configureCompartments("TNK-1", [
      {
        compartment_id: "C1",
        capacity_gallons: 3000,
        allowed_grades: ["DIESEL_2"],
        position_index: 0,
      },
      {
        compartment_id: "C2",
        capacity_gallons: 1500,
        allowed_grades: ["GASOLINE_REG", "GASOLINE_PREM"],
        position_index: 1,
      },
    ]);

    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/fuel/mvp/compartments/TNK-1`);
    expect(options.method).toBe("PUT");
    const body = JSON.parse(options.body);
    expect(body.compartments).toHaveLength(2);
    expect(body.compartments[0].capacity_liters).toBeCloseTo(
      3000 * LITERS_PER_GALLON,
      6,
    );
    expect(body.compartments[1].capacity_liters).toBeCloseTo(
      1500 * LITERS_PER_GALLON,
      6,
    );
    for (const row of body.compartments) {
      expect(row).not.toHaveProperty("capacity_gallons");
    }
    // Canonical US product codes are passed through untouched.
    expect(body.compartments[1].allowed_grades).toEqual([
      "GASOLINE_REG",
      "GASOLINE_PREM",
    ]);
  });

  it("litersToGallons round-trips the values the read boundary sees", () => {
    expect(litersToGallons(3785.411784)).toBeCloseTo(1000, 6);
    expect(litersToGallons(null)).toBe(0);
  });
});
