/**
 * Tests for InvoicesListPage.
 *
 * Focus: the previously-dead customer filter is now wired through a
 * CustomerPicker (backed by getCustomers) and filters invoices by customer.
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

jest.mock("../../../services/commerceApi", () => ({
  getInvoices: jest.fn(),
  getCustomers: jest.fn(),
}));

// Export CSV button: stub the download and the session roles.
jest.mock("../../../services/exportApi", () => ({
  downloadCsvExport: jest.fn(),
}));
jest.mock("../../../utils/auth", () => ({
  ...jest.requireActual("../../../utils/auth"),
  getCurrentUserRoles: jest.fn(async () => []),
}));

import { ApiError } from "../../../services/api";
import { getCustomers, getInvoices } from "../../../services/commerceApi";
import { downloadCsvExport } from "../../../services/exportApi";
import { getCurrentUserRoles } from "../../../utils/auth";
import InvoicesListPage from "../InvoicesListPage";

const mockGetInvoices = getInvoices as jest.MockedFunction<typeof getInvoices>;
const mockGetCustomers = getCustomers as jest.MockedFunction<
  typeof getCustomers
>;

function invoicesResponse() {
  return {
    data: [],
    cursor: null,
    has_more: false,
    request_id: "req-1",
  };
}

function customersResponse() {
  return {
    data: [
      { customer_id: "CUST-1", display_name: "Acme Fuel Co", status: "active" },
      { customer_id: "CUST-2", display_name: "Beta Corp", status: "active" },
    ],
    cursor: null,
    has_more: false,
    request_id: "req-2",
  };
}

describe("InvoicesListPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetInvoices.mockResolvedValue(invoicesResponse() as any);
    mockGetCustomers.mockResolvedValue(customersResponse() as any);
  });

  it("loads the customer roster when the Filters popover opens", async () => {
    render(<InvoicesListPage />);
    await waitFor(() => expect(mockGetInvoices).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());
  });

  it("filters invoices by the selected customer", async () => {
    render(<InvoicesListPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Filters" }));
    await waitFor(() => expect(mockGetCustomers).toHaveBeenCalled());

    fireEvent.click(await screen.findByLabelText("Customer"));
    fireEvent.click(await screen.findByText("Beta Corp"));

    await waitFor(() =>
      expect(mockGetInvoices).toHaveBeenCalledWith(
        expect.objectContaining({ customer_id: "CUST-2" }),
      ),
    );
  });

  it("shows the module-disabled state for INVOICING_DISABLED", async () => {
    mockGetInvoices.mockRejectedValue(
      new ApiError(
        "Commerce invoicing module is not enabled for this tenant",
        404,
        "INVOICING_DISABLED",
      ),
    );

    render(<InvoicesListPage />);

    expect(
      await screen.findByRole("heading", {
        name: "Invoicing isn't enabled for your account",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("Invoices")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("group", { name: "Invoice status" })).toBeNull();
    expect(
      screen.getByRole("button", { name: /Back to Today/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("keeps the error banner for other failures", async () => {
    mockGetInvoices.mockRejectedValue(new ApiError("boom", 500));

    render(<InvoicesListPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});

describe("InvoicesListPage — Export CSV", () => {
  const mockDownload = downloadCsvExport as jest.MockedFunction<
    typeof downloadCsvExport
  >;
  const mockRoles = getCurrentUserRoles as jest.MockedFunction<
    typeof getCurrentUserRoles
  >;

  beforeEach(() => {
    jest.clearAllMocks();
    mockGetInvoices.mockResolvedValue(invoicesResponse() as any);
    mockGetCustomers.mockResolvedValue(customersResponse() as any);
    mockDownload.mockResolvedValue({ filename: "invoices.csv" });
  });

  it("is hidden for a dispatcher (invoice export is admin only)", async () => {
    mockRoles.mockResolvedValue(["dispatcher"]);
    render(<InvoicesListPage />);
    await waitFor(() => expect(mockRoles).toHaveBeenCalled());
    await act(async () => {});
    expect(
      screen.queryByRole("button", { name: /Export CSV/ }),
    ).not.toBeInTheDocument();
  });

  it("exports the current status filter for an admin", async () => {
    mockRoles.mockResolvedValue(["admin"]);
    render(<InvoicesListPage />);
    await screen.findByRole("button", {
      name: /^Export CSV ?: invoices$/,
    });
    fireEvent.click(screen.getByRole("button", { name: /^Overdue/ }));
    await waitFor(() =>
      expect(mockGetInvoices).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "overdue" }),
      ),
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /^Export CSV ?: invoices$/ }),
      );
    });
    expect(mockDownload).toHaveBeenCalledWith("invoices", {
      status: "overdue",
      customer_id: "",
    });
  });
});
describe("InvoicesListPage — table (task 3.4)", () => {
  const row = (n: number, status: string) => ({
    invoice_id: `inv_${n}`,
    invoice_number: `INV-${1000 + n}`,
    status,
    total_cents: 123450,
    remaining_cents: 23450,
    due_date: "2026-10-31",
    qbo_push_state: "dead_letter",
  });
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetCustomers.mockResolvedValue(customersResponse() as any);
  });
  it("shows statuses by name with icons, formatted money and a calendar due date", async () => {
    mockGetInvoices.mockResolvedValue({
      data: [row(1, "open"), row(2, "partial"), row(3, "void")],
      cursor: null,
      has_more: false,
      request_id: "r",
    } as any);
    render(<InvoicesListPage onSelectInvoice={jest.fn()} />);
    const table = await screen.findByRole("table", { name: "Invoices" });
    expect(table).toHaveTextContent("Partially paid");
    expect(table).toHaveTextContent("Void");
    expect(table).toHaveTextContent("$1,234.50");
    expect(table).toHaveTextContent("Sat 31 Oct 2026");
    expect(table).toHaveTextContent("Failed");
    expect(table.querySelector('[data-status="open"]')).toHaveAttribute(
      "data-icon",
      "FileText",
    );
    expect(table).not.toHaveTextContent("dead_letter");
  });
  it("pages forward with the API cursor and back to page 1", async () => {
    mockGetInvoices
      .mockResolvedValueOnce({
        data: [row(1, "open")],
        cursor: "c2",
        has_more: true,
        request_id: "r",
      } as any)
      .mockResolvedValue({
        data: [row(2, "paid")],
        cursor: null,
        has_more: false,
        request_id: "r",
      } as any);
    render(<InvoicesListPage onSelectInvoice={jest.fn()} />);
    await screen.findByText("INV-1001");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await screen.findByText("INV-1002");
    expect(mockGetInvoices).toHaveBeenLastCalledWith(
      expect.objectContaining({ cursor: "c2" }),
    );
    fireEvent.click(screen.getByRole("button", { name: /previous/i }));
    await waitFor(() =>
      expect(mockGetInvoices).toHaveBeenLastCalledWith(
        expect.not.objectContaining({ cursor: expect.anything() }),
      ),
    );
  });
});
