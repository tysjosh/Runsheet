/**
 * Load-failure states for DepotDetailPage (OI-48), following the Terminal
 * and customer-tank detail page tests.
 */
import { fireEvent, render, screen } from "@testing-library/react";

const mockBack = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), back: mockBack }),
}));

jest.mock("../../services/fuelApi", () => ({
  getDepot: jest.fn(),
}));

import { ApiError } from "../../services/api";
import { getDepot } from "../../services/fuelApi";
import DepotDetailPage from "./DepotDetailPage";

const mockGetDepot = getDepot as jest.MockedFunction<typeof getDepot>;

const depot = {
  depot: {
    depot_id: "DEP-1",
    tenant_id: "t1",
    name: "North Yard",
    location_lat: 29.7,
    location_lon: -95.3,
    address: "1 Yard Rd",
    timezone: "America/Chicago",
    status: "active" as const,
    is_default: true,
  },
  assigned_assets: [],
};

describe("DepotDetailPage load failures", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("shows not found with a working back button on 404", async () => {
    mockGetDepot.mockRejectedValue(
      new ApiError("Depot not found", 404, "depot_not_found"),
    );
    render(<DepotDetailPage depotId="DEP-missing" />);
    expect(
      await screen.findByRole("heading", { name: "Depot not found" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/depot "DEP-missing"/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Go back/ }));
    expect(mockBack).toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Go to Setup" })).toHaveAttribute(
      "href",
      "/dashboard/settings?tab=company",
    );
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("uses the in-shell onBack when provided", async () => {
    const onBack = jest.fn();
    mockGetDepot.mockRejectedValue(new ApiError("nope", 404));
    render(<DepotDetailPage depotId="DEP-missing" onBack={onBack} />);
    fireEvent.click(await screen.findByRole("button", { name: /Go back/ }));
    expect(onBack).toHaveBeenCalled();
    expect(mockBack).not.toHaveBeenCalled();
  });

  it("shows the error banner and retries on 500", async () => {
    mockGetDepot.mockRejectedValueOnce(new ApiError("boom", 500));
    mockGetDepot.mockResolvedValueOnce(depot);
    render(<DepotDetailPage depotId="DEP-1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("heading", { name: "North Yard" }),
    ).toBeInTheDocument();
    expect(mockGetDepot).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });
});
