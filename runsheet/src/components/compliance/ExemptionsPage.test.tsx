/**
 * Settings → Company → Exemptions (task 3.5): list with expiry badges and the
 * Add exemption FormDialog.
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
  getTaxExemptions: jest.fn(),
  createTaxExemption: jest.fn(),
}));
jest.mock("../../services/commerceApi", () => ({
  getCustomers: jest.fn().mockResolvedValue({
    data: [{ customer_id: "CUST-1", display_name: "Acme Fuel Co" }],
    has_more: false,
  }),
}));

import {
  createTaxExemption,
  getTaxExemptions,
} from "../../services/complianceApi";
import ExemptionsPage, { getExpiryStatus } from "./ExemptionsPage";

beforeEach(() => {
  jest.clearAllMocks();
  (getTaxExemptions as jest.Mock).mockResolvedValue({
    data: [
      {
        exemption_id: "e1",
        customer_id: "CUST-1",
        exemption_type: "dyed_diesel",
        certificate_number: "637M-1",
        expiry_date: "2020-01-01",
      },
    ],
    pagination: { total_pages: 1 },
  });
});

it("grades expiry by calendar day", () => {
  const now = new Date("2026-10-08T12:00:00Z");
  expect(getExpiryStatus("2026-10-08", now)).toBe("expiring_soon");
  expect(getExpiryStatus("2026-10-07", now)).toBe("expired");
  expect(getExpiryStatus("2027-01-01", now)).toBe("active");
});

it("lists exemptions by type name with an expiry badge", async () => {
  render(<ExemptionsPage />);
  const table = await screen.findByRole("table", {
    name: "Tax exemption certificates",
  });
  await within(table).findByText("Dyed Diesel (IRS 637M)");
  expect(within(table).getByText("Expired")).toBeInTheDocument();
});

it("adds an exemption through the FormDialog", async () => {
  (createTaxExemption as jest.Mock).mockResolvedValue({ data: {} });
  render(<ExemptionsPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Add Exemption" }));
  const dialog = screen.getByRole("dialog", {
    name: "Add exemption certificate",
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Add Exemption" }));
  expect(await within(dialog).findByText("Pick a customer.")).toBeInTheDocument();
  fireEvent.click(within(dialog).getByLabelText("Customer ID"));
  fireEvent.click(await screen.findByText("Acme Fuel Co"));
  fireEvent.change(within(dialog).getByLabelText(/^Exemption type/), {
    target: { value: "farm_agricultural" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Certificate number/), {
    target: { value: "F-9" },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Expiry date/), {
    target: { value: "2027-01-01" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Add Exemption" }));
  await waitFor(() =>
    expect(createTaxExemption).toHaveBeenCalledWith({
      customer_id: "CUST-1",
      exemption_type: "farm_agricultural",
      certificate_number: "F-9",
      expiry_date: "2027-01-01",
    }),
  );
});
