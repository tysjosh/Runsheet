/**
 * Plans view dialogs on FormDialog (Phase 3 review finding: the station-bug
 * class in FuelDistributionPage): Emergency stop picks the product by name
 * and takes whole gallons; Cost configuration uses NumberFields.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/dashboard/dispatch",
}));
jest.mock("../../services/tenant", () => ({
  ...jest.requireActual("../../services/tenant"),
  getCurrentTenantId: () => "dev-tenant",
}));
jest.mock("../../services/fuelApi", () => ({
  ...jest.requireActual("../../services/fuelApi"),
  listDeliveryDestinations: jest.fn(),
  insertEmergencyStop: jest.fn(),
  updateCostConfig: jest.fn(),
}));

import {
  insertEmergencyStop,
  listDeliveryDestinations,
  updateCostConfig,
} from "../../services/fuelApi";
import { CostConfigPanel, EmergencyStopModal } from "./FuelDistributionPage";

beforeEach(() => {
  jest.clearAllMocks();
  (listDeliveryDestinations as jest.Mock).mockResolvedValue({
    items: [
      {
        destination_type: "retail_station",
        destination_id: "STN-042",
        name: "QA Station 42",
      },
    ],
    total: 1,
  });
});

describe("EmergencyStopModal", () => {
  it("picks the product by name, takes whole gallons and sends the catalog code", async () => {
    (insertEmergencyStop as jest.Mock).mockResolvedValue({ inserted: true });
    const onSuccess = jest.fn();
    render(
      <EmergencyStopModal
        routeId="R-1"
        onClose={jest.fn()}
        onSuccess={onSuccess}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "Emergency stop" });
    expect(within(dialog).queryByPlaceholderText(/DIESEL_2/)).toBeNull();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Insert stop" }),
    );
    expect(
      await within(dialog).findByText("Pick a product."),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText("Requested gallons must be greater than zero."),
    ).toBeInTheDocument();

    const station = await within(dialog).findByRole("option", {
      name: "QA Station 42",
    });
    fireEvent.change(station.closest("select") as HTMLSelectElement, {
      target: { value: "STN-042" },
    });
    fireEvent.click(within(dialog).getByRole("combobox", { name: /^Product/ }));
    fireEvent.click(await screen.findByRole("option", { name: /Propane/ }));
    const gallons = within(dialog).getByLabelText(/^Requested gallons/);
    expect(gallons).toHaveAttribute("type", "text");
    fireEvent.change(gallons, { target: { value: "1,250" } });
    fireEvent.change(within(dialog).getByLabelText(/^Priority reason/), {
      target: { value: "Hospital generator" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Insert stop" }),
    );
    await waitFor(() =>
      expect(insertEmergencyStop).toHaveBeenCalledWith("R-1", {
        fuel_grade: "PROPANE",
        requested_gallons: 1250,
        priority_reason: "Hospital generator",
        station_id: "STN-042",
      }),
    );
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  });

  it("shows a 409 reason code as readable text and stays open", async () => {
    (insertEmergencyStop as jest.Mock).mockRejectedValue(
      new Error("capacity_insufficient"),
    );
    render(
      <EmergencyStopModal
        routeId="R-1"
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "Emergency stop" });
    const station = await within(dialog).findByRole("option", {
      name: "QA Station 42",
    });
    fireEvent.change(station.closest("select") as HTMLSelectElement, {
      target: { value: "STN-042" },
    });
    fireEvent.click(within(dialog).getByRole("combobox", { name: /^Product/ }));
    fireEvent.click(await screen.findByRole("option", { name: /Kerosene/ }));
    fireEvent.change(within(dialog).getByLabelText(/^Requested gallons/), {
      target: { value: "200" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Priority reason/), {
      target: { value: "Urgent" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Insert stop" }),
    );
    expect(
      await within(dialog).findByText(/No compartment capacity/),
    ).toBeInTheDocument();
  });
});

describe("CostConfigPanel", () => {
  it("saves NumberField values, not type=number strings", async () => {
    (updateCostConfig as jest.Mock).mockResolvedValue({});
    const addToast = jest.fn();
    render(
      <CostConfigPanel
        onClose={jest.fn()}
        onSave={jest.fn()}
        addToast={addToast}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "Cost configuration" });
    const rate = within(dialog).getByLabelText(/^Driver hourly rate/);
    expect(rate).toHaveAttribute("type", "text");
    expect(rate).toHaveValue("25.00");
    fireEvent.change(rate, { target: { value: "31.5" } });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save configuration" }),
    );
    await waitFor(() =>
      expect(updateCostConfig).toHaveBeenCalledWith(
        "dev-tenant",
        expect.objectContaining({ driver_hourly_rate: 31.5, currency: "USD" }),
      ),
    );
    await waitFor(() =>
      expect(addToast).toHaveBeenCalledWith(
        "Cost configuration saved",
        "success",
      ),
    );
  });
});
