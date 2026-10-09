/**
 * MarginRecordsPage: a method=none record renders "Missing cost" and never
 * "$0.00" (freeze 12); filters map to query params; the CSV export is the
 * shared ExportCsvButton with type "margin", admin only.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../../../services/marginApi", () => ({
  ...jest.requireActual("../../../../services/marginApi"),
  getMarginRecords: jest.fn(),
  getMarginRecord: jest.fn(),
}));
jest.mock("../../../../services/exportApi", () => ({
  downloadCsvExport: jest.fn(async () => ({ filename: "margin.csv" })),
}));
jest.mock("../../../../utils/auth", () => ({
  ...jest.requireActual("../../../../utils/auth"),
  getCurrentUserRoles: jest.fn(async () => ["admin"]),
}));

import { downloadCsvExport } from "../../../../services/exportApi";
import {
  getMarginRecord,
  getMarginRecords,
  type MarginRecord,
  recordFilterParams,
} from "../../../../services/marginApi";
import { getCurrentUserRoles } from "../../../../utils/auth";
import MarginRecordsPage from "../MarginRecordsPage";

const mockRecords = getMarginRecords as jest.MockedFunction<
  typeof getMarginRecords
>;
const mockRecord = getMarginRecord as jest.MockedFunction<
  typeof getMarginRecord
>;
const mockDownload = downloadCsvExport as jest.MockedFunction<
  typeof downloadCsvExport
>;
const mockRoles = getCurrentUserRoles as jest.MockedFunction<
  typeof getCurrentUserRoles
>;

function record(overrides: Partial<MarginRecord> = {}): MarginRecord {
  return {
    record_id: "mr_1",
    stage: "delivery",
    source_key: "order:ORD-1",
    order_id: "ORD-1",
    invoice_id: null,
    line_index: null,
    customer_id: "CUST-1",
    account_id: "ACCT-1",
    product_code: "DIESEL_2",
    terminal_id: "TERM-1",
    gallons_ugal: 1_000_000_000,
    unit_price_micros: 3_000_000,
    revenue_cents: 300_000,
    method: "wac",
    product_cost_micros: 2_500_000,
    adders_micros: 0,
    landed_cost_micros: 2_500_000,
    cost_cents: 250_000,
    margin_cents: 50_000,
    margin_per_gallon_micros: 500_000,
    margin_bp: 1667,
    margin_pct: "16.67",
    no_cost_reason: null,
    flags: [],
    floor_micros_used: 100_000,
    cost_snapshot: { method: "wac" },
    as_of: "2026-10-01T15:00:00+00:00",
    version: 1,
    status: "active",
    origin: "live",
    frozen_at: null,
    computed_at: "2026-10-01T15:00:01+00:00",
    ...overrides,
  };
}

const TZ = "America/Chicago";

const MISSING = record({
  record_id: "mr_2",
  order_id: "ORD-2",
  method: "none",
  product_cost_micros: null,
  landed_cost_micros: null,
  cost_cents: null,
  margin_cents: null,
  margin_per_gallon_micros: null,
  margin_bp: null,
  margin_pct: null,
  no_cost_reason: "no_lots_no_rack",
  flags: ["missing_cost"],
});

beforeEach(() => {
  jest.clearAllMocks();
  mockRoles.mockResolvedValue(["admin"]);
  mockRecords.mockResolvedValue({
    items: [record(), MISSING],
    next_cursor: null,
    timezone: TZ,
  });
});

async function renderPage() {
  render(<MarginRecordsPage />);
  await screen.findByText("ORD-2");
}

it("renders a method=none record as Missing cost with its reason, never $0.00", async () => {
  await renderPage();
  const row = screen.getByText("ORD-2").closest("tr") as HTMLElement;
  expect(within(row).getByText("Missing cost")).toBeInTheDocument();
  expect(
    within(row).getByText(/No purchases or rack price/),
  ).toBeInTheDocument();
  expect(row.textContent).not.toContain("$0.00");
  expect(row.textContent).not.toContain("No cost%");
  // The costed row shows its numbers.
  const costed = screen.getByText("ORD-1").closest("tr") as HTMLElement;
  expect(within(costed).getByText("$2,500.00")).toBeInTheDocument();
  expect(within(costed).getByText("16.67%")).toBeInTheDocument();
});

it("shows the sale date in the settings timezone, the filters' date axis", async () => {
  // 03:00Z on Oct 5 is 22:00 on Oct 4 in Chicago.
  mockRecords.mockResolvedValue({
    items: [record({ as_of: "2026-10-05T03:00:00+00:00" }), MISSING],
    next_cursor: null,
    timezone: TZ,
  });
  await renderPage();
  const row = screen.getByText("ORD-1").closest("tr") as HTMLElement;
  expect(within(row).getByText("2026-10-04")).toBeInTheDocument();
  expect(row.textContent).not.toContain("2026-10-05");
  expect(
    screen.getByRole("columnheader", { name: "Sale date (America/Chicago)" }),
  ).toBeInTheDocument();
});

it("maps the filters to query params", async () => {
  await renderPage();
  fireEvent.click(screen.getByRole("button", { name: "Filters" }));
  fireEvent.change(screen.getByLabelText("Sale date (as of) from"), {
    target: { value: "2026-09-01" },
  });
  fireEvent.change(screen.getByLabelText("Sale date (as of) to"), {
    target: { value: "2026-09-30" },
  });
  fireEvent.change(screen.getByLabelText("Customer"), {
    target: { value: "CUST-9" },
  });
  fireEvent.change(screen.getByLabelText("Stage"), {
    target: { value: "invoice" },
  });
  fireEvent.change(screen.getByLabelText("Flag"), {
    target: { value: "below_floor" },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
  });
  await waitFor(() => expect(mockRecords).toHaveBeenCalledTimes(2));
  const [filters, cursor] = mockRecords.mock.calls[1];
  expect(cursor).toBeNull();
  expect(recordFilterParams(filters)).toEqual({
    start_date: "2026-09-01",
    end_date: "2026-09-30",
    customer_id: "CUST-9",
    stage: "invoice",
    flag: "below_floor",
    status: "active",
  });
});

it("exports CSV with type margin and the applied filters", async () => {
  await renderPage();
  const button = await screen.findByRole("button", {
    name: /Export CSV ?: margin records/,
  });
  await act(async () => {
    fireEvent.click(button);
  });
  expect(mockDownload).toHaveBeenCalledWith("margin", { status: "active" });
});

it("hides the export from a dispatcher", async () => {
  mockRoles.mockResolvedValue(["dispatcher"]);
  await renderPage();
  await waitFor(() => expect(mockRoles).toHaveBeenCalled());
  expect(
    screen.queryByRole("button", { name: /Export CSV/ }),
  ).not.toBeInTheDocument();
});

it("loads more with the next cursor", async () => {
  mockRecords
    .mockResolvedValueOnce({
      items: [record()],
      next_cursor: "c1",
      timezone: TZ,
    })
    .mockResolvedValueOnce({
      items: [MISSING],
      next_cursor: null,
      timezone: TZ,
    });
  render(<MarginRecordsPage />);
  const more = await screen.findByRole("button", { name: "Load more" });
  await act(async () => {
    fireEvent.click(more);
  });
  expect(mockRecords.mock.calls[1][1]).toBe("c1");
  expect(await screen.findByText("ORD-2")).toBeInTheDocument();
  expect(screen.getByText("ORD-1")).toBeInTheDocument();
});

it("shows the detail drawer with the snapshot and Missing cost for a none version", async () => {
  mockRecord.mockResolvedValue({ ...MISSING, versions: [MISSING] });
  await renderPage();
  await act(async () => {
    fireEvent.click(
      screen.getByRole("button", { name: /Details for record mr_2/ }),
    );
  });
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText("Cost snapshot")).toBeInTheDocument();
  expect(within(dialog).getAllByText("Missing cost").length).toBeGreaterThan(0);
  expect(dialog.textContent).not.toContain("$0.00");
});

describe("load-failure copy (Phase 3 margin feed-off bug)", () => {
  // ApiError from the real module (marginApi is partially mocked above).
  const { ApiError } = jest.requireActual("../../../../services/api");
  it.each([
    [
      "the feed is off (404 COMMERCE_DISABLED)",
      new ApiError("Margin feed is not enabled", 404, "COMMERCE_DISABLED"),
      "Margin isn't turned on for this account.",
    ],
    [
      "the caller isn't an admin (403)",
      new ApiError("Caller lacks a required role", 403, "INSUFFICIENT_ROLE"),
      "You don't have access to margin records. Margin is for tenant admins.",
    ],
    [
      "a real server error (500)",
      new ApiError("boom", 500, "INTERNAL_ERROR"),
      "Margin records could not be loaded. Try again.",
    ],
    [
      "an unrelated 404",
      new ApiError("Not found", 404, "NOT_FOUND"),
      "Margin records could not be loaded. Try again.",
    ],
  ])("says so when %s", async (_label, error, copy) => {
    mockRecords.mockRejectedValue(error);
    render(<MarginRecordsPage />);
    expect(await screen.findByText(copy)).toBeInTheDocument();
  });
});
