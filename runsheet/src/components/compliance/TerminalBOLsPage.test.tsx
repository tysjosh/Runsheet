/**
 * Compliance → BOLs (task 3.5): products by name, formatted gallons, status
 * chips, Upload BOL FormDialog.
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
  getTerminalBOLs: jest.fn(),
  uploadTerminalBOL: jest.fn(),
}));

import {
  getTerminalBOLs,
  uploadTerminalBOL,
} from "../../services/complianceApi";
import TerminalBOLsPage from "./TerminalBOLsPage";

beforeEach(() => {
  jest.clearAllMocks();
  (getTerminalBOLs as jest.Mock).mockResolvedValue({
    data: [
      {
        bol_id: "b1",
        load_number: "LD-1",
        product_code: "GASOLINE_REG",
        gross_gallons: 8012.456,
        net_gallons: 7990,
        supplier_name: "Valero",
        terminal_name: "Houston",
        driver_id: "DRV-1",
        timestamp: "2026-10-01T12:00:00Z",
        status: "pending_confirmation",
      },
    ],
    pagination: { total_pages: 1 },
  });
});

it("shows the product by name, gallons formatted, and a status badge", async () => {
  render(<TerminalBOLsPage />);
  const table = await screen.findByRole("table", { name: "Terminal BOLs" });
  await within(table).findByText("LD-1");
  expect(table).toHaveTextContent("Regular unleaded");
  expect(table).not.toHaveTextContent("GASOLINE_REG");
  expect(table).toHaveTextContent("8,012.5 gal");
  expect(within(table).getByText("Pending confirmation")).toBeInTheDocument();
});

it("filters by status chip", async () => {
  render(<TerminalBOLsPage />);
  await screen.findByText("LD-1");
  fireEvent.click(screen.getByRole("button", { name: /^Linked/ }));
  await waitFor(() =>
    expect(getTerminalBOLs).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "linked", page: 1 }),
    ),
  );
});

it("uploads through the FormDialog and requires a file", async () => {
  (uploadTerminalBOL as jest.Mock).mockResolvedValue({});
  render(<TerminalBOLsPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Upload BOL" }));
  const dialog = screen.getByRole("dialog", { name: "Upload terminal BOL" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Upload BOL" }));
  expect(
    await within(dialog).findByText("Choose a BOL document."),
  ).toBeInTheDocument();
  const file = new File(["%PDF"], "bol.pdf", { type: "application/pdf" });
  fireEvent.change(within(dialog).getByLabelText(/^BOL document/), {
    target: { files: [file] },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Upload BOL" }));
  await waitFor(() => expect(uploadTerminalBOL).toHaveBeenCalledWith(file));
});
