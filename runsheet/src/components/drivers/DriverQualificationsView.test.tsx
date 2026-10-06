/**
 * Regression test for C10: the DQF dashboard cards.
 *
 * ``GET /compliance/drivers/dashboard`` returns ``active_drivers``,
 * ``suspended_drivers``, ``expired_drivers`` and ``expiring_drivers`` (see
 * ``DQFDashboard`` in ``compliance/services/driver_qualification_service.py``).
 * An earlier client type read ``total_active`` / ``total_suspended`` /
 * ``total_expiring_soon``, which the backend never sends, so every card
 * showed 0. These tests pin the backend shape.
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

// Exactly the fields the backend model emits (plus generated_at).
const backendDashboard = {
  tenant_id: "tenant-1",
  total_drivers: 9,
  active_drivers: 4,
  suspended_drivers: 1,
  expired_drivers: 3,
  expiring_within_60_days: 2,
  expiring_within_30_days: 2,
  expiring_within_7_days: 1,
  drug_test_overdue: 0,
  expiring_drivers: 2,
  drivers: [],
  generated_at: "2026-10-05T00:00:00Z",
} as DQFDashboard;

function statValue(label: string): string | null {
  const labelEl = screen.getByText(label);
  // StatsBar grid: <div><div>{value}</div><div>{label}</div></div>
  return labelEl.previousElementSibling?.textContent ?? null;
}

describe("DriverQualificationsView DQF dashboard", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetDrivers.mockResolvedValue({
      data: [],
      pagination: { page: 1, size: 20, total: 0, total_pages: 1 },
    } as unknown as Awaited<ReturnType<typeof getDrivers>>);
    mockGetDashboard.mockResolvedValue({
      data: backendDashboard,
    } as Awaited<ReturnType<typeof getDriversDashboard>>);
  });

  it("shows active, suspended, expired and expiring counts from the backend payload", async () => {
    render(<DriverQualificationsView />);
    fireEvent.click(screen.getByRole("button", { name: "DQF Dashboard" }));

    await screen.findByText("Active Drivers");
    expect(statValue("Active Drivers")).toBe("4");
    expect(statValue("Suspended")).toBe("1");
    expect(statValue("Expired")).toBe("3");
    expect(statValue("Expiring Soon")).toBe("2");
  });
});
