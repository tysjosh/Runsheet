/**
 * Tests for PricingRulesPage.
 *
 * Focus: free-text Customer ID / Product Code fields are replaced with the
 * reusable CustomerPicker (getCustomers) and ProductPicker (listFuelProducts)
 * in both the rule-create form and the Resolve-Price test panel. Terminal ID
 * stays free text (no terminal roster).
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../../services/complianceApi", () => ({
  getPricingRules: jest.fn(),
  createPricingRule: jest.fn(),
  resolvePrice: jest.fn(),
}));
jest.mock("../../services/commerceApi", () => ({
  getCustomers: jest.fn(),
}));
jest.mock("../../services/fuelApi", () => ({
  listFuelProducts: jest.fn(),
}));

import { getCustomers } from "../../services/commerceApi";
import { getPricingRules, resolvePrice } from "../../services/complianceApi";
import { listFuelProducts } from "../../services/fuelApi";
import PricingRulesPage from "./PricingRulesPage";

const mockGetPricingRules = getPricingRules as jest.MockedFunction<
  typeof getPricingRules
>;
const mockResolvePrice = resolvePrice as jest.MockedFunction<
  typeof resolvePrice
>;
const mockGetCustomers = getCustomers as jest.MockedFunction<
  typeof getCustomers
>;
const mockListFuelProducts = listFuelProducts as jest.MockedFunction<
  typeof listFuelProducts
>;

describe("PricingRulesPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetPricingRules.mockResolvedValue({
      data: [],
      pagination: { page: 1, size: 20, total: 0, total_pages: 1 },
    } as any);
    mockGetCustomers.mockResolvedValue({
      data: [
        {
          customer_id: "CUST-1",
          display_name: "Acme Fuel Co",
          status: "active",
        },
      ],
      cursor: null,
    } as any);
    mockListFuelProducts.mockResolvedValue({
      items: [
        {
          product_code: "ULSD",
          display_name: "Ultra Low Sulfur Diesel",
          category: "diesel",
        },
      ],
    } as any);
  });

  it("loads roster data for the resolve-panel pickers on mount", async () => {
    render(<PricingRulesPage />);
    await waitFor(() => expect(mockGetPricingRules).toHaveBeenCalled());
    await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());
    await waitFor(() => expect(mockListFuelProducts).toHaveBeenCalled());
  });

  it("requires customer and product before resolving a price", async () => {
    render(<PricingRulesPage />);
    await waitFor(() => expect(mockGetPricingRules).toHaveBeenCalled());

    // Gallons is a native-required input; fill it so the picker-level
    // validation (customer + product) is what gets exercised.
    fireEvent.change(screen.getByLabelText("Gallons"), {
      target: { value: "500" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Resolve Price/i }));

    expect(
      await screen.findByText(/Customer and product code are required/i),
    ).toBeInTheDocument();
    expect(mockResolvePrice).not.toHaveBeenCalled();
  });

  it("keeps Terminal ID as a free-text input (no terminal roster)", async () => {
    render(<PricingRulesPage />);
    await waitFor(() => expect(mockGetPricingRules).toHaveBeenCalled());

    const terminal = screen.getByLabelText(/Terminal ID/i) as HTMLInputElement;
    expect(terminal.tagName).toBe("INPUT");
    expect(terminal.type).toBe("text");
  });
});
