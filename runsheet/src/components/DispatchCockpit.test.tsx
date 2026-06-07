/**
 * Tests for DispatchCockpit — the dispatcher "Today" landing.
 *
 * Pins the behaviours the cockpit promises: it aggregates work from several
 * sources (orders, delayed jobs, fuel alerts, approvals), renders prioritized
 * attention rows, and deep-links each item to where it's acted on. Every data
 * source loads independently and fails open.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockPush = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

jest.mock("../services/ordersApi", () => ({
  listOrders: jest.fn(),
  assignDriver: jest.fn(),
  releaseHoldOrder: jest.fn(),
}));
jest.mock("../services/schedulingApi", () => ({
  getDelayedJobs: jest.fn(),
}));
jest.mock("../services/fuelApi", () => ({
  getAlerts: jest.fn(),
}));
jest.mock("../services/agentApi", () => ({
  getApprovals: jest.fn(),
}));
jest.mock("../services/tenant", () => ({
  getCurrentTenantId: () => "tenant-a",
}));
// DriverPicker (used by the inline assign action) loads the roster.
jest.mock("../services/complianceApi", () => ({
  getDrivers: jest.fn().mockResolvedValue({
    data: [
      {
        driver_id: "DRV-1",
        full_name: "Ada Lovelace",
        cdl_class: "A",
        status: "active",
      },
    ],
    page: 1,
    size: 200,
    total: 1,
    request_id: "d1",
  }),
}));

import { getApprovals } from "../services/agentApi";
import { getAlerts as getFuelAlerts } from "../services/fuelApi";
import {
  assignDriver,
  listOrders,
  releaseHoldOrder,
} from "../services/ordersApi";
import { getDelayedJobs } from "../services/schedulingApi";
import DispatchCockpit from "./DispatchCockpit";

const mockListOrders = listOrders as jest.MockedFunction<typeof listOrders>;
const mockAssignDriver = assignDriver as jest.MockedFunction<
  typeof assignDriver
>;
const mockReleaseHold = releaseHoldOrder as jest.MockedFunction<
  typeof releaseHoldOrder
>;
const mockGetDelayedJobs = getDelayedJobs as jest.MockedFunction<
  typeof getDelayedJobs
>;
const mockGetFuelAlerts = getFuelAlerts as jest.MockedFunction<
  typeof getFuelAlerts
>;
const mockGetApprovals = getApprovals as jest.MockedFunction<
  typeof getApprovals
>;

function paginated<T>(data: T[]) {
  return {
    data,
    pagination: { page: 1, size: 6, total: data.length, total_pages: 1 },
    request_id: "r1",
  } as unknown as Awaited<ReturnType<typeof listOrders>>;
}

beforeEach(() => {
  mockListOrders.mockReset();
  mockAssignDriver.mockReset();
  mockReleaseHold.mockReset();
  mockGetDelayedJobs.mockReset();
  mockGetFuelAlerts.mockReset();
  mockGetApprovals.mockReset();
  mockPush.mockReset();

  // Default: empty everything; individual tests override.
  mockListOrders.mockResolvedValue(paginated([]));
  mockGetDelayedJobs.mockResolvedValue({
    data: [],
  } as unknown as Awaited<ReturnType<typeof getDelayedJobs>>);
  mockGetFuelAlerts.mockResolvedValue({
    data: [],
  } as unknown as Awaited<ReturnType<typeof getFuelAlerts>>);
  mockGetApprovals.mockResolvedValue({
    entries: [],
  } as unknown as Awaited<ReturnType<typeof getApprovals>>);
});

it("renders placed orders as attention rows and deep-links to the detail page", async () => {
  mockListOrders.mockImplementation((filters) =>
    Promise.resolve(
      filters?.status === "placed"
        ? paginated([
            {
              order_id: "ord_1",
              customer_id: "CUST-1",
              customer_name: "Acme Fuel Co",
              ship_to_address: "1 Main St",
              status: "placed",
              created_at: new Date().toISOString(),
            },
          ] as never)
        : paginated([]),
    ),
  );

  render(<DispatchCockpit />);

  expect(await screen.findByText("Acme Fuel Co")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Acme Fuel Co"));
  expect(mockPush).toHaveBeenCalledWith("/orders/ord_1");
});

it("shows delayed jobs and navigates to Dispatch via onNavigate", async () => {
  mockGetDelayedJobs.mockResolvedValue({
    data: [
      {
        job_id: "JOB-9",
        origin: "Houston",
        destination: "Dallas",
        delay_duration_minutes: 90,
        status: "in_progress",
      },
    ],
  } as unknown as Awaited<ReturnType<typeof getDelayedJobs>>);

  const onNavigate = jest.fn();
  render(<DispatchCockpit onNavigate={onNavigate} />);

  expect(await screen.findByText("JOB-9")).toBeInTheDocument();
  expect(screen.getByText("+1h 30m")).toBeInTheDocument();
  fireEvent.click(screen.getByText("JOB-9"));
  expect(onNavigate).toHaveBeenCalledWith("dispatch");
});

it("shows fuel alerts with status badges", async () => {
  mockGetFuelAlerts.mockResolvedValue({
    data: [
      {
        station_id: "STN-1",
        name: "Houston Terminal",
        status: "critical",
        stock_percentage: 8,
        location_name: "Houston, TX",
      },
    ],
  } as unknown as Awaited<ReturnType<typeof getFuelAlerts>>);

  render(<DispatchCockpit />);

  expect(await screen.findByText("Houston Terminal")).toBeInTheDocument();
  expect(screen.getByText("critical")).toBeInTheDocument();
});

it("surfaces the pending agent-proposal count", async () => {
  mockGetApprovals.mockResolvedValue({
    entries: [{ action_id: "a1" }, { action_id: "a2" }],
  } as unknown as Awaited<ReturnType<typeof getApprovals>>);

  render(<DispatchCockpit />);

  expect(
    await screen.findByText(/2 proposals awaiting review/i),
  ).toBeInTheDocument();
});

it("renders empty states and stays usable when a source fails", async () => {
  mockGetFuelAlerts.mockRejectedValue(new Error("boom"));

  render(<DispatchCockpit />);

  await waitFor(() => expect(mockListOrders).toHaveBeenCalled());
  expect(await screen.findByText("No orders waiting")).toBeInTheDocument();
  expect(screen.getByText("All stations healthy")).toBeInTheDocument();
});

it("assigns a driver inline and removes the order from the list", async () => {
  mockListOrders.mockImplementation((filters) =>
    Promise.resolve(
      filters?.status === "placed"
        ? paginated([
            {
              order_id: "ord_1",
              customer_id: "CUST-1",
              customer_name: "Acme Fuel Co",
              ship_to_address: "1 Main St",
              status: "placed",
              assigned_driver_id: null,
              created_at: new Date().toISOString(),
            },
          ] as never)
        : paginated([]),
    ),
  );
  mockAssignDriver.mockResolvedValue({
    data: { order_id: "ord_1", status: "scheduled" },
    request_id: "r1",
  } as unknown as Awaited<ReturnType<typeof assignDriver>>);

  render(<DispatchCockpit />);

  // Open the inline assign panel, pick a driver, confirm.
  fireEvent.click(await screen.findByText("Assign"));
  fireEvent.click(await screen.findByLabelText("Driver"));
  fireEvent.click(await screen.findByText("Ada Lovelace"));
  fireEvent.click(screen.getByText("Confirm"));

  await waitFor(() =>
    expect(mockAssignDriver).toHaveBeenCalledWith("ord_1", {
      driver_id: "DRV-1",
    }),
  );
  // Row is removed after a successful assignment.
  await waitFor(() =>
    expect(screen.queryByText("Acme Fuel Co")).not.toBeInTheDocument(),
  );
});

it("releases a hold inline", async () => {
  mockListOrders.mockImplementation((filters) =>
    Promise.resolve(
      filters?.status === "on_hold"
        ? paginated([
            {
              order_id: "ord_h",
              customer_id: "CUST-2",
              customer_name: "Beta Corp",
              ship_to_address: "2 Oak Ave",
              status: "on_hold",
              created_at: new Date().toISOString(),
            },
          ] as never)
        : paginated([]),
    ),
  );
  mockReleaseHold.mockResolvedValue({
    data: { order_id: "ord_h", status: "placed" },
    request_id: "r1",
  } as unknown as Awaited<ReturnType<typeof releaseHoldOrder>>);

  render(<DispatchCockpit />);

  fireEvent.click(await screen.findByText("Release"));

  await waitFor(() => expect(mockReleaseHold).toHaveBeenCalledWith("ord_h"));
  expect(await screen.findByText("Hold released")).toBeInTheDocument();
});
