/** UI-3 for InvoiceTable (R5.1, PD23): table + narrow stacked view. */
import { render, screen, within } from "@testing-library/react";
import { invoice } from "../__fixtures__/portal";
import InvoiceTable from "../InvoiceTable";

describe("InvoiceTable", () => {
  const invoices = [
    invoice(),
    invoice({
      invoice_id: "QA-INV-2",
      invoice_number: "1002",
      status_code: "partial",
      status_label: "Partially paid",
      remaining_cents: 2550,
    }),
  ];

  it("renders one row per invoice with a link, status text and USD money", () => {
    render(<InvoiceTable invoices={invoices} />);
    const table = screen.getByRole("table", { name: "Invoices" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(
      within(table).getByRole("link", { name: "Invoice 1001" }),
    ).toHaveAttribute("href", "/portal/invoices/QA-INV-1");
    expect(within(table).getByText("Partially paid")).toBeInTheDocument();
    expect(within(table).getAllByText("$1,050.00").length).toBeGreaterThan(0);
    expect(within(table).getByText("$25.50")).toBeInTheDocument();
    expect(within(table).getAllByText("Oct 31, 2026")).toHaveLength(2);
  });

  it("has a stacked list for narrow screens with the same invoices", () => {
    render(<InvoiceTable invoices={invoices} />);
    const list = screen.getByRole("list", { name: "Invoices" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Partially paid")).toBeInTheDocument();
  });

  it("says when there is nothing to show", () => {
    render(<InvoiceTable invoices={[]} />);
    expect(screen.getByText("No invoices to show.")).toBeInTheDocument();
  });
});
