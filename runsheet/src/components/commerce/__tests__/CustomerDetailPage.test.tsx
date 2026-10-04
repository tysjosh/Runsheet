/**
 * Load-failure states for CustomerDetailPage (F-1). The same component backs
 * /commerce/customers/[id] and /dashboard/customers/[id].
 */
import { fireEvent, render, screen } from "@testing-library/react";

const mockPush = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, back: jest.fn() }),
}));

jest.mock("../../../services/commerceApi", () => ({
  getCustomer: jest.fn(),
}));

import { ApiError } from "../../../services/api";
import { getCustomer } from "../../../services/commerceApi";
import CustomerDetailPage from "../CustomerDetailPage";

const mockGetCustomer = getCustomer as jest.MockedFunction<typeof getCustomer>;

describe("CustomerDetailPage load failures", () => {
  beforeEach(() => jest.clearAllMocks());

  it("shows not found and routes back to the customer list", async () => {
    mockGetCustomer.mockRejectedValue(
      new ApiError("Customer not found", 404, "customer_not_found"),
    );

    render(<CustomerDetailPage customerId="cust_missing" />);

    expect(
      await screen.findByRole("heading", { name: "Customer not found" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/customer "cust_missing"/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Back to Customers/ }));
    expect(mockPush).toHaveBeenCalledWith("/commerce/customers");
    expect(
      screen.getByRole("link", { name: "Go to Customers" }),
    ).toHaveAttribute("href", "/dashboard/customers");
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("uses the in-shell onBack (dashboard route) when provided", async () => {
    const onBack = jest.fn();
    mockGetCustomer.mockRejectedValue(new ApiError("Customer not found", 404));

    render(<CustomerDetailPage customerId="cust_missing" onBack={onBack} />);

    fireEvent.click(
      await screen.findByRole("button", { name: /Back to Customers/ }),
    );
    expect(onBack).toHaveBeenCalled();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("shows the module-disabled state for CUSTOMERS_DISABLED", async () => {
    mockGetCustomer.mockRejectedValue(
      new ApiError(
        "Commerce customers module is not enabled for this tenant",
        404,
        "CUSTOMERS_DISABLED",
      ),
    );

    render(<CustomerDetailPage customerId="cust_001" />);

    expect(
      await screen.findByRole("heading", {
        name: "Customers isn't enabled for your account",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("shows the error banner and retries on 500", async () => {
    mockGetCustomer.mockRejectedValue(new ApiError("boom", 500));

    render(<CustomerDetailPage customerId="cust_001" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(mockGetCustomer).toHaveBeenCalledTimes(2);
  });
});
