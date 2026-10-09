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
  within,
} from "@testing-library/react";

// Plans' sub-views are URL-synced (`?sub=`, UI revamp R8.7): a stateful
// router mock so a click on "Clusters" actually switches the view.
jest.mock("next/navigation", () => {
  const { useSyncExternalStore } = jest.requireActual("react");
  let qs = "";
  const listeners = new Set<() => void>();
  const replace = (url: string) => {
    qs = url.split("?")[1] ?? "";
    for (const l of listeners) l();
  };
  return {
    useRouter: () => ({ push: replace, replace, prefetch() {}, back() {} }),
    usePathname: () => "/dashboard/dispatch",
    useSearchParams: () =>
      new URLSearchParams(
        useSyncExternalStore(
          (l: () => void) => {
            listeners.add(l);
            return () => listeners.delete(l);
          },
          () => qs,
        ),
      ),
    useParams: () => ({}),
    __reset: () => {
      qs = "";
    },
  };
});
beforeEach(() => {
  (jest.requireMock("next/navigation") as { __reset: () => void }).__reset();
});
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
    getPlan: jest.fn(),
    getPlanCosts: jest.fn(),
    getPlanOutcomes: jest.fn(),
    rejectPlan: jest.fn(),
  };
});
jest.mock("../../utils/auth", () => ({
  ...jest.requireActual("../../utils/auth"),
  getCurrentUserId: jest.fn().mockResolvedValue("dispatcher-1"),
}));

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
  getPlan,
  getPlanCosts,
  getPlanOutcomes,
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
const mockGetPlan = getPlan as jest.MockedFunction<typeof getPlan>;
const mockGetPlanCosts = getPlanCosts as jest.MockedFunction<
  typeof getPlanCosts
