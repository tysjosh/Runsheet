/**
 * Price books (task 3.4): DataTable list, the lg sectioned FormDialog with the
 * sm add-rule sub-dialog, Activate as a row action, and the price check.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../../services/commerceApi", () => ({
  getPriceBooks: jest.fn(),
  getPriceBook: jest.fn(),
  createPriceBook: jest.fn(),
  updatePriceBook: jest.fn(),
  activatePriceBook: jest.fn(),
  resolvePricing: jest.fn(),
}));

import { ApiError } from "../../../services/api";
import {
  activatePriceBook,
  createPriceBook,
  getPriceBook,
  getPriceBooks,
  updatePriceBook,
} from "../../../services/commerceApi";
import PriceBookEditor from "../PriceBookEditor";

const m = {
  list: getPriceBooks as jest.Mock,
  get: getPriceBook as jest.Mock,
  create: createPriceBook as jest.Mock,
  update: updatePriceBook as jest.Mock,
  activate: activatePriceBook as jest.Mock,
};

const book = {
  price_book_id: "pb_1",
  tenant_id: "t",
  name: "Q4 Commercial",
  description: "Winter",
  status: "draft",
  rule_count: 1,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-02T00:00:00Z",
};
const rule = {
  rule_id: "r_1",
  price_book_id: "pb_1",
  product_code: "DIESEL_2",
  scope_type: "default",
  scope_value: "",
  unit_price_cents: 389,
  min_quantity_gallons: 500,
  effective_from: "2026-10-01",
  effective_to: null,
  created_at: "2026-10-01T00:00:00Z",
};

beforeEach(() => {
  jest.clearAllMocks();
  m.list.mockResolvedValue({ data: [book], request_id: "r" });
  m.get.mockResolvedValue({
    data: { ...book, rules: [rule] },
    request_id: "r",
  });
});

it("lists books with a status badge and opens the editor with rules by product name", async () => {
  render(<PriceBookEditor />);
  await screen.findByText("Q4 Commercial");
  const table = screen.getByRole("table", { name: "Price books" });
  expect(within(table).getByText("Draft")).toBeInTheDocument();
  fireEvent.click(within(table).getByText("Q4 Commercial"));
  const dialog = await screen.findByRole("dialog", {
    name: "Edit Q4 Commercial",
  });
  const rules = within(dialog).getByRole("list", { name: "Pricing rules" });
  expect(rules).toHaveTextContent("Diesel #2 (on-road)");
  expect(rules).toHaveTextContent("$3.89/gal");
  expect(rules).toHaveTextContent("from 500 gal");
  expect(rules).not.toHaveTextContent("DIESEL_2");
});

it("adds a rule in the sub-dialog and saves it with the book", async () => {
  m.update.mockResolvedValue({ data: book, request_id: "r" });
  render(<PriceBookEditor />);
  fireEvent.click(await screen.findByText("Q4 Commercial"));
  await screen.findByRole("dialog", { name: "Edit Q4 Commercial" });
  fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
  const sub = await screen.findByRole("dialog", { name: "Add rule" });
  // Validation: product and price are required.
  fireEvent.click(within(sub).getByRole("button", { name: "Add rule" }));
  expect(await within(sub).findByText("Pick a product.")).toBeInTheDocument();
  expect(within(sub).getByText("Enter a price.")).toBeInTheDocument();
  // Escape closes only the sub-dialog (stacked dialogs).
  fireEvent.keyDown(document.activeElement ?? document.body, {
    key: "Escape",
  });
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Add rule" })).toBeNull(),
  );
  expect(
    screen.getByRole("dialog", { name: "Edit Q4 Commercial" }),
  ).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Add rule" }));
  const sub2 = await screen.findByRole("dialog", { name: "Add rule" });
  fireEvent.click(within(sub2).getByRole("combobox", { name: /^Product/ }));
  fireEvent.click(await screen.findByRole("option", { name: /Kerosene/ }));
  fireEvent.change(within(sub2).getByLabelText(/^Unit price/), {
    target: { value: "4.25" },
  });
  fireEvent.blur(within(sub2).getByLabelText(/^Unit price/));
  fireEvent.click(within(sub2).getByRole("button", { name: "Add rule" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Add rule" })).toBeNull(),
  );
  const outer = screen.getByRole("dialog", { name: "Edit Q4 Commercial" });
  expect(within(outer).getByText("Rules (2)")).toBeInTheDocument();
  fireEvent.click(within(outer).getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(m.update).toHaveBeenCalled());
  const [id, payload] = m.update.mock.calls[0];
  expect(id).toBe("pb_1");
  expect(payload.rules).toHaveLength(2);
  expect(payload.rules[1]).toMatchObject({
    product_code: "KEROSENE",
    unit_price_cents: 425,
    scope_type: "default",
  });
  expect(payload.rules[0]).not.toHaveProperty("rule_id");
});

it("creates a book, showing envelope field errors inline", async () => {
  m.create
    .mockRejectedValueOnce(
      new ApiError("Invalid", 422, "VALIDATION_ERROR", {
        fields: { name: "Name already used" },
      }),
    )
    .mockResolvedValueOnce({ data: { ...book, price_book_id: "pb_2" } });
  render(<PriceBookEditor />);
  await screen.findByText("Q4 Commercial");
  fireEvent.click(screen.getByRole("button", { name: "New price book" }));
  const dialog = await screen.findByRole("dialog", { name: "New price book" });
  fireEvent.change(within(dialog).getByLabelText(/^Name/), {
    target: { value: "Q4 Commercial" },
  });
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Create price book" }),
  );
  expect(
    await within(dialog).findByText("Name already used"),
  ).toBeInTheDocument();
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Create price book" }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "New price book" })).toBeNull(),
  );
  expect(m.create).toHaveBeenLastCalledWith({
    name: "Q4 Commercial",
    description: undefined,
    rules: [],
  });
});

it("activates a draft from the row menu", async () => {
  m.activate.mockResolvedValue({ data: { ...book, status: "active" } });
  render(<PriceBookEditor />);
  await screen.findByText("Q4 Commercial");
  fireEvent.click(
    screen.getByRole("button", { name: /Actions for Q4 Commercial/ }),
  );
  fireEvent.click(await screen.findByRole("menuitem", { name: "Activate" }));
  await waitFor(() => expect(m.activate).toHaveBeenCalledWith("pb_1"));
});
