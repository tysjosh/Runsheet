/**
 * Regression test for the DQF dashboard stats (C10).
 *
 * The page used to read `total_active` / `total_suspended` /
 * `total_expiring_soon`, which the backend never sends, so every stat
 * rendered 0. It now reads the backend's field names and shows expired
 * drivers separately from expiring ones.
 */

import { fireEvent, render, screen } from "@testing-library/react";

jest.mock("../../services/complianceApi", () => {
  const actual = jest.requireActual("../../services/complianceApi");
  return {
    ...actual,
    getDrivers: jest.fn(),
    getDriversDashboard: jest.fn(),
  };
});

import {
  type DQFDashboard,
  getDrivers,
  getDriversDashboard,
} from "../../services/complianceApi";
import DriverQualificationsView from "./DriverQualificationsView";

const mockGetDrivers = getDrivers as jest.MockedFunction<typeof getDrivers>;
const mockGetDashboard = getDriversDashboard as jest.MockedFunction<
  typeof getDriversDashboard
>;

/** Mirrors the backend `DQFDashboard` payload. */
function dashboardFixture(): DQFDashboard {
  return {
    tenant_id: "demo-tenant",
    total_drivers: 7,
    active_drivers: 4,
    suspended_drivers: 1,
    expired_drivers: 2,
    expiring_within_60_days: 3,
    expiring_within_30_days: 1,
    expiring_within_7_days: 0,
    drug_test_overdue: 0,
    drivers: [],
  };
}

afterEach(() => {
  jest.clearAllMocks();
});

describe("DriverQualificationsView — DQF dashboard stats", () => {
  it("renders backend counts with expired separate from expiring", async () => {
    mockGetDrivers.mockResolvedValue({
      data: [],
      request_id: "drivers",
    } as unknown as Awaited<ReturnType<typeof getDrivers>>);
    mockGetDashboard.mockResolvedValue({
      data: dashboardFixture(),
      request_id: "dash",
    });

    render(<DriverQualificationsView />);
    fireEvent.click(
      await screen.findByRole("button", { name: /DQF Dashboard/i }),
    );

    const expiredLabel = await screen.findByText("Expired");
    const expiringLabel = screen.getByText("Expiring ≤30 days");
    const activeLabel = screen.getByText("Active Drivers");
    const suspendedLabel = screen.getByText("Suspended");

    // Each stat's value is rendered alongside its label in the same card.
    expect(expiredLabel.parentElement).toHaveTextContent("2");
    expect(expiringLabel.parentElement).toHaveTextContent("1");
    expect(activeLabel.parentElement).toHaveTextContent("4");
    expect(suspendedLabel.parentElement).toHaveTextContent("1");
  });
});
