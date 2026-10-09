/**
 * Compliance → Meters (task 3.5): DataTable with calibration badges, audit
 * trail in a Drawer with whole formatted gallons, Register meter FormDialog.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/complianceApi", () => ({
  ...jest.requireActual("../../services/complianceApi"),
  getMeters: jest.fn(),
  getMeterAuditTrail: jest.fn(),
  createMeter: jest.fn(),
}));
jest.mock("../../services/api", () => ({
  apiService: { getAssets: jest.fn() },
}));

import { apiService } from "../../services/api";
import {
  createMeter,
  getMeterAuditTrail,
  getMeters,
} from "../../services/complianceApi";
import MeterAuditPage, { calibrationStatus } from "./MeterAuditPage";

const meter = {
  meter_id: "m1",
  meter_number: "MTR-001",
  truck_id: "TRUCK-001",
  calibration_certificate_number: "CAL-1",
  calibration_date: "2026-01-01",
  calibration_expiry_date: "2027-01-01",
  weights_measures_authority: "TX Ag",
};

beforeEach(() => {
  jest.clearAllMocks();
  (getMeters as jest.Mock).mockResolvedValue({
    data: [meter],
    pagination: { total_pages: 1 },
  });
  (getMeterAuditTrail as jest.Mock).mockResolvedValue({
    data: [
      {
        audit_id: "a1",
        delivery_id: "DEL-1",
        invoice_id: "INV-1",
        gross_gallons: 5283.4410471,
        net_gallons: 5200,
        variance_flag: "net_exceeds_gross",
        timestamp: "2026-10-01T12:00:00Z",
      },
    ],
    pagination: { total_pages: 1 },
  });
  (apiService.getAssets as jest.Mock).mockResolvedValue({
    data: [{ id: "TRUCK-001", name: "Rig 1", assetType: "vehicle" }],
  });
});

it("grades calibration with an icon badge", () => {
  const now = new Date("2026-10-08T00:00:00Z");
  expect(calibrationStatus("2026-10-01", now).status).toBe("critical");
  expect(calibrationStatus("2026-10-20", now).label).toMatch(/^Expiring/);
  expect(calibrationStatus("2027-10-20", now).status).toBe("ok");
});

it("opens the audit trail in a drawer with formatted gallons and a named variance", async () => {
  render(<MeterAuditPage />);
  fireEvent.click(await screen.findByText("MTR-001"));
  const drawer = await screen.findByRole("dialog", {
    name: "Audit trail · MTR-001",
  });
  expect(await within(drawer).findByText("5,283.4 gal")).toBeInTheDocument();
  expect(within(drawer).getByText("Net exceeds gross")).toBeInTheDocument();
  expect(drawer).not.toHaveTextContent("5283.44104");
});

it("registers a meter through the FormDialog with inline validation", async () => {
  (createMeter as jest.Mock).mockResolvedValue({ data: {} });
  render(<MeterAuditPage />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Register meter" }),
  );
  const dialog = screen.getByRole("dialog", { name: "Register meter" });
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Register meter" }),
  );
  expect(await within(dialog).findByText("Pick a truck.")).toBeInTheDocument();
  expect(createMeter).not.toHaveBeenCalled();
  fireEvent.change(within(dialog).getByLabelText(/^Meter number/), {
    target: { value: "MTR-9" },
  });
  fireEvent.click(within(dialog).getByLabelText("Truck ID"));
  fireEvent.click(await screen.findByText("Rig 1"));
  fireEvent.change(within(dialog).getByLabelText(/^Calibration certificate/), {
    target: { value: "CAL-9" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Weights & Measures/), {
    target: { value: "TX Ag" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Calibration date/), {
    target: { value: "2026-01-01" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Calibration expiry/), {
    target: { value: "2027-01-01" },
  });
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Register meter" }),
  );
  await waitFor(() =>
    expect(createMeter).toHaveBeenCalledWith(
      expect.objectContaining({ meter_number: "MTR-9", truck_id: "TRUCK-001" }),
    ),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Register meter" })).toBeNull(),
  );
});
