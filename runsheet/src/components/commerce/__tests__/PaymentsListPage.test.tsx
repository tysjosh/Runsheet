/**
 * Tests for PaymentsListPage.
 *
 * Focus: the brittle "inv"/"acc" substring heuristic search is replaced with
 * an explicit account picker (SearchableSelect backed by getAccounts) and a
 * plain Invoice ID text input.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../../../services/commerceApi", () => ({
  getPayments: jest.fn(),
  getAccounts: jest.fn(),
}));

import { getAccounts, getPayments } from "../../../services/commerceApi";
import PaymentsListPage from "../PaymentsListPage";

const mockGetPayments = getPayments as jest.MockedFunction<typeof getPayments>;
const mockGetAccounts = getAccounts as jest.MockedFunction<typeof getAccounts>;

function paymentsResponse() {
  return {
    data: [],
    cursor: null,
    has_more: false,
    request_id: "req-1",
  };
}

function accountsResponse() {
  return {
    data: [
      { account_id: "acc_1", display_name: "Acme — Main", status: "active" },
      { account_id: "acc_2", display_name: "Beta — Ops", status: "active" },
    ],
    cursor: null,
    has_more: false,
    request_id: "req-2",
  };
}

describe("PaymentsListPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetPayments.mockResolvedValue(paymentsResponse() as any);
    mockGetAccounts.mockResolvedValue(accountsResponse() as any);
  });

  it("loads the account roster for the picker on mount", async () => {
    render(<PaymentsListPage />);
    await waitFor(() => expect(mockGetAccounts).toHaveBeenCalled());
    await waitFor(() => expect(mockGetPayments).toHaveBeenCalled());
  });

  it("filters payments by the selected account", async () => {
    render(<PaymentsListPage />);

    await waitFor(() => expect(mockGetAccounts).toHaveBeenCalled());

    fireEvent.click(await screen.findByLabelText("Account"));
    fireEvent.click(await screen.findByText("Beta — Ops"));

    await waitFor(() =>
      expect(mockGetPayments).toHaveBeenCalledWith(
        expect.objectContaining({ account_id: "acc_2" }),
      ),
    );
  });

  it("filters payments by an explicit invoice id", async () => {
    render(<PaymentsListPage />);
    await waitFor(() => expect(mockGetPayments).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Invoice ID"), {
      target: { value: "inv_123" },
    });

    await waitFor(() =>
      expect(mockGetPayments).toHaveBeenCalledWith(
        expect.objectContaining({ invoice_id: "inv_123" }),
      ),
    );
  });
});
