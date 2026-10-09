/**
 * Margin create/edit flows on FormDialog (task 3.4): cost entry add,
 * supersede and void; margin settings edit with product floors by name.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../../../services/marginApi", () => ({
  getCostEntries: jest.fn(),
  createCostEntry: jest.fn(),
  supersedeCostEntry: jest.fn(),
  voidCostEntry: jest.fn(),
  importCostEntries: jest.fn(),
  getMarginSettings: jest.fn(),
  updateMarginSettings: jest.fn(),
}));

import { ApiError } from "../../../../services/api";
import {
  createCostEntry,
  getCostEntries,
  getMarginSettings,
  supersedeCostEntry,
  updateMarginSettings,
  voidCostEntry,
} from "../../../../services/marginApi";
import CostEntriesPage from "../CostEntriesPage";
import MarginSettingsForm from "../MarginSettingsForm";

const entry = {
  entry_id: "mce_1",
  kind: "purchase",
  product_code: "DIESEL_2",
  terminal_id: null,
  supplier_name: "Acme",
  effective_at: "2026-10-01T12:00:00Z",
  effective_to: null,
  unit_cost_micros: 2_450_000,
  gallons_milli: 7_500_000,
  adder_type: null,
  bol_id: null,
  reference: "PO-1",
  notes: null,
  status: "active",
  source: "manual",
  created_by: "u",
  created_at: "2026-10-01T12:00:00Z",
};

beforeEach(() => {
  jest.clearAllMocks();
  (getCostEntries as jest.Mock).mockResolvedValue({
    items: [entry],
    next_cursor: null,
  });
});

describe("Cost entries", () => {
  it("lists products by name, not code", async () => {
    render(<CostEntriesPage />);
    const table = await screen.findByRole("table", {
      name: "Cost entries, newest first",
    });
    await within(table).findByText("Diesel #2 (on-road)");
    expect(table).not.toHaveTextContent("DIESEL_2");
    expect(table).toHaveTextContent("7,500");
  });

  it("adds an entry: inline validation, then the payload with the decimal string", async () => {
    (createCostEntry as jest.Mock).mockResolvedValue({
      entry,
      warnings: [],
    });
    render(<CostEntriesPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Add cost entry" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Add cost entry",
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add entry" }));
    expect(
      await within(dialog).findByText("Pick a product."),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText("Enter the unit cost."),
    ).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("combobox", { name: /^Product/ }));
    fireEvent.click(await screen.findByRole("option", { name: /Kerosene/ }));
    fireEvent.change(within(dialog).getByLabelText(/^Effective from/), {
      target: { value: "2026-10-02T08:00" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Unit cost/), {
      target: { value: "2.4512345" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add entry" }));
    await waitFor(() => expect(createCostEntry).toHaveBeenCalled());
    expect((createCostEntry as jest.Mock).mock.calls[0][0]).toMatchObject({
      product_code: "KEROSENE",
      unit_cost_usd: "2.4512345",
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Add cost entry" }),
      ).toBeNull(),
    );
  });

  it("supersede needs a reason and maps a 409 to the dialog banner", async () => {
    (supersedeCostEntry as jest.Mock).mockRejectedValue(
      new ApiError("Conflict", 409),
    );
    render(<CostEntriesPage />);
    await screen.findByText("Diesel #2 (on-road)");
    fireEvent.click(
      screen.getByRole("button", { name: /Actions for Cost entry mce_1/ }),
    );
    fireEvent.click(await screen.findByRole("menuitem", { name: "Supersede" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Supersede cost entry",
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save new version" }),
    );
    expect(
      await within(dialog).findByText("Enter a reason for the change."),
    ).toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText(/^Reason for the change/), {
      target: { value: "Supplier correction" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save new version" }),
    );
    expect(
      await within(dialog).findByText(
        "An active entry like this already exists.",
      ),
    ).toBeInTheDocument();
  });

  it("voids with a reason", async () => {
    (voidCostEntry as jest.Mock).mockResolvedValue(undefined);
    render(<CostEntriesPage />);
    await screen.findByText("Diesel #2 (on-road)");
    fireEvent.click(
      screen.getByRole("button", { name: /Actions for Cost entry mce_1/ }),
    );
    fireEvent.click(await screen.findByRole("menuitem", { name: "Void" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Void cost entry",
    });
    fireEvent.change(within(dialog).getByLabelText(/^Reason/), {
      target: { value: "Duplicate" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Void entry" }));
    await waitFor(() =>
      expect(voidCostEntry).toHaveBeenCalledWith("mce_1", "Duplicate"),
    );
  });
});

describe("Margin settings", () => {
  const settings = {
    wac_window_days: 30,
    rack_staleness_days: 4,
    floor_micros: 100_000,
    product_floors: { DIESEL_2: 150_000 },
    timezone: "America/Chicago",
    updated_at: null,
    updated_by: null,
  };

  it("shows floors by product name and saves edited values", async () => {
    (getMarginSettings as jest.Mock).mockResolvedValue(settings);
    (updateMarginSettings as jest.Mock).mockResolvedValue({
      settings: { ...settings, wac_window_days: 14 },
      warnings: [],
    });
    render(<MarginSettingsForm />);
    expect(
      await screen.findByText(/Diesel #2 \(on-road\) \$0\.150000/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit settings" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Edit margin settings",
    });
    const wac = within(dialog).getByLabelText(/^Weighted-average window/);
    fireEvent.change(wac, { target: { value: "400" } });
    fireEvent.blur(wac);
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save settings" }),
    );
    expect(
      await within(dialog).findByText("Enter 1 to 365 days."),
    ).toBeInTheDocument();
    fireEvent.change(wac, { target: { value: "14" } });
    fireEvent.blur(wac);
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save settings" }),
    );
    await waitFor(() => expect(updateMarginSettings).toHaveBeenCalled());
    expect((updateMarginSettings as jest.Mock).mock.calls[0][0]).toEqual({
      wac_window_days: 14,
      rack_staleness_days: 4,
      floor_usd_per_gallon: "0.100000",
      product_floors: { DIESEL_2: "0.150000" },
      timezone: "America/Chicago",
    });
  });
});
