/**
 * Load-failure states for CustomerTankDetailPage (F-1).
 */
import { fireEvent, render, screen } from "@testing-library/react";

const mockBack = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), back: mockBack }),
}));

jest.mock("../../services/fuelApi", () => ({
  getCustomerTankWithLinks: jest.fn(),
}));

import { ApiError } from "../../services/api";
import { getCustomerTankWithLinks } from "../../services/fuelApi";
import CustomerTankDetailPage from "./CustomerTankDetailPage";

const mockGetTank = getCustomerTankWithLinks as jest.MockedFunction<
  typeof getCustomerTankWithLinks
>;

describe("CustomerTankDetailPage load failures", () => {
  beforeEach(() => jest.clearAllMocks());

  it("shows not found with back and home links on 404", async () => {
    mockGetTank.mockRejectedValue(new ApiError("Customer tank not found", 404));

    render(<CustomerTankDetailPage customerTankId="CT-missing" />);

    expect(
      await screen.findByRole("heading", { name: "Customer tank not found" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/customer tank "CT-missing"/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Go back/ }));
    expect(mockBack).toHaveBeenCalled();
    expect(
      screen.getByRole("link", { name: "Go to Customers" }),
    ).toHaveAttribute("href", "/dashboard/customers");
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("shows the error banner and retries on 500", async () => {
    mockGetTank.mockRejectedValue(new ApiError("boom", 500));

    render(<CustomerTankDetailPage customerTankId="CT-1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(mockGetTank).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });
});

describe("CustomerTankDetailPage content (UI revamp 3.3)", () => {
  beforeEach(() => jest.clearAllMocks());

  it("shows the product by name with its code, and whole gallons", async () => {
    mockGetTank.mockResolvedValue({
      customer_tank_id: "QA-CT-1",
      tenant_id: "t",
      customer_id: "QA-CUST-1",
      customer_type: "commercial",
      fuel_type: "diesel",
      fuel_product_code: "DIESEL_2",
      capacity_gallons: 5283.441047162968,
      current_level_gallons: 1200.6,
      last_reading_at: "2026-10-08T13:30:00Z",
      location_lat: 29.7604267,
      location_lon: -95.3698028,
      zip_code: "77002",
      k_factor: 1.00234,
      use_case: "fleet_fueling",
      status: "active",
      links: {},
    } as never);
    render(<CustomerTankDetailPage customerTankId="QA-CT-1" />);
    expect(
      await screen.findByRole("heading", { level: 1, name: "Customer tank" }),
    ).toBeInTheDocument();
    expect(screen.getByText("5,283 gal")).toBeInTheDocument();
    expect(screen.getByText("1,201 gal")).toBeInTheDocument();
    expect(screen.getByText("23%")).toBeInTheDocument();
    // Product: readable name, code only as secondary text.
    expect(screen.getAllByText("Diesel #2 (on-road)").length).toBeGreaterThan(
      0,
    );
    expect(screen.getByText("DIESEL_2")).toBeInTheDocument();
    expect(screen.queryByText("diesel")).toBeNull();
    expect(screen.getByText("Commercial")).toBeInTheDocument();
    expect(screen.getByText("Fleet fueling")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText(/29\.76043, -95\.36980/)).toBeInTheDocument();
    expect(screen.queryByText(/5283\.44|5283,44/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(mockBack).toHaveBeenCalled();
  });
});
