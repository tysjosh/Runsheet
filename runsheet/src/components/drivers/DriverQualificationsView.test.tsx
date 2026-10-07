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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../../services/complianceApi", () => {
  const actual = jest.requireActual("../../services/complianceApi");
  return {
    ...actual,
    getDrivers: jest.fn(),
    getDriversDashboard: jest.fn(),
  };
});

jest.mock("../../utils/auth", () => ({
  getCurrentUserRoles: jest.fn(),
}));

jest.mock("../../services/exportApi", () => ({
  downloadCsvExport: jest.fn(),
}));

import {
  type DQFDashboard,
  getDrivers,
  getDriversDashboard,
} from "../../services/complianceApi";
import { downloadCsvExport } from "../../services/exportApi";
import { getCurrentUserRoles } from "../../utils/auth";
import DriverQualificationsView from "./DriverQualificationsView";

const mockRoles = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;
const mockDownload = downloadCsvExport as jest.MockedFunction<
  typeof downloadCsvExport
>;

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
    mockRoles.mockResolvedValue(["admin"]);
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

describe("DriverQualificationsView Export CSV (OI-57)", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockDownload.mockResolvedValue(undefined);
    mockGetDrivers.mockResolvedValue({
      data: [],
      pagination: { page: 1, size: 20, total: 0, total_pages: 1 },
    } as unknown as Awaited<ReturnType<typeof getDrivers>>);
  });

  it("shows the button to admins and sends the status filter", async () => {
    mockRoles.mockResolvedValue(["admin"]);
    render(<DriverQualificationsView />);
    const button = (await screen.findByText("Export CSV")).closest("button");
    if (!button) throw new Error("no Export CSV button");
    expect(button).toHaveTextContent("Export CSV: driver qualifications");
    fireEvent.change(screen.getByLabelText("Status"), {
      target: { value: "suspended" },
    });
    fireEvent.click(button);
    await waitFor(() =>
      expect(mockDownload).toHaveBeenCalledWith("driver_qualifications", {
        status: "suspended",
      }),
    );
  });

  it("sends no status when the filter is All", async () => {
    mockRoles.mockResolvedValue(["admin"]);
    render(<DriverQualificationsView />);
    const button = (await screen.findByText("Export CSV")).closest("button");
    if (!button) throw new Error("no Export CSV button");
    fireEvent.click(button);
    await waitFor(() =>
      expect(mockDownload).toHaveBeenCalledWith("driver_qualifications", {
        status: undefined,
      }),
    );
  });

  it("hides the button from dispatchers", async () => {
    mockRoles.mockResolvedValue(["dispatcher"]);
    render(<DriverQualificationsView />);
    await waitFor(() => expect(mockRoles).toHaveBeenCalled());
    await waitFor(() => expect(mockGetDrivers).toHaveBeenCalled());
    expect(screen.queryByText("Export CSV")).not.toBeInTheDocument();
  });
});
