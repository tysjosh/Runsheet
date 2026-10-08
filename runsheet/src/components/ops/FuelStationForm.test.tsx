/**
 * FuelStationForm (UI revamp task 3.2): the station dialog on FormDialog,
 * with the two owner-reported bugs fixed:
 *  - a litre-based capacity showed "5283,441047162968" (now "5,283" gal)
 *  - Fuel Type read "GASOLINE_REG (Regular U…" (now cap + name, code secondary)
 */
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { configureFormat, formatConfig } from "../../lib/format";
import type { FuelStation } from "../../services/fuelApi";
import FuelStationForm, { validateStationForm } from "./FuelStationForm";

jest.mock("../../services/tenant", () => ({
  getCurrentTenantId: () => "tenant-1",
}));
jest.mock("../../services/fuelApi", () => {
  const actual = jest.requireActual("../../services/fuelApi");
  return {
    ...actual,
    createStation: jest.fn(),
    updateStation: jest.fn(),
    updateStationThreshold: jest.fn(),
  };
});

import {
  createStation,
  updateStation,
  updateStationThreshold,
} from "../../services/fuelApi";

const mockCreate = createStation as jest.Mock;
const mockUpdate = updateStation as jest.Mock;
const mockThreshold = updateStationThreshold as jest.Mock;

function station(overrides: Partial<FuelStation> = {}): FuelStation {
  return {
    station_id: "FS-1",
    name: "QA North Depot",
    fuel_type: "GASOLINE_REG",
    capacity_liters: 20_000,
    current_stock_liters: 10_000,
    daily_consumption_rate: 0,
    days_until_empty: 0,
    alert_threshold_pct: 20,
    status: "normal",
    location_name: "North",
    ...overrides,
  } as FuelStation;
}

const original = {
  locale: formatConfig().locale,
  timeZone: formatConfig().timeZone,
};
afterEach(() => {
  configureFormat(original);
  jest.clearAllMocks();
});

describe("FuelStationForm", () => {
  it("shows a 20,000 L station's capacity as whole gallons (5,283 gal)", () => {
    render(
      <FuelStationForm
        mode="edit"
        station={station()}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />,
    );
    const cap = screen.getByLabelText(/^Capacity/) as HTMLInputElement;
    expect(cap.value).toBe("5,283");
    expect(cap).not.toHaveAttribute("type", "number");
    expect(screen.queryByDisplayValue(/5283[.,]44/)).toBeNull();
  });

  it("formats and parses capacity in the browser locale (de-DE)", async () => {
    configureFormat({ ...original, locale: "de-DE" });
    const onSuccess = jest.fn();
    mockUpdate.mockResolvedValue(station());
    render(
      <FuelStationForm
        mode="edit"
        station={station()}
        onClose={jest.fn()}
        onSuccess={onSuccess}
      />,
    );
    const cap = screen.getByLabelText(/^Capacity/) as HTMLInputElement;
    expect(cap.value).toBe("5.283");
    fireEvent.change(cap, { target: { value: "6.000" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    });
    expect(mockUpdate).toHaveBeenCalledWith(
      "FS-1",
      expect.objectContaining({ capacity_gallons: 6000 }),
      "tenant-1",
    );
  });

  it("shows the product cap and readable name, with the code as secondary text", () => {
    render(
      <FuelStationForm
        mode="edit"
        station={station()}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />,
    );
    const trigger = screen.getByRole("combobox", { name: /^Fuel type/ });
    expect(trigger).toHaveTextContent("Regular unleaded");
    expect(trigger).not.toHaveTextContent("GASOLINE_REG (");
    fireEvent.click(trigger);
    const regular = within(screen.getByRole("listbox")).getByRole("option", {
      name: /Regular unleaded/,
    });
    expect(regular).toHaveTextContent("GASOLINE_REG");
  });

  it("uses the threshold endpoint when only the threshold changed", async () => {
    mockThreshold.mockResolvedValue(station({ alert_threshold_pct: 30 }));
    render(
      <FuelStationForm
        mode="edit"
        station={station()}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/^Alert threshold/), {
      target: { value: "30" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    });
    expect(mockThreshold).toHaveBeenCalledWith("FS-1", 30, "tenant-1");
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it("does not resend an unchanged (rounded) capacity on a full edit", async () => {
    mockUpdate.mockResolvedValue(station());
    render(
      <FuelStationForm
        mode="edit"
        station={station()}
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/^Station name/), {
      target: { value: "QA North Depot 2" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    });
    const payload = mockUpdate.mock.calls[0][1];
    expect(payload).not.toHaveProperty("capacity_gallons");
    expect(payload.name).toBe("QA North Depot 2");
  });

  it("creates a station with whole-gallon numbers", async () => {
    mockCreate.mockResolvedValue(station());
    const onSuccess = jest.fn();
    render(
      <FuelStationForm
        mode="create"
        onClose={jest.fn()}
        onSuccess={onSuccess}
      />,
    );
    expect(
      screen.getByRole("heading", { name: "Add fuel station" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/^Station name/), {
      target: { value: "QA South" },
    });
    fireEvent.change(screen.getByLabelText(/^Capacity/), {
      target: { value: "12,000" },
    });
    fireEvent.change(screen.getByLabelText(/^Initial stock/), {
      target: { value: "8,000" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create station" }));
    });
    expect(mockCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "QA South",
        fuel_type: "DIESEL_2",
        capacity_gallons: 12000,
        initial_stock_gallons: 8000,
        alert_threshold_pct: 20,
      }),
      "tenant-1",
    );
    expect(onSuccess).toHaveBeenCalled();
  });

  it("shows inline errors and keeps the dialog open on invalid input", async () => {
    render(
      <FuelStationForm
        mode="create"
        onClose={jest.fn()}
        onSuccess={jest.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/^Alert threshold/), {
      target: { value: "120" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create station" }));
    });
    expect(screen.getByText("Station name is required.")).toBeInTheDocument();
    expect(
      screen.getByText("Capacity must be a positive number."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Threshold must be between 0 and 100."),
    ).toBeInTheDocument();
    expect(mockCreate).not.toHaveBeenCalled();
  });
});

describe("validateStationForm", () => {
  const ok = {
    name: "A",
    fuel_type: "DIESEL_2" as const,
    capacity_gallons: 100,
    initial_stock_gallons: 10,
    location_name: "",
    alert_threshold_pct: 20,
  };
  it("accepts valid values", () => {
    expect(validateStationForm(ok)).toEqual({});
  });
  it("rejects stock above capacity", () => {
    expect(
      validateStationForm({ ...ok, initial_stock_gallons: 101 })
        .initial_stock_gallons,
    ).toMatch(/at most/);
  });
});
