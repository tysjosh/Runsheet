/**
 * Tests for PriceProtectionContractsPage (ContractForm).
 *
 * Focus: free-text Customer ID / Product Code become CustomerPicker
 * (getCustomers) and ProductPicker (listFuelProducts); Account ID becomes a
 * customer-scoped account picker (SearchableSelect backed by getAccounts).
 * Required-field validation is preserved now that native required is gone.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../../services/complianceApi", () => ({
  getPriceProtectionContracts: jest.fn(),
  createPriceProtectionContract: jest.fn(),
  updatePriceProtectionContract: jest.fn(),
}));
jest.mock("../../services/commerceApi", () => ({
  getCustomers: jest.fn(),
  getAccounts: jest.fn(),
}));
jest.mock("../../services/fuelApi", () => ({
  listFuelProducts: jest.fn(),
}));

import { getAccounts, getCustomers } from "../../services/commerceApi";
import {
  createPriceProtectionContract,
  getPriceProtectionContracts,
} from "../../services/complianceApi";
import { listFuelProducts } from "../../services/fuelApi";
import PriceProtectionContractsPage from "./PriceProtectionContractsPage";

const mockGetContracts = getPriceProtectionContracts as jest.MockedFunction<
  typeof getPriceProtectionContracts
>;
const mockCreateContract = createPriceProtectionContract as jest.MockedFunction<
  typeof createPriceProtectionContract
>;
const mockGetCustomers = getCustomers as jest.MockedFunction<
  typeof getCustomers
>;
const mockGetAccounts = getAccounts as jest.MockedFunction<typeof getAccounts>;
const mockListFuelProducts = listFuelProducts as jest.MockedFunction<
  typeof listFuelProducts
>;

describe("PriceProtectionContractsPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetContracts.mockResolvedValue({
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
    mockGetAccounts.mockResolvedValue({
      data: [
        { account_id: "acc_1", display_name: "Acme — Main", status: "active" },
      ],
      cursor: null,
    } as any);
    mockListFuelProducts.mockResolvedValue({
      items: [
        {
          product_code: "HEATING_OIL",
          display_name: "Heating Oil",
          category: "distillate",
        },
      ],
    } as any);
  });

  it("loads customer and product rosters when the create form opens", async () => {
    render(<PriceProtectionContractsPage />);
    await waitFor(() => expect(mockGetContracts).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /Add Contract/i }));

    await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());
    await waitFor(() => expect(mockListFuelProducts).toHaveBeenCalled());
  });

  it("scopes the account picker to the chosen customer", async () => {
    render(<PriceProtectionContractsPage />);
    await waitFor(() => expect(mockGetContracts).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /Add Contract/i }));
    await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());

    // No account roster fetch until a customer is selected.
    expect(mockGetAccounts).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByLabelText("Customer ID"));
    fireEvent.click(await screen.findByText("Acme Fuel Co"));

    await waitFor(() =>
      expect(mockGetAccounts).toHaveBeenCalledWith(
        expect.objectContaining({ customer_id: "CUST-1" }),
      ),
    );
  });

  it("validates required customer/account/product before submit", async () => {
    render(<PriceProtectionContractsPage />);
    await waitFor(() => expect(mockGetContracts).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /Add Contract/i }));
    await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());

    // Satisfy the remaining native-required fields so the picker-level
    // validation (customer + account + product) is what gets exercised.
    fireEvent.change(screen.getByLabelText("Start Date"), {
      target: { value: "2026-01-01" },
    });
    fireEvent.change(screen.getByLabelText("End Date"), {
      target: { value: "2026-12-31" },
    });
    fireEvent.change(screen.getByLabelText("Contracted Gallons"), {
      target: { value: "1000" },
    });

    // Submit button shares the "Add Contract" label inside the form.
    const submit = screen
      .getAllByRole("button", { name: /Add Contract/i })
      .at(-1) as HTMLButtonElement;
    fireEvent.click(submit);

    expect(
      await screen.findByText(
        /Customer, account, and product code are required/i,
      ),
    ).toBeInTheDocument();
    expect(mockCreateContract).not.toHaveBeenCalled();
  });
});
