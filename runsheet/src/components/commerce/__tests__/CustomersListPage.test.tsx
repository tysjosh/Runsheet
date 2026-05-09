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
}));

import { getCustomers } from "../../../services/commerceApi";
import CustomersListPage from "../CustomersListPage";

const mockGetCustomers = getCustomers as jest.MockedFunction<typeof getCustomers>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function customerFixture(overrides: Record<string, unknown> = {}) {
  return {
    customer_id: "cust_001",
    tenant_id: "tenant-a",
    display_name: "Acme Fuel Corp",
    legal_name: "Acme Fuel Corporation LLC",
    email: "billing@acme.com",
    phone: "555-0100",
    status: "active",
    account_ids: ["acc_001", "acc_002"],
    tags: ["enterprise"],
    created_at: "2024-01-15T10:00:00Z",
    updated_at: "2024-06-01T08:00:00Z",
    ...overrides,
  };
}

function paginatedResponse(customers: unknown[], page = 1, totalPages = 1) {
  return {
    data: customers,
    pagination: { page, size: 20, total: customers.length, total_pages: totalPages },
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
        customerFixture({ customer_id: "cust_002", display_name: "Beta Energy" }),
      ]) as any,
    );

    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
      expect(screen.getByText("Beta Energy")).toBeInTheDocument();
    });
  });

  it("shows loading state initially", () => {
    mockGetCustomers.mockReturnValue(new Promise(() => {}));
    render(<CustomersListPage />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("shows error state on fetch failure", async () => {
    mockGetCustomers.mockRejectedValue(new Error("Network error"));
    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/Network error/)).toBeInTheDocument();
    });
  });

  it("shows empty state when no customers found", async () => {
    mockGetCustomers.mockResolvedValue(paginatedResponse([]) as any);
    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("No customers found.")).toBeInTheDocument();
    });
  });

  it("calls onSelectCustomer when View Details is clicked", async () => {
    mockGetCustomers.mockResolvedValue(
      paginatedResponse([customerFixture()]) as any,
    );
    const onSelect = jest.fn();
    render(<CustomersListPage onSelectCustomer={onSelect} />);

    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /View Details/i }));
    expect(onSelect).toHaveBeenCalledWith("cust_001");
  });

  it("filters by status when dropdown changes", async () => {
    mockGetCustomers.mockResolvedValue(paginatedResponse([customerFixture()]) as any);
    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
    });

    const statusSelect = screen.getByLabelText("Status");
    fireEvent.change(statusSelect, { target: { value: "archived" } });

    await waitFor(() => {
      expect(mockGetCustomers).toHaveBeenCalledWith(
        expect.objectContaining({ status: "archived", page: 1 }),
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

    const prevBtn = screen.getByRole("button", { name: /Previous/i });
    expect(prevBtn).toBeDisabled();

    fireEvent.click(nextBtn);

    await waitFor(() => {
      expect(mockGetCustomers).toHaveBeenCalledWith(
        expect.objectContaining({ page: 2 }),
      );
    });
  });

  it("submits search form and resets page", async () => {
    mockGetCustomers.mockResolvedValue(paginatedResponse([customerFixture()]) as any);
    render(<CustomersListPage />);

    await waitFor(() => {
      expect(screen.getByText("Acme Fuel Corp")).toBeInTheDocument();
    });

    const searchInput = screen.getByLabelText("Search");
    fireEvent.change(searchInput, { target: { value: "beta" } });

    const searchBtn = screen.getByRole("button", { name: /Search/i });
    fireEvent.click(searchBtn);

    await waitFor(() => {
      expect(mockGetCustomers).toHaveBeenCalledWith(
        expect.objectContaining({ search: "beta", page: 1 }),
      );
    });
  });
});
