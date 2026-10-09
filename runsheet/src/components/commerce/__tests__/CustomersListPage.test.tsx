/**
 * Tests for CustomersListPage component.
 *
 * Covers:
 * - Happy path: list fetch + render
 * - Status filter changes trigger refetch
 * - Search form submission
 * - Pagination controls
 * - Error state rendering
 * - Empty state rendering
 * - Customer selection callback
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../../../services/commerceApi", () => ({
  getCustomers: jest.fn(),
  getCustomer: jest.fn(),
  createCustomer: jest.fn(),
}));

import { ApiError } from "../../../services/api";
import {
  createCustomer,
  getCustomer,
  getCustomers,
} from "../../../services/commerceApi";
import CustomersListPage from "../CustomersListPage";

const mockGetCustomers = getCustomers as jest.MockedFunction<
  typeof getCustomers
>;
const mockGetCustomer = getCustomer as jest.MockedFunction<typeof getCustomer>;
const mockCreate = createCustomer as jest.MockedFunction<typeof createCustomer>;
/** The list read (not the `size: 1` chip-count reads). */
const listCalls = () =>
  mockGetCustomers.mock.calls.filter(([f]) => f?.size !== 1).map(([f]) => f);

// ─── Fixtures ────────────────────────────────────────────────────────────────

function customerFixture(overrides: Record<string, unknown> = {}) {
  return {
    customer_id: "cust_001",
    tenant_id: "tenant-a",
    display_name: "Acme Fuel Corp",
    legal_name: "Acme Fuel Corporation LLC",
    primary_email: "billing@acme.com",
    phone: "555-0100",
    status: "active",
    account_count: 2,
    tags: ["enterprise"],
    created_at: "2024-01-15T10:00:00Z",
    updated_at: "2024-06-01T08:00:00Z",
    ...overrides,
  };
}

