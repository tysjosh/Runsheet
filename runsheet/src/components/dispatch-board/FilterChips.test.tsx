/**
 * Board filter chips: a deep-linked status the panel doesn't list (the
 * Dashboard's exceptions count sends `status=failed,rejected`) shows as a
 * pressed chip so it can be seen and cleared.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { FilterChips } from "./FilterChips";

const filters = {
  call_type: [],
  product: [],
  priority: [],
  window: [],
  status: ["failed", "rejected"],
  has_warnings: false,
};

it("shows deep-linked statuses as pressed chips that clear", () => {
  const onChange = jest.fn();
  render(
    <FilterChips
      filters={filters}
      products={[]}
      priorities={[]}
      onChange={onChange}
    />,
  );
  expect(screen.getByRole("button", { name: "Failed" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByRole("button", { name: "Rejected" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  fireEvent.click(screen.getByRole("button", { name: "Failed" }));
  expect(onChange).toHaveBeenCalledWith({ ...filters, status: ["rejected"] });
});