>;
const mockGetPlanOutcomes = getPlanOutcomes as jest.MockedFunction<
  typeof getPlanOutcomes
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
      fireEvent.click(screen.getByRole("tab", { name: /^Clusters$/i }));
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

    const epsInput = screen.getByLabelText(/Cluster radius/i);
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

    const fuelGradeInput = screen.getByLabelText(/^Product$/i);
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
      // The list is a table now: the run id has its own column.
      expect(screen.getByText("run-new")).toBeInTheDocument();
    });
    expect(mockGeneratePlan).toHaveBeenCalledWith("dev-tenant");
    expect(mockListPlans).toHaveBeenCalledWith("dev-tenant", 1, 10, undefined);
    expect(screen.getByText("Plan generated successfully")).toBeInTheDocument();
  });

  it("does not report success when the run came back degraded", async () => {
    // A degraded run raised nothing — every pipeline stage returned normally —
    // but the route stage produced no routes. This page is the last consumer of
    // the completion signal, so an unconditional success toast here is where
    // the silent skip would reach the dispatcher as "it worked".
    mockListPlans.mockResolvedValue({
      data: [],
      pagination: { page: 1, size: 10, total: 0, total_pages: 1 },
      request_id: "req-degraded",
    });
    mockGeneratePlan.mockResolvedValue({
      run_id: "run-degraded",
      status: "degraded",
      degraded: true,
      degradation_reasons: [
        {
          agent_id: "route_planning",
          reasons: [{ reason_code: "unresolvable_stop_locations" }],
        },
      ],
    });

    render(<FuelDistributionPage />);

    await waitFor(() => {
      expect(mockListPlans).toHaveBeenCalled();
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Generate Plan/i }));
    });

    await waitFor(() => {
      expect(
        screen.getByText(/Plan generated with problems: route_planning/),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Plan generated successfully"),
    ).not.toBeInTheDocument();
  });

  async function clickGenerate() {
    mockListPlans.mockResolvedValue({
      data: [],
      pagination: { page: 1, size: 10, total: 0, total_pages: 1 },
      request_id: "req-generate",
    });
    render(<FuelDistributionPage />);
    await waitFor(() => {
      expect(mockListPlans).toHaveBeenCalled();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Generate Plan/i }));
    });
  }

  it("shows the backend detail when a dyed-diesel plan was blocked (OI-02)", async () => {
    const detail =
      "Dyed-diesel compliance check unavailable: 1 loading plan(s) blocked (trucks truck-A). Retry when the compliance service is back.";
    mockGeneratePlan.mockResolvedValue({
      run_id: "run-blocked",
      status: "degraded",
      degraded: true,
      degradation_reasons: [
        {
          agent_id: "compartment_loading",
          reasons: [{ reason_code: "dyed_diesel_check_unavailable", detail }],
        },
      ],
    });

    await clickGenerate();

    await waitFor(() => {
      expect(
        screen.getByText(`Plan generated with problems: ${detail}`),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Plan generated successfully"),
    ).not.toBeInTheDocument();
  });

  it("reports a failed run as a failure, not success", async () => {
    mockGeneratePlan.mockResolvedValue({
      run_id: "run-failed",
      status: "failed",
      failed_agent: "compartment_loading",
      error_message: "ES connection failed",
    });

    await clickGenerate();

    await waitFor(() => {
      expect(
        screen.getByText("Plan generation failed: ES connection failed"),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Plan generated successfully"),
    ).not.toBeInTheDocument();
  });

  it("lists orders that could not be loaded (OI-39)", async () => {
    mockGeneratePlan.mockResolvedValue({
      run_id: "run-unplaced",
      status: "complete",
      unplaced_orders: [
        {
          order_id: "ORD-1",
          station_id: "st-1",
          product_code: "GASOLINE_REG",
          liters: 4000,
          reason: "no_compatible_compartment",
          partial: false,
        },
      ],
    });

    await clickGenerate();

    await waitFor(() => {
      expect(
        screen.getByText(
          "1 order(s) not loaded: ORD-1 (no compatible compartment)",
        ),
      ).toBeInTheDocument();
    });
    expect(screen.getByText("Plan generated successfully")).toBeInTheDocument();
  });

  it("caps the unplaced list at five ids and shows unknown reasons verbatim", async () => {
    mockGeneratePlan.mockResolvedValue({
      run_id: "run-many",
      status: "complete",
      unplaced_orders: Array.from({ length: 7 }, (_, i) => ({
        order_id: `ORD-${i + 1}`,
        station_id: "st-1",
        liters: 100,
        reason: i === 0 ? "some_new_reason" : "no_truck_capacity",
      })),
    });

    await clickGenerate();

    await waitFor(() => {
      expect(
        screen.getByText(
          "7 order(s) not loaded: ORD-1 (some_new_reason), ORD-2 (no truck capacity), ORD-3 (no truck capacity), ORD-4 (no truck capacity), ORD-5 (no truck capacity), +2 more",
        ),
      ).toBeInTheDocument();
    });
  });

  it("renders a scheduled plan with a warning badge, Approve and no Reject (R12.7)", async () => {
    mockListPlans.mockResolvedValue({
      data: [
        {
          plan_id: "plan-sched",
          run_id: "run-sched",
          status: "scheduled",
          truck_id: "truck-9",
          created_at: "2026-10-04T12:00:00Z",
        },
      ],
      pagination: { page: 1, size: 10, total: 1, total_pages: 1 },
      request_id: "req-sched",
    });
    render(<FuelDistributionPage />);
    const badge = await screen.findByText("scheduled");
    expect(badge).toHaveClass("bg-warning-light");
    expect(badge).not.toHaveClass("bg-info-light");
    expect(
      screen.getByRole("button", { name: "Approve plan plan-sched" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Reject plan plan-sched" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "Scheduled" }),
    ).toBeInTheDocument();
  });

  // Review pass 1, finding 2 (owner decision "hide"): a plan keeps
  // draft/proposed until the executor finalizes it, so Reject must also
  // follow execution_status. Approve stays as the recovery path.
  function planRow(executionStatus: string | null) {
    return {
      plan_id: "plan-exec",
      run_id: "run-exec",
      status: "proposed",
      truck_id: "truck-9",
      created_at: "2026-10-04T12:00:00Z",
      total_utilization_pct: 80,
      execution_status: executionStatus,
    };
  }

  function mockPlanList(executionStatus: string | null) {
    mockListPlans.mockResolvedValue({
      data: [planRow(executionStatus)],
      pagination: { page: 1, size: 10, total: 1, total_pages: 1 },
      request_id: "req-exec",
    } as never);
  }

  it.each(["in_progress", "incomplete", "succeeded"])(
    "hides Reject but keeps Approve in the plan list when execution_status is %s",
    async (executionStatus) => {
      mockPlanList(executionStatus);
      render(<FuelDistributionPage />);
      expect(
        await screen.findByRole("button", { name: "Approve plan plan-exec" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Reject plan plan-exec" }),
      ).not.toBeInTheDocument();
    },
  );

  it.each([null, "failed"])(
    "still offers Reject in the plan list when execution_status is %s",
    async (executionStatus) => {
      mockPlanList(executionStatus);
      render(<FuelDistributionPage />);
      expect(
        await screen.findByRole("button", { name: "Reject plan plan-exec" }),
      ).toBeInTheDocument();
    },
  );

  async function openDetail(executionStatus: string | null) {
    mockPlanList(executionStatus);
    mockGetPlan.mockResolvedValue({
      plan_id: "plan-exec",
      loading_plan: {
        ...planRow(executionStatus),
        assignments: [],
        unserved_demand_liters: 0,
        total_weight_kg: 0,
        tenant_id: "dev-tenant",
      },
      route_plan: null,
    } as never);
    mockGetPlanCosts.mockRejectedValue(new Error("no costs"));
    mockGetPlanOutcomes.mockRejectedValue(new Error("no outcomes"));
    render(<FuelDistributionPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "View plan plan-exec" }),
    );
    await screen.findByText("Plan: plan-exec");
  }

  it.each(["in_progress", "incomplete", "succeeded"])(
    "hides Reject but keeps Approve in the plan detail when execution_status is %s",
    async (executionStatus) => {
      await openDetail(executionStatus);
      expect(
        screen.getByRole("button", { name: /^Approve$/ }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /^Reject$/ }),
      ).not.toBeInTheDocument();
    },
  );

  it("still offers Reject in the plan detail when the executor never ran", async () => {
    await openDetail(null);
    expect(
      screen.getByRole("button", { name: /^Reject$/ }),
    ).toBeInTheDocument();
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

describe("Dispatch → Plans chrome (UI revamp task 2.5, R8.7)", () => {
  beforeEach(() => {
    window.localStorage.setItem("tenant_id", "dev-tenant");
    mockListPlans.mockResolvedValue({
      data: [
        {
          plan_id: "plan-r",
          run_id: "run-r",
          status: "proposed",
          truck_id: "truck-9",
          created_at: "2026-10-04T12:00:00Z",
          total_utilization_pct: 80,
          execution_status: null,
        },
      ],
      pagination: { page: 1, size: 10, total: 1, total_pages: 1 },
      request_id: "req-r",
    } as never);
  });

  it("has no header or tab row of its own; sub-views are a toolbar segment synced to ?sub=", async () => {
    render(<FuelDistributionPage />);
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
    expect(screen.queryByText("Fuel Distribution")).toBeNull();
    const views = screen.getByRole("tablist", { name: "Plan views" });
    expect(
      within(views)
        .getAllByRole("tab")
        .map((t) => t.textContent),
    ).toEqual(["Plans", "Forecasts", "Priorities", "Clusters"]);
    expect(views.closest('[role="toolbar"]')).not.toBeNull();
    fireEvent.click(within(views).getByRole("tab", { name: "Forecasts" }));
    // Each view has its own toolbar row, so query the switch again.
    expect(
      within(screen.getByRole("tablist", { name: "Plan views" })).getByRole(
        "tab",
        { name: "Forecasts" },
      ),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("Reject opens a FormDialog and keeps it open with the error on failure", async () => {
    const { rejectPlan } = jest.requireMock("../../services/fuelApi");
    rejectPlan.mockRejectedValueOnce(new Error("Plan already dispatched"));
    render(<FuelDistributionPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Reject plan plan-r" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Reject plan" });
    fireEvent.change(within(dialog).getByLabelText(/Reason/), {
      target: { value: "Wrong truck" },
    });
    await act(async () => {
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Reject plan" }),
      );
    });
    expect(rejectPlan).toHaveBeenCalledWith(
      "plan-r",
      expect.anything(),
      "dispatcher-1",
      "Wrong truck",
    );
    expect(
      await within(dialog).findByText("Plan already dispatched"),
    ).toBeInTheDocument();
    rejectPlan.mockResolvedValueOnce({ plan_id: "plan-r", status: "rejected" });
    await act(async () => {
      fireEvent.click(
        within(dialog).getByRole("button", { name: "Reject plan" }),
      );
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
