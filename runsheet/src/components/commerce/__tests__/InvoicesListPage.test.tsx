/**
 * Tests for InvoicesListPage.
 *
 * Focus: the previously-dead customer filter is now wired through a
 * CustomerPicker (backed by getCustomers) and filters invoices by customer.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../../../services/commerceApi", () => ({
  getInvoices: jest.fn(),
  getCustomers: jest.fn(),
}));

import { getCustomers, getInvoices } from "../../../services/commerceApi";
import InvoicesListPage from "../InvoicesListPage";

const mockGetInvoices = getInvoices as jest.MockedFunction<typeof getInvoices>;
const mockGetCustomers = getCustomers as jest.MockedFunction<
  typeof getCustomers
>;

function invoicesResponse() {
  return {
    data: [],
    cursor: null,
    has_more: false,
    request_id: "req-1",
  };
}

function customersResponse() {
  return {
    data: [
      { customer_id: "CUST-1", display_name: "Acme Fuel Co", status: "active" },
      { customer_id: "CUST-2", display_name: "Beta Corp", status: "active" },
    ],
    cursor: null,
    has_more: false,
    request_id: "req-2",
  };
}

describe("InvoicesListPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetInvoices.mockResolvedValue(invoicesResponse() as any);
    mockGetCustomers.mockResolvedValue(customersResponse() as any);
  });

  it("loads the customer roster for the filter picker on mount", async () => {
    render(<InvoicesListPage />);
    await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());
    await waitFor(() => expect(mockGetInvoices).toHaveBeenCalled());
  });

  it("filters invoices by the selected customer", async () => {
    render(<InvoicesListPage />);

    await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());

    fireEvent.click(await screen.findByLabelText("Customer"));
    fireEvent.click(await screen.findByText("Beta Corp"));

    await waitFor(() =>
      expect(mockGetInvoices).toHaveBeenCalledWith(
        expect.objectContaining({ customer_id: "CUST-2" }),
      ),
    );
  });
});
