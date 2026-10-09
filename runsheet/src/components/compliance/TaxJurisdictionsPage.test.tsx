/**
 * Settings → Company → Tax jurisdictions (task 3.5): rates in cents with the
 * backend's tenths-of-a-cent scale, products by name, Add rate FormDialog.
 */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/complianceApi", () => ({
  ...jest.requireActual("../../services/complianceApi"),
  getTaxJurisdictions: jest.fn(),
  createTaxJurisdiction: jest.fn(),
}));

import {
  createTaxJurisdiction,
  getTaxJurisdictions,
} from "../../services/complianceApi";
import TaxJurisdictionsPage, { formatRate } from "./TaxJurisdictionsPage";

beforeEach(() => {
  jest.clearAllMocks();
  (getTaxJurisdictions as jest.Mock).mockResolvedValue({
    data: [
      {
        jurisdiction_id: "j1",
        fips_code: "48",
        jurisdiction_level: "state",
        tax_type: "excise",
        product_codes: ["DIESEL_2"],
        rate_cents_per_gallon: 200,
        effective_date: "2026-01-01",
        expiry_date: null,
      },
    ],
    pagination: { total_pages: 1 },
  });
});

it("formats stored tenths of a cent as cents", () => {
  expect(formatRate(184)).toBe("18.4¢");
  expect(formatRate(244)).toBe("24.4¢");
});

it("lists the rate in cents and the product by name", async () => {
  render(<TaxJurisdictionsPage />);
  const table = await screen.findByRole("table", {
    name: "Tax jurisdiction rates",
  });
  await within(table).findByText("20.0¢");
  expect(table).toHaveTextContent("Diesel #2 (on-road)");
  expect(table).not.toHaveTextContent("DIESEL_2");
  expect(table).toHaveTextContent("Excise");
});

it("adds a rate: products by name, 18.4¢ stored as 184", async () => {
  (createTaxJurisdiction as jest.Mock).mockResolvedValue({ data: {} });
  render(<TaxJurisdictionsPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Add Rate" }));
  const dialog = screen.getByRole("dialog", { name: "Add jurisdiction rate" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Add Rate" }));
  expect(
    await within(dialog).findByText("Pick at least one product."),
  ).toBeInTheDocument();
  fireEvent.change(within(dialog).getByLabelText(/^FIPS code/), {
    target: { value: "48201" },
  });
  fireEvent.click(within(dialog).getByLabelText(/Regular unleaded/));
  const rate = within(dialog).getByLabelText(/^Rate/);
  fireEvent.change(rate, { target: { value: "18.4" } });
  fireEvent.blur(rate);
  fireEvent.change(within(dialog).getByLabelText(/^Effective date/), {
    target: { value: "2026-11-01" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Add Rate" }));
  await waitFor(() =>
    expect(createTaxJurisdiction).toHaveBeenCalledWith(
      expect.objectContaining({
        fips_code: "48201",
        product_codes: ["GASOLINE_REG"],
        rate_cents_per_gallon: 184,
      }),
    ),
  );
});
