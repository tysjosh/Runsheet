/**
 * Fleet → Inventory (UI revamp task 3.1): toolbar chips with summary counts,
 * DataTable row menu, and the create / edit / adjust FormDialogs plus the
 * delete confirm.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../services/api", () => ({
  apiService: { getInventory: jest.fn(), updateInventoryItem: jest.fn() },
}));
jest.mock("../services/inventoryApi", () => ({
  adjustStock: jest.fn(),
  createItem: jest.fn(),
  deleteItem: jest.fn(),
  getAlerts: jest.fn(),
  getItemHistory: jest.fn(),
  getSummary: jest.fn(),
}));

import { apiService } from "../services/api";
import {
  adjustStock,
  createItem,
  deleteItem,
  getAlerts,
  getItemHistory,
  getSummary,
} from "../services/inventoryApi";
import Inventory from "./Inventory";

const item = (over: Record<string, unknown> = {}) => ({
  id: "QA-INV-1",
  name: "QA Hose",
  category: "fuel_equipment",
  location: "QA Depot",
  quantity: 1234,
  unit: "pieces",
  status: "in_stock",
  lastUpdated: "2026-10-08T13:30:00Z",
  ...over,
});

const getInventory = apiService.getInventory as jest.Mock;

beforeEach(() => {
  jest.clearAllMocks();
  getInventory.mockResolvedValue({
    data: [
      item(),
      item({ id: "QA-INV-2", name: "QA Filter", status: "low_stock" }),
    ],
  });
  (getSummary as jest.Mock).mockResolvedValue({
    data: {
      total_items: 2,
      in_stock: 1,
      low_stock: 1,
      out_of_stock: 0,
      total_value: 12_345.6,
    },
  });
  (getAlerts as jest.Mock).mockResolvedValue({ data: [] });
  (getItemHistory as jest.Mock).mockResolvedValue({ data: [] });
});

async function openMenu(name: string, entry: RegExp) {
  await screen.findByText(name);
  fireEvent.click(screen.getByRole("button", { name: `Actions for ${name}` }));
  fireEvent.click(screen.getByRole("menuitem", { name: entry }));
}

describe("Inventory", () => {
  it("shows chips with summary counts, grouped quantities and status badges", async () => {
    render(<Inventory />);
    expect(
      await screen.findByRole("button", { name: /All\s*2/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Low stock\s*1/ }),
    ).toBeInTheDocument();
    const row = screen.getByText("QA Hose").closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("1,234 pieces");
    expect(row).toHaveTextContent("In stock");
    expect(row).toHaveTextContent("Fuel equipment");
    expect(screen.getByText("Stock value $12,346")).toBeInTheDocument();
  });

  it("creates an item through the FormDialog with validation", async () => {
    (createItem as jest.Mock).mockResolvedValue({ data: item() });
    render(<Inventory />);
    await screen.findByText("QA Hose");
    fireEvent.click(screen.getByRole("button", { name: "Add item" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Add inventory item",
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Create item" }),
    );
    expect(await screen.findByText("Enter a name.")).toBeInTheDocument();
    expect(createItem).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/^Name/), {
      target: { value: "QA Nozzle" },
    });
    fireEvent.change(screen.getByLabelText(/^Location/), {
      target: { value: "QA Yard" },
    });
    fireEvent.change(screen.getByLabelText(/Unit cost/), {
      target: { value: "12.5" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Create item" }),
    );
    await waitFor(() =>
      expect(createItem).toHaveBeenCalledWith(
        expect.objectContaining({
          name: "QA Nozzle",
          location: "QA Yard",
          unit_cost: 12.5,
          quantity: 0,
          min_threshold: 5,
          max_capacity: 100,
          supplier: null,
        }),
      ),
    );
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
  });

  it("edits an item from the row menu and shows the API error inline", async () => {
    (apiService.updateInventoryItem as jest.Mock)
      .mockRejectedValueOnce(new Error("Location not found"))
      .mockResolvedValueOnce({ data: item({ location: "QA Yard 2" }) });
    render(<Inventory />);
    await openMenu("QA Hose", /Edit item/);
    const dialog = await screen.findByRole("dialog", {
      name: "Edit inventory item",
    });
    expect(screen.getByLabelText(/^Quantity/)).toHaveValue("1,234");
    fireEvent.change(screen.getByLabelText(/^Location/), {
      target: { value: "QA Yard 2" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save changes" }),
    );
    expect(await screen.findByText("Location not found")).toBeInTheDocument();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Save changes" }),
    );
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(apiService.updateInventoryItem).toHaveBeenLastCalledWith(
      "QA-INV-1",
      { quantity: 1234, status: "in_stock", location: "QA Yard 2" },
    );
  });

  it("consumes stock as a negative change", async () => {
    (adjustStock as jest.Mock).mockResolvedValue({ data: {} });
    render(<Inventory />);
    await openMenu("QA Hose", /Consume/);
    const dialog = await screen.findByRole("dialog", { name: "Consume stock" });
    fireEvent.change(screen.getByLabelText(/Quantity to deduct/), {
      target: { value: "4" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Consume" }));
    await waitFor(() =>
      expect(adjustStock).toHaveBeenCalledWith("QA-INV-1", {
        quantity_change: -4,
        reason: "used_for_maintenance",
      }),
    );
  });

  it("deletes after confirmation", async () => {
    (deleteItem as jest.Mock).mockResolvedValue({});
    render(<Inventory />);
    await openMenu("QA Hose", /Delete item/);
    const dialog = await screen.findByRole("dialog", { name: "Delete item?" });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Delete item" }),
    );
    await waitFor(() => expect(deleteItem).toHaveBeenCalledWith("QA-INV-1"));
  });
});
