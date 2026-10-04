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
      screen.getByRole("link", { name: "Go to Fuel Ops" }),
    ).toHaveAttribute("href", "/dashboard/fuel-ops");
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