function paginatedResponse(customers: unknown[], page = 1, totalPages = 1) {
  return {
    data: customers,
    pagination: {
      page,
      size: 20,
      total: customers.length,
      total_pages: totalPages,
    },
    cursor: null,
    has_more: page < totalPages,
    request_id: "req-123",
  };
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("CustomersListPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("renders customer list on successful fetch", async () => {
    mockGetCustomers.mockResolvedValue(
      paginatedResponse([
        customerFixture(),
        customerFixture({
          customer_id: "cust_002",
          display_name: "Beta Energy",
        }),
      ]) as any,
    );

    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
      expect(screen.getByText("Beta Energy")).toBeInTheDocument();
    });
  });

  it("shows skeleton rows while loading", () => {
    mockGetCustomers.mockReturnValue(new Promise(() => {}));
    render(<CustomersListPage />);
    expect(
      screen.getByRole("table", { name: "Customers" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("No customers found")).toBeNull();
  });

  it("shows error state on fetch failure", async () => {
    mockGetCustomers.mockRejectedValue(new Error("Network error"));
    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/Network error/)).toBeInTheDocument();
    });
  });

  it("shows the module-disabled state for CUSTOMERS_DISABLED", async () => {
    mockGetCustomers.mockRejectedValue(
      new ApiError(
        "Commerce customers module is not enabled for this tenant",
        404,
        "CUSTOMERS_DISABLED",
      ),
    );
    render(<CustomersListPage />);

    expect(
      await screen.findByRole("heading", {
        name: "Customers isn't enabled for your account",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("group", { name: "Customer status" })).toBeNull();
    expect(
      screen.getByRole("button", { name: /Back to Today/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("shows empty state when no customers found", async () => {
    mockGetCustomers.mockResolvedValue(paginatedResponse([]) as any);
    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("No customers found")).toBeInTheDocument();
    });
  });

  it("calls onSelectCustomer when a row is clicked", async () => {
    mockGetCustomers.mockResolvedValue(
      paginatedResponse([customerFixture()]) as any,
    );
    const onSelect = jest.fn();
    render(<CustomersListPage onSelectCustomer={onSelect} />);

    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Acme Fuel Corp"));
    expect(onSelect).toHaveBeenCalledWith("cust_001");
  });

  it("renders customer detail in-shell when no onSelectCustomer override is provided", async () => {
    mockGetCustomers.mockResolvedValue(
      paginatedResponse([customerFixture()]) as any,
    );
    mockGetCustomer.mockResolvedValue({
      data: {
        ...customerFixture(),
        open_invoice_count: 1,
        open_balance_cents: 12345,
        lifetime_revenue_cents: 678900,
      },
      request_id: "req-1",
    } as any);

    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Actions for Acme Fuel Corp" }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: /View details/i }));

    // Detail loads in place (no route navigation) — the in-shell Back affordance
    // appears and the customer summary renders.
    expect(
      await screen.findByRole("button", { name: /Back to Customers/i }),
    ).toBeInTheDocument();
    expect(mockGetCustomer).toHaveBeenCalledWith("cust_001");

    // Returning to the list restores it.
    fireEvent.click(screen.getByRole("button", { name: /Back to Customers/i }));
    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
    });
  });

  it("filters by status from the chips", async () => {
    mockGetCustomers.mockResolvedValue(
      paginatedResponse([customerFixture()]) as any,
    );
    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Archived/ }));

    await waitFor(() => {
      expect(listCalls()).toContainEqual(
        expect.objectContaining({ status: "archived", page: 1, size: 20 }),
      );
    });
  });

  it("paginates with Previous/Next buttons", async () => {
    mockGetCustomers.mockResolvedValue(
      paginatedResponse([customerFixture()], 1, 3) as any,
    );
    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
    });

    const nextBtn = screen.getByRole("button", { name: /Next/i });
    expect(nextBtn).not.toBeDisabled();

    const prevBtn = screen.getByRole("button", { name: /Prev/i });
    expect(prevBtn).toBeDisabled();

    fireEvent.click(nextBtn);

    await waitFor(() => {
      expect(mockGetCustomers).toHaveBeenCalledWith(
        expect.objectContaining({ page: 2 }),
      );
    });
  });

  it("submits search form and resets page", async () => {
    mockGetCustomers.mockResolvedValue(
      paginatedResponse([customerFixture()]) as any,
    );
    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
    });

    const searchInput = screen.getByLabelText("Search");
    fireEvent.change(searchInput, { target: { value: "beta" } });

    await waitFor(() => {
      expect(mockGetCustomers).toHaveBeenCalledWith(
        expect.objectContaining({ search: "beta", page: 1 }),
      );
    });
  });

  it("shows status chips with counts from per-status reads", async () => {
    mockGetCustomers.mockImplementation(async (f) => {
      const total =
        f?.status === "active" ? 7 : f?.status === "archived" ? 2 : 9;
      return {
        ...paginatedResponse([customerFixture()]),
        pagination: { page: 1, size: 20, total, total_pages: 1 },
      } as any;
    });
    render(<CustomersListPage />);
    expect(
      await screen.findByRole("button", { name: /All\s*9/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Active\s*7/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Archived\s*2/ }),
    ).toBeInTheDocument();
  });

  it("shows the status as an icon badge with a label and money as currency", async () => {
    mockGetCustomers.mockResolvedValue(
      paginatedResponse([
        customerFixture({ status: "archived", open_balance_cents: 1234567 }),
      ]) as any,
    );
    render(<CustomersListPage />);
    await screen.findByText("Acme Fuel Corp");
    const row = screen.getByText("Acme Fuel Corp").closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("Archived");
    expect(row).toHaveTextContent("$12,345.67");
  });

  it("opens a customer's tanks in a drawer from the row menu", async () => {
    mockGetCustomers.mockResolvedValue(
      paginatedResponse([customerFixture()]) as any,
    );
    render(<CustomersListPage onSelectCustomer={jest.fn()} />);
    await screen.findByText("Acme Fuel Corp");
    fireEvent.click(
      screen.getByRole("button", { name: "Actions for Acme Fuel Corp" }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: /View tanks/i }));
    expect(
      await screen.findByRole("dialog", { name: "Tanks · Acme Fuel Corp" }),
    ).toBeInTheDocument();
  });

  describe("New customer FormDialog", () => {
    beforeEach(() => {
      mockGetCustomers.mockResolvedValue(
        paginatedResponse([customerFixture()]) as any,
      );
    });

    it("validates, creates, toasts and refetches", async () => {
      mockCreate.mockResolvedValue({
        data: customerFixture({ customer_id: "cust_new" }),
        request_id: "r",
      } as any);
      render(<CustomersListPage />);
      await screen.findByText("Acme Fuel Corp");
      fireEvent.click(screen.getByRole("button", { name: "New customer" }));
      const dialog = await screen.findByRole("dialog", {
        name: "New customer",
      });
      fireEvent.click(screen.getByRole("button", { name: "Create customer" }));
      expect(await screen.findByText("Enter a name.")).toBeInTheDocument();
      expect(mockCreate).not.toHaveBeenCalled();
      fireEvent.change(screen.getByLabelText(/^Name/), {
        target: { value: "  QA Fuel Co  " },
      });
      fireEvent.change(screen.getByLabelText(/Billing email/), {
        target: { value: "nope" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Create customer" }));
      expect(
        await screen.findByText("Enter a valid email address."),
      ).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText(/Billing email/), {
        target: { value: "ap@qa.example" },
      });
      const before = listCalls().length;
      fireEvent.click(screen.getByRole("button", { name: "Create customer" }));
      await waitFor(() =>
        expect(mockCreate).toHaveBeenCalledWith({
          display_name: "QA Fuel Co",
          primary_email: "ap@qa.example",
        }),
      );
      await waitFor(() => expect(dialog).not.toBeInTheDocument());
      await waitFor(() => expect(listCalls().length).toBeGreaterThan(before));
    });

    it("keeps the dialog open with the envelope field error inline", async () => {
      mockCreate.mockRejectedValue(
        new ApiError("Validation failed", 422, "VALIDATION_ERROR", {
          fields: { display_name: "A customer with this name exists" },
        }),
      );
      render(<CustomersListPage />);
      await screen.findByText("Acme Fuel Corp");
      fireEvent.click(screen.getByRole("button", { name: "New customer" }));
      fireEvent.change(await screen.findByLabelText(/^Name/), {
        target: { value: "Acme Fuel Corp" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Create customer" }));
      expect(
        await screen.findByText("A customer with this name exists"),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("dialog", { name: "New customer" }),
      ).toBeInTheDocument();
    });
  });
});
