/** InvoiceTable (R5.1, R14.9, PD23): rows below 1024 px, a table from 1024 px. */
import { render, screen, within } from "@testing-library/react";
import { invoice } from "../__fixtures__/portal";
import InvoiceTable from "../InvoiceTable";
import { date } from "../portalFormat";

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

function wideScreen(on: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: on && query.includes("min-width: 1024px"),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}

const realMatchMedia = window.matchMedia;
afterEach(() => {
  window.matchMedia = realMatchMedia;
});

describe("InvoiceTable", () => {
  it("renders a table from 1024 px with links, badges and USD money", () => {
    wideScreen(true);
    render(<InvoiceTable invoices={invoices} />);
    const table = screen.getByRole("table", { name: "Invoices" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(
      within(table).getByRole("link", { name: "Invoice 1001" }),
    ).toHaveAttribute("href", "/portal/invoices/QA-INV-1");
    const partial = within(table).getByText("Partially paid");
    expect(partial.closest("[data-status]")).toHaveAttribute(
      "data-status",
      "partial",
    );
    expect(within(table).getAllByText("$1,050.00").length).toBeGreaterThan(0);
    expect(within(table).getByText("$25.50")).toBeInTheDocument();
    // A bare due date is that calendar day, never shifted by the zone.
    expect(within(table).getAllByText(date("2026-10-31"))).toHaveLength(2);
    expect(date("2026-10-31", new Date(2026, 5, 1))).toBe("Sat 31 Oct");
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("renders two-line rows below 1024 px with the same invoices", () => {
    wideScreen(false);
    render(<InvoiceTable invoices={invoices} />);
    const list = screen.getByRole("list", { name: "Invoices" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Partially paid")).toBeInTheDocument();
    expect(within(list).getByText("$25.50")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
});
