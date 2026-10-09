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
    fireEvent.change(screen.getByLabelText(/^Start Date/), {
      target: { value: "2026-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/^End Date/), {
      target: { value: "2026-12-31" },
    });
    fireEvent.change(screen.getByLabelText(/^Contracted Gallons/), {
      target: { value: "1000" },
    });

    // Submit button shares the "Add Contract" label inside the form.
    const submit = screen
      .getAllByRole("button", { name: /Add Contract/i })
      .at(-1) as HTMLButtonElement;
    fireEvent.click(submit);

    // Field-level errors in the FormDialog (task 3.5).
    expect(await screen.findByText("Pick a customer.")).toBeInTheDocument();
    expect(screen.getByText("Pick an account.")).toBeInTheDocument();
    expect(screen.getByText("Pick a product.")).toBeInTheDocument();
    expect(mockCreateContract).not.toHaveBeenCalled();
  });

  it("lists products by name and prices in dollars, and edits in the dialog", async () => {
    mockGetContracts.mockResolvedValue({
      data: [
        {
          contract_id: "pc_1",
          customer_id: "CUST-1",
          account_id: "acc_1",
          product_code: "DIESEL_2",
          contract_type: "collar",
          start_date: "2026-01-01",
          end_date: "2026-12-31",
          contracted_gallons: 10000,
          remaining_gallons: 4000,
          price_cap_cents: 400,
          price_floor_cents: 300,
          fixed_price_cents: null,
          status: "active",
        },
      ],
      pagination: { page: 1, size: 20, total: 1, total_pages: 1 },
    } as any);
    const { updatePriceProtectionContract } = jest.requireMock(
      "../../services/complianceApi",
    ) as { updatePriceProtectionContract: jest.Mock };
    updatePriceProtectionContract.mockResolvedValue({ data: {} });
    render(<PriceProtectionContractsPage />);
    const table = await screen.findByRole("table", {
      name: "Price protection contracts",
    });
    await waitFor(() => expect(table).toHaveTextContent("Diesel #2 (on-road)"));
    expect(table).toHaveTextContent("$3.00–$4.00");
    expect(table).toHaveTextContent("4,000 gal of 10,000 gal");
    // Market $3.50 inside the collar: no variance; 6,000 gal delivered.
    expect(table).toHaveTextContent("$0.00 gain");
    fireEvent.click(screen.getByText("$3.00–$4.00"));
    const cap = await screen.findByLabelText(/^Price cap/);
    expect(cap).toHaveValue("4.00");
    fireEvent.change(cap, { target: { value: "4.25" } });
    fireEvent.blur(cap);
    fireEvent.click(screen.getByRole("button", { name: "Update Contract" }));
    await waitFor(() =>
      expect(updatePriceProtectionContract).toHaveBeenCalledWith(
        "pc_1",
        expect.objectContaining({
          price_cap_cents: 425,
          price_floor_cents: 300,
        }),
      ),
    );
  });
});
