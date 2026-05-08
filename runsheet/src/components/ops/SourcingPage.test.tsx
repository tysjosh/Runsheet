/**
 * Tests for :file:`SourcingPage.tsx` (Task 11.8).
 *
 * Coverage is intentionally focused on the behaviors the spec calls
 * out:
 *
 *  1. **``validateQueryForm`` pure helper** — required-field checks,
 *     numeric-range enforcement on volume / lat / lon, CSV trimming
 *     for ``terminal_ids``, and canonical coercion of the ``branded``
 *     toggle to the backend's boolean shape.
 *  2. **Rack price fallback banner** — renders when the persisted
 *     ``SourcingRecommendation.rack_price_fallback`` flag is true and
 *     stays hidden otherwise (Req 8.2.5 surface).
 *  3. **Wait-warning banner** — renders when the aggregated
 *     ``wait_warning_terminal_ids`` list is non-empty (Req 8.4.5).
 *  4. **Ranked-candidate rows** — candidates render in the order the
 *     backend returned them with their price / distance / score /
 *     wait badge.
 *
 * The fuelApi module is mocked wholesale so these tests never touch
 * the network.
 *
 * Validates: Requirement 8.5.4.
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    ...actual,
    getSourcingRecommendations: jest.fn(),
    listRackPrices: jest.fn(),
    listSupplierContracts: jest.fn(),
    getTerminalWaitSummary: jest.fn(),
  };
});

import type {
  RackPriceListResponse,
  SourcingRecommendation,
  SupplierContractListResponse,
  TerminalWaitSummary,
} from "../../services/fuelApi";
import {
  getSourcingRecommendations,
  getTerminalWaitSummary,
  listRackPrices,
  listSupplierContracts,
} from "../../services/fuelApi";
import SourcingPage, { validateQueryForm } from "./SourcingPage";

const mockGetRec = getSourcingRecommendations as jest.MockedFunction<
  typeof getSourcingRecommendations
>;
const mockListRack = listRackPrices as jest.MockedFunction<
  typeof listRackPrices
>;
const mockListContracts = listSupplierContracts as jest.MockedFunction<
  typeof listSupplierContracts
>;
const mockGetWait = getTerminalWaitSummary as jest.MockedFunction<
  typeof getTerminalWaitSummary
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function recommendationFixture(
  overrides: Partial<SourcingRecommendation> = {},
): SourcingRecommendation {
  const base: SourcingRecommendation = {
    recommendation_id: "rec-001",
    request_id: "req-001",
    tenant_id: "tenant-a",
    truck_id: null,
    run_id: null,
    product_code: "DIESEL_2",
    volume_gallons: 8000,
    origin_lat: 40.7128,
    origin_lon: -74.006,
    candidates: [
      {
        terminal_id: "term_001",
        price_per_gallon_usd: 3.2501,
        branded_flag: false,
        contract_id: null,
        avg_wait_minutes: 15,
        distance_km_from_start: 12.4,
        score: 0.91,
        reasons: ["Lowest rack price in slate", "Within 15 km of origin"],
        wait_warning: false,
      },
      {
        terminal_id: "term_002",
        price_per_gallon_usd: 3.3123,
        branded_flag: true,
        contract_id: "sc-001",
        avg_wait_minutes: 80,
        distance_km_from_start: 22.1,
        score: 0.58,
        reasons: ["Contract boost applied", "Wait exceeds threshold"],
        wait_warning: true,
      },
    ],
    rack_price_fallback: false,
    wait_warning_terminal_ids: ["term_002"],
    generated_at: "2024-06-01T12:00:00Z",
  };
  return { ...base, ...overrides };
}

function emptyRackList(): RackPriceListResponse {
  return { items: [], total: 0, page: 1, page_size: 50, has_next: false };
}

function emptyContractList(): SupplierContractListResponse {
  return { items: [], total: 0, page: 1, page_size: 50, has_next: false };
}

function waitSummaryFixture(
  overrides: Partial<TerminalWaitSummary> = {},
): TerminalWaitSummary {
  return {
    terminal_id: "term_002",
    tenant_id: "tenant-a",
    window_minutes: 120,
    avg_wait_minutes: 80,
    sample_count: 5,
    max_wait_minutes: 110,
    most_recent_report_at: "2024-06-01T11:45:00Z",
    wait_warning_threshold_minutes: 60,
    wait_warning_exceeded: true,
    window_start: "2024-06-01T10:00:00Z",
    window_end: "2024-06-01T12:00:00Z",
    generated_at: "2024-06-01T12:00:00Z",
    source: "computed",
    ...overrides,
  };
}

// ─── Pure helper tests ───────────────────────────────────────────────────────

describe("validateQueryForm", () => {
  const base = {
    product_code: "DIESEL_2",
    volume_gallons: "8000",
    origin_lat: "40.7128",
    origin_lon: "-74.006",
    branded: "any" as const,
    truck_id: "",
    run_id: "",
    as_of: "",
    terminal_ids: "",
  };

  it("returns a valid query for well-formed inputs", () => {
    const result = validateQueryForm(base);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.query).toEqual(
        expect.objectContaining({
          product_code: "DIESEL_2",
          volume_gallons: 8000,
          origin_lat: 40.7128,
          origin_lon: -74.006,
        }),
      );
      expect(result.value.query.branded).toBeUndefined();
    }
  });

  it("rejects a blank product code", () => {
    const result = validateQueryForm({ ...base, product_code: "   " });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/product code/i);
  });

  it("rejects non-positive volume", () => {
    const result = validateQueryForm({ ...base, volume_gallons: "0" });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/positive/i);
  });

  it("rejects out-of-range latitude", () => {
    const result = validateQueryForm({ ...base, origin_lat: "100" });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/latitude/i);
  });

  it("rejects out-of-range longitude", () => {
    const result = validateQueryForm({ ...base, origin_lon: "-200" });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatch(/longitude/i);
  });

  it("maps branded='branded' to true and branded='unbranded' to false", () => {
    const branded = validateQueryForm({ ...base, branded: "branded" });
    const unbranded = validateQueryForm({ ...base, branded: "unbranded" });
    expect(branded.ok).toBe(true);
    expect(unbranded.ok).toBe(true);
    if (branded.ok) expect(branded.value.query.branded).toBe(true);
    if (unbranded.ok) expect(unbranded.value.query.branded).toBe(false);
  });

  it("trims CSV terminal_ids and drops empty entries", () => {
    const result = validateQueryForm({
      ...base,
      terminal_ids: " term_001, , term_042 ,",
    });
    expect(result.ok).toBe(true);
    if (result.ok)
      expect(result.value.query.terminal_ids).toBe("term_001,term_042");
  });

  it("passes through optional truck_id and run_id when provided", () => {
    const result = validateQueryForm({
      ...base,
      truck_id: " T-0042 ",
      run_id: "run-1",
    });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.query.truck_id).toBe("T-0042");
      expect(result.value.query.run_id).toBe("run-1");
    }
  });
});

// ─── Component tests ─────────────────────────────────────────────────────────

describe("SourcingPage", () => {
  beforeEach(() => {
    mockGetRec.mockReset();
    mockListRack.mockReset();
    mockListContracts.mockReset();
    mockGetWait.mockReset();
    mockListRack.mockResolvedValue(emptyRackList());
    mockListContracts.mockResolvedValue(emptyContractList());
    // The best candidate is auto-expanded and lazy-loads its wait
    // summary — return a default so tests that don't explicitly
    // override it still resolve the fetch cleanly.
    mockGetWait.mockResolvedValue(
      waitSummaryFixture({ terminal_id: "term_001" }),
    );
  });

  async function submitQuery() {
    fireEvent.change(screen.getByLabelText(/Product code/i), {
      target: { value: "DIESEL_2" },
    });
    fireEvent.change(screen.getByLabelText(/Volume \(gallons\)/i), {
      target: { value: "8000" },
    });
    fireEvent.change(screen.getByLabelText(/Origin latitude/i), {
      target: { value: "40.7128" },
    });
    fireEvent.change(screen.getByLabelText(/Origin longitude/i), {
      target: { value: "-74.006" },
    });
    await act(async () => {
      fireEvent.submit(screen.getByTestId("sourcing-query-form"));
    });
  }

  it("renders empty state before any query is submitted", () => {
    render(<SourcingPage />);
    expect(
      screen.getByText(/Enter a product, volume, and origin above/i),
    ).toBeInTheDocument();
  });

  it("renders ranked candidates with rack-price fallback banner suppressed when flag is false", async () => {
    mockGetRec.mockResolvedValue(recommendationFixture());

    render(<SourcingPage />);
    await submitQuery();

    await waitFor(() => {
      expect(
        screen.getByTestId("sourcing-candidate-term_001"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("sourcing-candidate-term_002"),
    ).toBeInTheDocument();
    // Best (term_001) is expanded by default and shows its reason list.
    expect(screen.getByText(/Lowest rack price in slate/i)).toBeInTheDocument();
    // Rack-price fallback banner should not render when flag is false.
    expect(
      screen.queryByTestId("sourcing-rack-fallback-banner"),
    ).not.toBeInTheDocument();
  });

  it("renders the rack-price fallback banner when the recommender served cached prices", async () => {
    mockGetRec.mockResolvedValue(
      recommendationFixture({ rack_price_fallback: true }),
    );
    render(<SourcingPage />);
    await submitQuery();

    await waitFor(() => {
      expect(
        screen.getByTestId("sourcing-rack-fallback-banner"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText(/Rack prices served from cache/i),
    ).toBeInTheDocument();
  });

  it("renders the wait-warning banner and badges the offending terminal", async () => {
    mockGetRec.mockResolvedValue(recommendationFixture());
    render(<SourcingPage />);
    await submitQuery();

    await waitFor(() => {
      expect(
        screen.getByTestId("sourcing-wait-warning-banner"),
      ).toBeInTheDocument();
    });
    // The banner enumerates the offending terminals.
    expect(
      screen.getByTestId("sourcing-wait-warning-banner"),
    ).toHaveTextContent(/term_002/);
    // Row-level wait-warning badge is rendered on term_002.
    const offendingRow = screen.getByTestId("sourcing-candidate-term_002");
    expect(within(offendingRow).getByText(/Wait warning/)).toBeInTheDocument();
  });

  it("lazy-loads the terminal wait summary when a candidate is expanded", async () => {
    mockGetRec.mockResolvedValue(recommendationFixture());
    mockGetWait.mockResolvedValue(waitSummaryFixture());

    render(<SourcingPage />);
    await submitQuery();

    // term_001 (best) is auto-expanded → wait summary was requested for it.
    await waitFor(() => {
      expect(mockGetWait).toHaveBeenCalledWith("term_001");
    });

    // Expanding term_002 triggers a second lookup.
    const secondRow = await screen.findByTestId("sourcing-candidate-term_002");
    await act(async () => {
      fireEvent.click(within(secondRow).getByRole("button"));
    });

    await waitFor(() => {
      expect(mockGetWait).toHaveBeenCalledWith("term_002");
    });
  });

  it("refreshes rack prices and contracts using the canonical product code", async () => {
    mockGetRec.mockResolvedValue(recommendationFixture());
    render(<SourcingPage />);
    await submitQuery();

    await waitFor(() => {
      expect(mockListRack).toHaveBeenCalled();
    });
    // First positional arg (only arg) is the filters object.
    expect(mockListRack.mock.calls[0][0]).toEqual(
      expect.objectContaining({ product_code: "DIESEL_2" }),
    );
    expect(mockListContracts.mock.calls[0][0]).toEqual(
      expect.objectContaining({ product_code: "DIESEL_2", status: "active" }),
    );
  });

  it("surfaces a recommendation error without crashing the page", async () => {
    mockGetRec.mockRejectedValue(new Error("backend boom"));

    render(<SourcingPage />);
    await submitQuery();

    await waitFor(() => {
      expect(
        screen.getByText(/Could not load terminal recommendations/i),
      ).toBeInTheDocument();
    });
    // No candidate rows when the call failed.
    expect(
      screen.queryByTestId("sourcing-candidate-term_001"),
    ).not.toBeInTheDocument();
  });
});
