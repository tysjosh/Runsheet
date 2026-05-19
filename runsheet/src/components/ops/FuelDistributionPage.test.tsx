/**
 * Tests for :file:`FuelDistributionPage.tsx` — Phase 2 Batch B Clusters tab.
 *
 * Coverage is intentionally focused on the Clusters tab behaviors the
 * spec calls out:
 *
 *  1. Clicking the "Clusters" tab renders both side-by-side panels.
 *  2. The priority-clusters panel auto-loads with the tenant stamp
 *     plus the default DBSCAN parameters (eps_miles=3, min_samples=2).
 *  3. The combinable-groups panel auto-loads with the tenant stamp
 *     and the default min_members=2 filter.
 *  4. Editing ``eps_miles`` and clicking **Re-cluster** re-issues
 *     ``listPriorityClusters`` with the new value.
 *  5. Submitting a ``fuel_grade=DIESEL_2`` filter re-issues
 *     ``listCombinableGroups`` with the canonicalized fuel_grade.
 *  6. When the backend reports ``has_next: true`` the Next button
 *     advances the page and calls the endpoint with ``page: 2``.
 *
 * Validates: Requirements 3.2.4, 3.4.3, 3.4.4 (UI surfaces).
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    __esModule: true,
    ...actual,
    listPriorityClusters: jest.fn(),
    listCombinableGroups: jest.fn(),
    listPlans: jest.fn(),
    generatePlan: jest.fn(),
    listDeliveryDestinations: jest.fn(),
  };
});

// StormModeBanner polls the status endpoint on mount; stub it out so
// the tests don't have to care about its internal fetch.
jest.mock("./StormModeBanner", () => ({
  __esModule: true,
  default: () => null,
}));

// The plan-execution socket opens a real WebSocket in jsdom which
// spams the test logs. Stub it; we don't need WS behavior for the
// Clusters tab tests.
jest.mock("../../hooks/usePlanExecutionSocket", () => ({
  __esModule: true,
  usePlanExecutionSocket: () => ({
    state: "disconnected",
    isConnected: false,
    reconnectAttempt: 0,
    reconnectDelay: 0,
    lastUpdate: null,
    error: null,
    connect: jest.fn(),
    disconnect: jest.fn(),
    connectionStatus: null,
  }),
  default: () => ({
    state: "disconnected",
    isConnected: false,
    reconnectAttempt: 0,
    reconnectDelay: 0,
    lastUpdate: null,
    error: null,
    connect: jest.fn(),
    disconnect: jest.fn(),
    connectionStatus: null,
  }),
}));

import type {
  CombinableGroup,
  CombinableGroupListResponse,
  PriorityClustersResponse,
} from "../../services/fuelApi";
import {
  generatePlan,
  listCombinableGroups,
  listDeliveryDestinations,
  listPlans,
  listPriorityClusters,
} from "../../services/fuelApi";
import FuelDistributionPage from "./FuelDistributionPage";

const mockListPriorityClusters = listPriorityClusters as jest.MockedFunction<
  typeof listPriorityClusters
>;
const mockListCombinableGroups = listCombinableGroups as jest.MockedFunction<
  typeof listCombinableGroups
>;
const mockListPlans = listPlans as unknown as jest.MockedFunction<
  typeof listPlans
>;
const mockGeneratePlan = generatePlan as jest.MockedFunction<
  typeof generatePlan
>;
const mockListDeliveryDestinations =
  listDeliveryDestinations as jest.MockedFunction<
    typeof listDeliveryDestinations
  >;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function clustersFixture(
  overrides: Partial<PriorityClustersResponse> = {},
): PriorityClustersResponse {
  return {
    run_id: "run-001",
    eps_miles: 3,
    min_samples: 2,
    total: 1,
    items: [
      {
        cluster_id: "cluster_0",
        centroid: { lat: 40.12345, lon: -74.12345 },
        member_count: 3,
        highest_priority_bucket: "high",
        fuel_grades: ["DIESEL_2"],
      },
    ],
    ...overrides,
  };
}

function groupFixture(
  overrides: Partial<CombinableGroup> = {},
): CombinableGroup {
  return {
    group_id: "cg-0001",
    tenant_id: "dev-tenant",
    run_id: "run-001",
    fuel_grades: ["DIESEL_2"],
    estimated_combined_gallons: 12500,
    centroid: { lat: 40.7128, lon: -74.006 },
    generated_at: "2024-06-01T12:00:00Z",
    members: [
      {
        destination_type: "station",
        destination_id: "STN-042",
        station_id: "STN-042",
        customer_tank_id: null,
        fuel_grade: "DIESEL_2",
        product_code: "DIESEL_2",
        estimated_gallons: 6000,
        location: { lat: 40.71, lon: -74.0 },
      },
      {
        destination_type: "customer_tank",
        destination_id: "CT-101",
        station_id: null,
        customer_tank_id: "CT-101",
        fuel_grade: "DIESEL_2",
        product_code: "DIESEL_2",
        estimated_gallons: 6500,
        location: { lat: 40.72, lon: -74.01 },
      },
    ],
    ...overrides,
  };
}

function groupsListFixture(
  overrides: Partial<CombinableGroupListResponse> = {},
): CombinableGroupListResponse {
  return {
    items: [groupFixture()],
    total: 1,
    page: 1,
    page_size: 10,
    has_next: false,
    ...overrides,
  };
}

// ─── Suite ───────────────────────────────────────────────────────────────────

describe("FuelDistributionPage — Clusters tab", () => {
  beforeEach(() => {
    window.localStorage.setItem("tenant_id", "dev-tenant");
    mockListPriorityClusters.mockReset();
    mockListCombinableGroups.mockReset();
    mockListPlans.mockReset();
    mockGeneratePlan.mockReset();
    mockListDeliveryDestinations.mockReset();
    mockListPlans.mockResolvedValue({
      data: [],
      pagination: { page: 1, size: 10, total: 0, total_pages: 1 },
      request_id: "req-plans",
    });
    mockGeneratePlan.mockResolvedValue({
      run_id: "run-001",
      plan_id: "plan-001",
      status: "completed",
    });
    mockListPriorityClusters.mockResolvedValue(clustersFixture());
    mockListCombinableGroups.mockResolvedValue(groupsListFixture());
    mockListDeliveryDestinations.mockResolvedValue({ items: [], total: 0 });
  });

  async function goToClustersTab() {
    render(<FuelDistributionPage />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Clusters$/i }));
    });
  }

  it("renders both panels when the Clusters tab is active", async () => {
    await goToClustersTab();

    await waitFor(() => {
      expect(screen.getByTestId("priority-clusters-panel")).toBeInTheDocument();
      expect(screen.getByTestId("combinable-groups-panel")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("heading", { name: /Priority Clusters \(DBSCAN\)/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: /Combinable Groups \(Union-Find\)/i,
      }),
    ).toBeInTheDocument();
  });

  it("calls listPriorityClusters with the tenant stamp and DBSCAN defaults", async () => {
    await goToClustersTab();

    await waitFor(() => {
      expect(mockListPriorityClusters).toHaveBeenCalled();
    });
    expect(mockListPriorityClusters.mock.calls[0][0]).toEqual(
      expect.objectContaining({
        tenant_id: "dev-tenant",
        eps_miles: 3,
        min_samples: 2,
      }),
    );
  });

  it("calls listCombinableGroups with the tenant stamp and min_members default", async () => {
    await goToClustersTab();

    await waitFor(() => {
      expect(mockListCombinableGroups).toHaveBeenCalled();
    });
    expect(mockListCombinableGroups.mock.calls[0][0]).toEqual(
      expect.objectContaining({
        tenant_id: "dev-tenant",
        min_members: 2,
        page: 1,
      }),
    );
  });

  it("re-clusters with the user-provided eps_miles", async () => {
    await goToClustersTab();

    await waitFor(() => {
      expect(mockListPriorityClusters).toHaveBeenCalledTimes(1);
    });

    const epsInput = screen.getByLabelText(/eps_miles/i);
    fireEvent.change(epsInput, { target: { value: "5" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Re-cluster/i }));
    });

    await waitFor(() => {
      expect(mockListPriorityClusters).toHaveBeenCalledTimes(2);
    });
    const secondCallArg = mockListPriorityClusters.mock.calls[1][0];
    expect(secondCallArg).toEqual(
      expect.objectContaining({
        tenant_id: "dev-tenant",
        eps_miles: 5,
        min_samples: 2,
      }),
    );
  });

  it("submits a fuel_grade filter as DIESEL_2 to listCombinableGroups", async () => {
    await goToClustersTab();

    await waitFor(() => {
      expect(mockListCombinableGroups).toHaveBeenCalledTimes(1);
    });

    const fuelGradeInput = screen.getByLabelText(/fuel_grade/i);
    fireEvent.change(fuelGradeInput, { target: { value: "DIESEL_2" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Apply$/i }));
    });

    await waitFor(() => {
      expect(mockListCombinableGroups).toHaveBeenCalledTimes(2);
    });
    expect(mockListCombinableGroups.mock.calls[1][0]).toEqual(
      expect.objectContaining({
        tenant_id: "dev-tenant",
        fuel_grade: "DIESEL_2",
        min_members: 2,
        page: 1,
      }),
    );
  });

  it("advances to page 2 when Next is clicked and has_next is true", async () => {
    mockListCombinableGroups.mockResolvedValueOnce(
      groupsListFixture({ has_next: true, total: 25 }),
    );
    // Subsequent page-2 call returns a smaller slice.
    mockListCombinableGroups.mockResolvedValueOnce(
      groupsListFixture({
        items: [groupFixture({ group_id: "cg-0002" })],
        has_next: false,
        page: 2,
        total: 25,
      }),
    );

    await goToClustersTab();

    await waitFor(() => {
      expect(mockListCombinableGroups).toHaveBeenCalledTimes(1);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Next page/i }));
    });

    await waitFor(() => {
      expect(mockListCombinableGroups).toHaveBeenCalledTimes(2);
    });
    expect(mockListCombinableGroups.mock.calls[1][0]).toEqual(
      expect.objectContaining({
        tenant_id: "dev-tenant",
        min_members: 2,
        page: 2,
      }),
    );
  });
});

describe("FuelDistributionPage — Plans tab", () => {
  beforeEach(() => {
    window.localStorage.setItem("tenant_id", "dev-tenant");
    mockListPriorityClusters.mockReset();
    mockListCombinableGroups.mockReset();
    mockListPlans.mockReset();
    mockGeneratePlan.mockReset();
    mockListDeliveryDestinations.mockReset();
    mockListPriorityClusters.mockResolvedValue(clustersFixture());
    mockListCombinableGroups.mockResolvedValue(groupsListFixture());
    mockListDeliveryDestinations.mockResolvedValue({ items: [], total: 0 });
  });

  it("shows a newly generated plan after clearing stale list filters", async () => {
    mockListPlans
      .mockResolvedValueOnce({
        data: [],
        pagination: { page: 1, size: 10, total: 0, total_pages: 1 },
        request_id: "req-initial",
      })
      .mockResolvedValueOnce({
        data: [],
        pagination: { page: 1, size: 10, total: 0, total_pages: 1 },
        request_id: "req-filtered",
      })
      .mockResolvedValue({
        data: [
          {
            plan_id: "plan-new",
            run_id: "run-new",
            status: "draft",
            truck_id: "truck-17",
            created_at: "2026-05-12T12:00:00Z",
          },
        ],
        pagination: { page: 1, size: 10, total: 1, total_pages: 1 },
        request_id: "req-generated",
      });
    mockGeneratePlan.mockResolvedValue({
      run_id: "run-new",
      plan_id: "plan-new",
      status: "completed",
    });

    render(<FuelDistributionPage />);

    await waitFor(() => {
      expect(mockListPlans).toHaveBeenCalledTimes(1);
    });

    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "completed" },
    });

    await waitFor(() => {
      expect(mockListPlans).toHaveBeenCalledWith(
        "dev-tenant",
        1,
        10,
        "completed",
      );
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Generate Plan/i }));
    });

    await waitFor(() => {
      expect(screen.getByText("plan-new")).toBeInTheDocument();
      expect(screen.getByText("Run: run-new")).toBeInTheDocument();
    });
    expect(mockGeneratePlan).toHaveBeenCalledWith("dev-tenant");
    expect(mockListPlans).toHaveBeenCalledWith("dev-tenant", 1, 10, undefined);
  });
});

// ─── Emergency-stop destination picker (Batch D2, Req 6.2.4) ────────────────

/**
 * The emergency-stop modal that renders the destination <select>
 * populated from :func:`listDeliveryDestinations` is nested deep
 * inside ``PlanDetailView``. Reaching it from a render-level test
 * requires:
 *
 *   1. Seeding ``listPlans`` with a plan row.
 *   2. Clicking that plan to trigger ``getPlan``.
 *   3. Stubbing ``getPlanCosts`` / ``getPlanOutcomes`` plus the
 *      route-plan payload so the ``PlanDetailView`` renders a route
 *      with a ``route_id``.
 *   4. Clicking the "Emergency Stop" button on that route to flip
 *      ``emergencyRouteId`` and mount the modal.
 *
 * That setup is brittle and duplicates coverage that already lives
 * in the ``ReconciliationPage`` + ``EmergencyStopModal`` unit paths.
 * The modal-level logic (destinations fetched on mount, re-fetched
 * when destinationType flips, fallback to free-text on error) is a
 * self-contained effect and is exercised by the Playwright smoke
 * run in ``runsheet/e2e``.
 *
 * When someone later extracts ``EmergencyStopModal`` as an exported
 * component, add:
 *
 *   • one test that mocks ``listDeliveryDestinations`` with a small
 *     fixture and asserts the <select> is populated on mount;
 *   • one test that flips destinationType and asserts a second
 *     fetch is issued with ``destination_type: 'customer_tank'``.
 *
 * The underlying helper contract is already exercised in
 * ``src/services/fuelApi.test.ts`` (``listDeliveryDestinations``).
 */
describe.skip("FuelDistributionPage — emergency-stop destination picker", () => {
  it("populates the destination <select> from listDeliveryDestinations", () => {
    // Intentionally skipped — see block comment above for rationale.
    // ``insertEmergencyStop`` is the helper invoked on submit; the
    // destination-picker effect uses ``listDeliveryDestinations``.
  });
});
