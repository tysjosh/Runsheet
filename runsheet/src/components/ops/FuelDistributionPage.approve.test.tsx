/**
 * OI-16: approving a plan that still has `placed` orders shows which orders
 * to confirm.
 *
 * Kept in its own file (the Clusters-tab suite in FuelDistributionPage.test.tsx
 * mocks a different set of fuelApi calls).
 */
import { fireEvent, render, screen } from "@testing-library/react";

jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    __esModule: true,
    ...actual,
    listPriorityClusters: jest.fn(),
    listCombinableGroups: jest.fn(),
    listPlans: jest.fn(),
    listDeliveryDestinations: jest.fn(),
    approvePlan: jest.fn(),
  };
});

jest.mock("../../services/tenant", () => ({
  getCurrentTenantId: () => "tenant-1",
}));

jest.mock("../../utils/auth", () => ({
  getCurrentUserId: jest.fn(),
}));

jest.mock("./StormModeBanner", () => ({
  __esModule: true,
  default: () => null,
}));

jest.mock("../../hooks/usePlanExecutionSocket", () => {
  const socket = () => ({
    state: "disconnected",
    isConnected: false,
    reconnectAttempt: 0,
    reconnectDelay: 0,
    lastUpdate: null,
    error: null,
    connect: jest.fn(),
    disconnect: jest.fn(),
    connectionStatus: null,
  });
  return { __esModule: true, usePlanExecutionSocket: socket, default: socket };
});

import { ApiError } from "../../services/api";
import {
  approvePlan,
  listDeliveryDestinations,
  listPlans,
} from "../../services/fuelApi";
import { getCurrentUserId } from "../../utils/auth";
import FuelDistributionPage from "./FuelDistributionPage";

const mockListPlans = listPlans as unknown as jest.Mock;
const mockApprove = approvePlan as jest.MockedFunction<typeof approvePlan>;
const mockUserId = getCurrentUserId as jest.MockedFunction<
  typeof getCurrentUserId
>;
const mockDestinations = listDeliveryDestinations as unknown as jest.Mock;

beforeEach(() => {
  jest.clearAllMocks();
  mockUserId.mockResolvedValue("dispatcher-1");
  mockDestinations.mockResolvedValue({ data: [], total: 0 });
  mockListPlans.mockResolvedValue({
    data: [
      {
        plan_id: "plan-1",
        run_id: "run-1",
        status: "proposed",
        truck_id: "truck-1",
        created_at: "2026-10-07T12:00:00Z",
        total_utilization_pct: 80,
        execution_status: null,
      },
    ],
    pagination: { page: 1, size: 10, total: 1, total_pages: 1 },
    request_id: "req-1",
  });
});

it("names the placed orders when dispatch refuses them", async () => {
  mockApprove.mockRejectedValue(
    new ApiError(
      "Confirm these orders before dispatching: ORD-1, ORD-2",
      409,
      "ORDERS_NOT_CONFIRMED",
      { order_ids: ["ORD-1", "ORD-2"], plan_id: "plan-1" },
    ),
  );
  render(<FuelDistributionPage />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Approve plan plan-1" }),
  );
  expect(
    await screen.findByText(
      "Confirm these orders before dispatching: ORD-1, ORD-2",
    ),
  ).toBeInTheDocument();
  expect(mockApprove).toHaveBeenCalledWith(
    "plan-1",
    "tenant-1",
    "dispatcher-1",
  );
});

it("keeps the server message for other approve failures", async () => {
  mockApprove.mockRejectedValue(
    new ApiError(
      "Plan plan-1 cannot be dispatched",
      409,
      "PLAN_NOT_APPROVABLE",
    ),
  );
  render(<FuelDistributionPage />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Approve plan plan-1" }),
  );
  expect(
    await screen.findByText("Plan plan-1 cannot be dispatched"),
  ).toBeInTheDocument();
});
