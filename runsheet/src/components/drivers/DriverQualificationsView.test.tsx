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
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/complianceApi", () => {
  const actual = jest.requireActual("../../services/complianceApi");
  return {
    ...actual,
    getDrivers: jest.fn(),
    getDriver: jest.fn(),
    getDriversDashboard: jest.fn(),
    createDriver: jest.fn(),
    updateDriver: jest.fn(),
  };
});

jest.mock("../../utils/auth", () => ({
  getCurrentUserRoles: jest.fn(),
}));

jest.mock("../../services/exportApi", () => ({
  downloadCsvExport: jest.fn(),
}));

import {
  createDriver,
  type DQFDashboard,
  getDriver,
  getDrivers,
  getDriversDashboard,
  updateDriver,
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

describe("DriverQualificationsView DQF counts", () => {
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
  it("shows total, active, suspended and expired counts on the status chips", async () => {
    render(<DriverQualificationsView />);
    expect(
      await screen.findByRole("button", { name: /All\s*9/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Active\s*4/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Suspended\s*1/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Expired\s*3/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("2 expiring within 60 days")).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("button", { name: /Suspended/ }));
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

const driver = {
  driver_id: "QA-DRV-1",
  tenant_id: "tenant-1",
  full_name: "QA Driver One",
  cdl_number: "QA123",
  cdl_state: "TX",
  cdl_class: "A",
  cdl_expiry_date: "2027-01-31",
  medical_card_expiry_date: "2026-11-15",
  hazmat_endorsement_expiry_date: null,
  tanker_endorsement_expiry_date: null,
  last_drug_test_date: null,
  last_mvr_date: null,
  status: "active",
  created_at: "",
  updated_at: "",
};

describe("DriverQualificationsView list, detail and FormDialog (3.1)", () => {
  const mockCreate = createDriver as jest.Mock;
  const mockUpdate = updateDriver as jest.Mock;
  const mockGetDriver = getDriver as jest.Mock;
  beforeEach(() => {
    jest.clearAllMocks();
    mockRoles.mockResolvedValue(["dispatcher"]);
    mockGetDrivers.mockResolvedValue({
      data: [driver],
      pagination: { page: 1, size: 20, total: 1, total_pages: 1 },
    } as never);
    mockGetDashboard.mockResolvedValue({
      data: {
        ...backendDashboard,
        drivers: [
          {
            driver_id: "QA-DRV-1",
            full_name: "QA Driver One",
            status: "active",
            qualifications: [
              {
                qualification_type: "medical_card",
                expiry_date: "2026-11-15",
                days_until_expiry: 12,
                alert_level: "urgent",
                status: "expiring_soon",
              },
              {
                qualification_type: "cdl",
                expiry_date: "2027-01-31",
                days_until_expiry: 100,
                alert_level: "ok",
                status: "valid",
              },
            ],
          },
        ],
      },
    } as never);
  });

  it("shows calendar dates on their day and the qualification alerts", async () => {
    render(<DriverQualificationsView />);
    const row = (await screen.findByText("QA Driver One")).closest(
      "tr",
    ) as HTMLElement;
    expect(row).toHaveTextContent("Sun 31 Jan 2027");
    expect(await screen.findByText("Medical card · 12 d")).toBeInTheDocument();
    expect(screen.queryByText(/^CDL ·/)).toBeNull();
  });

  it("opens the detail drawer from the row", async () => {
    mockGetDriver.mockResolvedValue({ data: driver });
    render(<DriverQualificationsView />);
    fireEvent.click(await screen.findByText("QA Driver One"));
    const drawer = await screen.findByRole("dialog", { name: "QA Driver One" });
    expect(drawer).toHaveTextContent("Sun 15 Nov 2026");
  });

  it("adds a driver through the sectioned FormDialog with validation", async () => {
    mockCreate.mockResolvedValue({ data: driver });
    render(<DriverQualificationsView />);
    await screen.findByText("QA Driver One");
    fireEvent.click(screen.getByRole("button", { name: "Add driver" }));
    const dialog = await screen.findByRole("dialog", { name: "Add driver" });
    expect(
      screen.getByRole("navigation", { name: "Sections" }),
    ).toHaveTextContent(/Identity.*CDL.*Medical.*Endorsements/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Add driver" }));
    expect(
      await screen.findByText("Enter the driver's name."),
    ).toBeInTheDocument();
    expect(mockCreate).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/Full name/), {
      target: { value: "QA Driver Two" },
    });
    fireEvent.change(screen.getByLabelText(/CDL number/), {
      target: { value: "QA999" },
    });
    fireEvent.change(screen.getByLabelText(/CDL state/), {
      target: { value: "ok" },
    });
    fireEvent.change(screen.getByLabelText(/CDL expiry/), {
      target: { value: "2028-02-01" },
    });
    fireEvent.change(screen.getByLabelText(/Medical card expiry/), {
      target: { value: "2027-03-01" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add driver" }));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          full_name: "QA Driver Two",
          cdl_state: "OK",
          cdl_expiry_date: "2028-02-01",
          hazmat_endorsement_expiry_date: null,
        }),
      ),
    );
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
  });

  it("edits a driver from the row menu", async () => {
    mockUpdate.mockResolvedValue({ data: driver });
    render(<DriverQualificationsView />);
    await screen.findByText("QA Driver One");
    fireEvent.click(
      screen.getByRole("button", { name: "Actions for QA Driver One" }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Edit driver" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit driver" });
    expect(screen.getByLabelText(/Full name/)).toHaveValue("QA Driver One");
    fireEvent.change(screen.getByLabelText(/Full name/), {
      target: { value: "QA Driver Renamed" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save changes" }),
    );
    await waitFor(() =>
      expect(mockUpdate).toHaveBeenCalledWith(
        "QA-DRV-1",
        expect.objectContaining({ full_name: "QA Driver Renamed" }),
      ),
    );
  });
});
