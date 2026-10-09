/**
 * Tests for PricingRulesPage.
 *
 * Focus: free-text Customer ID / Product Code fields are replaced with the
 * reusable CustomerPicker (getCustomers) and ProductPicker (listFuelProducts)
 * in both the rule-create form and the Resolve-Price test panel. Terminal ID
 * stays free text (no terminal roster).
 */

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

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

  it("loads roster data for the resolve-panel pickers when Price check opens", async () => {
    render(<PricingRulesPage />);
    await waitFor(() => expect(mockGetPricingRules).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Price check" }));
    await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());
    await waitFor(() => expect(mockListFuelProducts).toHaveBeenCalled());
  });

  it("requires customer and product before resolving a price", async () => {
    render(<PricingRulesPage />);
    await waitFor(() => expect(mockGetPricingRules).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Price check" }));

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
    fireEvent.click(screen.getByRole("button", { name: "Price check" }));

    const terminal = screen.getByLabelText(/Terminal ID/i) as HTMLInputElement;
    expect(terminal.tagName).toBe("INPUT");
    expect(terminal.type).toBe("text");
  });

  it("adds a tiered rule in the sectioned FormDialog, prices in dollars", async () => {
    const { createPricingRule } = jest.requireMock(
      "../../services/complianceApi",
    ) as { createPricingRule: jest.Mock };
    createPricingRule.mockResolvedValue({ data: {} });
    render(<PricingRulesPage />);
    await waitFor(() => expect(mockGetPricingRules).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Add Rule" }));
    const dialog = screen.getByRole("dialog", { name: "Add pricing rule" });
    fireEvent.change(within(dialog).getByLabelText(/^Strategy/), {
      target: { value: "tiered_volume" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add tier" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Add Rule" }));
    expect(await within(dialog).findByText("Pick a product.")).toBeInTheDocument();
    expect(
      within(dialog).getByText("Tier 2 needs a minimum and a price."),
    ).toBeInTheDocument();
    expect(createPricingRule).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByLabelText("Product Code"));
    fireEvent.click(await screen.findByText("Ultra Low Sulfur Diesel"));
    const price2 = within(dialog).getByLabelText("Tier 2 price");
    fireEvent.change(price2, { target: { value: "3.25" } });
    fireEvent.blur(price2);
    fireEvent.change(within(dialog).getByLabelText(/^Effective date/), {
      target: { value: "2026-11-01" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add Rule" }));
    await waitFor(() => expect(createPricingRule).toHaveBeenCalled());
    expect(createPricingRule.mock.calls[0][0]).toMatchObject({
      product_code: "ULSD",
      strategy: "tiered_volume",
      tier_thresholds: [
        { min_gallons: 0, max_gallons: 1000, price_cents: 350 },
        { min_gallons: 1001, max_gallons: null, price_cents: 325 },
      ],
    });
  });
});
