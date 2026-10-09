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
  const input = screen.getByRole("combobox", {
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

  it("implements the combobox pattern with keyboard selection", async () => {
    mockSearch.mockResolvedValue({
      orders: [{ type: "order", id: "ord_1", label: "ord_1" }],
      customers: [{ type: "customer", id: "C1", label: "Acme" }],
      assets: [],
    });
    const assign = jest.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, assign },
    });
    render(<GlobalSearch onSubmitFallback={jest.fn()} />);
    type("ac");
    const input = screen.getByRole("combobox", { name: /search orders/i });
    await screen.findByText("Acme");
    expect(input).toHaveAttribute("aria-expanded", "true");
    const listbox = screen.getByRole("listbox", { name: "Search results" });
    expect(input).toHaveAttribute("aria-controls", listbox.id);
    expect(
      screen.getByRole("group", { name: "Customers" }),
    ).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const options = screen.getAllByRole("option");
    expect(input).toHaveAttribute("aria-activedescendant", options[1].id);
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(assign).toHaveBeenCalledWith(expect.stringContaining("C1"));
  });

  it("hands the query to the fallback when nothing is active", async () => {
    mockSearch.mockResolvedValue({ orders: [], customers: [], assets: [] });
    const fallback = jest.fn();
    render(<GlobalSearch onSubmitFallback={fallback} />);
    type("diesel");
    await waitFor(() => expect(mockSearch).toHaveBeenCalled());
    fireEvent.keyDown(
      screen.getByRole("combobox", { name: /search orders/i }),
      {
        key: "Enter",
      },
    );
    expect(fallback).toHaveBeenCalledWith("diesel");
  });

  it("focuses on '/' unless the user is typing in a field", () => {
    render(
      <>
        <input aria-label="other" />
        <GlobalSearch onSubmitFallback={jest.fn()} />
      </>,
    );
    const input = screen.getByRole("combobox", { name: /search orders/i });
    fireEvent.keyDown(document.body, { key: "/" });
    expect(input).toHaveFocus();
    const other = screen.getByRole("textbox", { name: "other" });
    other.focus();
    fireEvent.keyDown(other, { key: "/" });
    expect(other).toHaveFocus();
  });
});
