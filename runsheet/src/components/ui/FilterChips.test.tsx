/**
 * FilterChips `collapse` (R4.3): chips that don't fit move into a "More"
 * menu in declared order instead of clipping; the selected chip stays shown.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { FilterChips, fitChips, visibleChipIndexes } from "./FilterChips";

const OPTIONS = ["All", "Placed", "Confirmed", "Scheduled", "Delivered"].map(
  (label) => ({ id: label.toLowerCase(), label, count: 3 }),
);

it("fitChips keeps every chip when they fit and reserves room for More", () => {
  expect(fitChips([50, 50, 50], 60, 162)).toBe(3);
  // 162 > 150, so More (60) + one chip (6 + 50); a second would need 172
  expect(fitChips([50, 50, 50], 60, 150)).toBe(1);
});

// jsdom has no layout: give each chip 80 px and the strip 260 px.
function withLayout(stripWidth: number) {
  const rect = jest
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockReturnValue({ width: 80 } as DOMRect);
  const client = jest
    .spyOn(HTMLElement.prototype, "clientWidth", "get")
    .mockReturnValue(stripWidth);
  return () => {
    rect.mockRestore();
    client.mockRestore();
  };
}

it("collapses the chips that don't fit into a More menu", () => {
  const restore = withLayout(260);
  const onChange = jest.fn();
  render(
    <FilterChips
      label="Order status"
      options={OPTIONS}
      value="all"
      onChange={onChange}
      collapse
    />,
  );
  const group = screen.getByRole("group", { name: "Order status" });
  // More (80) + 2 chips (80 + 6 + 80 + 6) = 252 ≤ 260; a third doesn't fit.
  const pressed = within(group)
    .getAllByRole("button")
    .filter((b) => b.hasAttribute("aria-pressed"));
  expect(pressed.map((b) => b.textContent)).toEqual(["All3", "Placed3"]);
  fireEvent.click(
    within(group).getByRole("button", { name: /More order status filters/ }),
  );
  fireEvent.click(screen.getByRole("menuitemradio", { name: /Delivered/ }));
  expect(onChange).toHaveBeenCalledWith("delivered");
  restore();
});

it("keeps the selected chip visible when it would overflow", () => {
  const restore = withLayout(260);
  render(
    <FilterChips
      label="Order status"
      options={OPTIONS}
      value="delivered"
      onChange={() => {}}
      collapse
    />,
  );
  const group = screen.getByRole("group", { name: "Order status" });
  expect(
    within(group).getByRole("button", { name: /^Delivered/ }),
  ).toHaveAttribute("aria-pressed", "true");
  restore();
});

it("swapping in a wide selected chip pushes out enough chips that nothing clips", () => {
  const widths = [50, 50, 50, 50, 120];
  const more = 60;
  const avail = 250;
  // Three leading chips fit without a selection.
  expect(fitChips(widths, more, avail)).toBe(3);
  const shown = visibleChipIndexes(widths, more, avail, 4);
  expect(shown).toContain(4);
  const used =
    more + shown.reduce((s, i) => s + widths[i], 0) + 6 * shown.length;
  expect(used).toBeLessThanOrEqual(avail);
  expect(shown).toEqual([0, 4]);
});

it("keeps the leading order when the selection is already visible", () => {
  expect(visibleChipIndexes([50, 50, 50, 50, 50], 60, 250, 1)).toEqual([
    0, 1, 2,
  ]);
  expect(visibleChipIndexes([50, 50], 60, 250, 1)).toEqual([0, 1]);
});
