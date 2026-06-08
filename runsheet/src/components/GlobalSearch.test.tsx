/**
 * Tests for GlobalSearch — the header's cross-entity search dropdown.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

jest.mock("../services/api", () => ({
  apiService: { universalSearch: jest.fn() },
}));

import { apiService } from "../services/api";
import GlobalSearch from "./GlobalSearch";

const mockSearch = apiService.universalSearch as jest.Mock;

function type(value: string) {
  const input = screen.getByRole("searchbox", {
    name: /search orders, customers/i,
  });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value } });
}

describe("GlobalSearch", () => {
  beforeEach(() => {
    mockSearch.mockReset();
  });

  it("renders grouped results from the universal search endpoint", async () => {
    mockSearch.mockResolvedValue({
      orders: [
        {
          type: "order",
          id: "ord_1",
          label: "ord_1",
          sublabel: "Acme · placed",
        },
      ],
      customers: [
        {
          type: "customer",
          id: "CUST-001",
          label: "Acme Fuel Distribution",
          sublabel: "acme@fuel.com",
        },
      ],
      assets: [],
    });

    render(<GlobalSearch onSubmitFallback={jest.fn()} />);
    type("acme");

    await waitFor(() => expect(mockSearch).toHaveBeenCalledWith("acme", 5));

    // Group headers for the non-empty groups appear; the empty assets group
    // is omitted.
    expect(await screen.findByText("Orders")).toBeInTheDocument();
    expect(screen.getByText("Customers")).toBeInTheDocument();
    expect(screen.queryByText("Assets")).not.toBeInTheDocument();
    expect(screen.getByText("Acme Fuel Distribution")).toBeInTheDocument();
    expect(screen.getByText("ord_1")).toBeInTheDocument();
  });

  it("shows an empty state when nothing matches", async () => {
    mockSearch.mockResolvedValue({ orders: [], customers: [], assets: [] });

    render(<GlobalSearch onSubmitFallback={jest.fn()} />);
    type("zzz");

    await waitFor(() =>
      expect(screen.getByText(/no matches for/i)).toBeInTheDocument(),
    );
  });

  it("does not query for an empty string", async () => {
    render(<GlobalSearch onSubmitFallback={jest.fn()} />);
    type("   ");
    // Give the debounce window time to elapse.
    await new Promise((r) => setTimeout(r, 350));
    expect(mockSearch).not.toHaveBeenCalled();
  });
});
