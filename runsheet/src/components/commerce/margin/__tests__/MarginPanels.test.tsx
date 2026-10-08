/**
 * Margin panels: CostImportDialog (dry run, then "Import N rows"), the
 * Summary labels, MarginRecomputePanel (labels, required reason, 409 alert,
 * completed status), MarginAlertsPanel grouping and MarginHub keyboard tabs.
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
  importCostEntries: jest.fn(),
  getMarginSummary: jest.fn(),
  startMarginRecompute: jest.fn(),
  getMarginRecomputeRun: jest.fn(),
  getMarginAlerts: jest.fn(),
  resolveMarginAlert: jest.fn(),
  getMarginRecords: jest.fn(async () => ({ items: [], next_cursor: null })),
  getMarginSettings: jest.fn(async () => ({
    wac_window_days: 30,
    rack_staleness_days: 4,
    floor_micros: 100_000,
    product_floors: {},
    timezone: "America/Chicago",
    feed_activated_at: null,
    updated_by: null,
    updated_at: null,
    persisted: false,
  })),
}));
jest.mock("../../../../utils/auth", () => ({
  ...jest.requireActual("../../../../utils/auth"),
  getCurrentUserRoles: jest.fn(async () => ["admin"]),
}));

import { ApiError } from "../../../../services/api";
import {
  getMarginAlerts,
  getMarginRecomputeRun,
  getMarginSummary,
  importCostEntries,
  type MarginAlert,
  type MarginSummary,
  resolveMarginAlert,
  type SummaryBlock,
  startMarginRecompute,
} from "../../../../services/marginApi";
import CostImportDialog from "../CostImportDialog";
import MarginAlertsPanel from "../MarginAlertsPanel";
import MarginHub from "../MarginHub";
import MarginRecomputePanel, {
  RECOMPUTE_HELP,
  RECOMPUTE_POLL_MS,
} from "../MarginRecomputePanel";
import MarginSummaryPanel from "../MarginSummaryPanel";

const mockImport = importCostEntries as jest.MockedFunction<
  typeof importCostEntries
>;
const mockSummary = getMarginSummary as jest.MockedFunction<
  typeof getMarginSummary
>;
const mockStart = startMarginRecompute as jest.MockedFunction<
  typeof startMarginRecompute
>;
const mockRun = getMarginRecomputeRun as jest.MockedFunction<
  typeof getMarginRecomputeRun
>;
const mockAlerts = getMarginAlerts as jest.MockedFunction<
  typeof getMarginAlerts
>;
const mockResolve = resolveMarginAlert as jest.MockedFunction<
  typeof resolveMarginAlert
>;

beforeEach(() => {
  jest.clearAllMocks();
  jest.useRealTimers();
});

// ─── CostImportDialog ────────────────────────────────────────────────────────

describe("CostImportDialog", () => {
  const file = new File(["kind,product_code\n"], "costs.csv", {
    type: "text/csv",
  });

  function pick() {
    fireEvent.change(screen.getByLabelText("Cost entries CSV"), {
      target: { files: [file] },
    });
  }

  it("shows dry-run row errors and imports nothing", async () => {
    mockImport.mockRejectedValueOnce(
      new ApiError("Cost entry import failed", 422, "VALIDATION_ERROR", {
        errors: [
          {
            row: 3,
            loc: ["unit_cost_usd"],
            msg: "send as a string",
            type: "invalid_decimal",
          },
        ],
      }),
    );
    render(
      <CostImportDialog isOpen onClose={jest.fn()} onImported={jest.fn()} />,
    );
    pick();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Check file" }));
    });
    expect(mockImport).toHaveBeenCalledWith(file, true);
    const alert = screen
      .getAllByRole("alert")
      .find((el) => el.textContent?.includes("Row 3"));
    expect(alert?.textContent).toContain(
      "Row 3: unit_cost_usd: send as a string",
    );
    expect(
      screen.queryByRole("button", { name: /^Import \d+ rows$/ }),
    ).not.toBeInTheDocument();
  });

  it("dry run then Import N rows", async () => {
    const onImported = jest.fn();
    mockImport
      .mockResolvedValueOnce({
        dry_run: true,
        rows_total: 3,
        rows_valid: 2,
        duplicates: [{ row: 3, natural_key: "k", duplicate_of_row: 1 }],
        created_entry_ids: [],
        warnings: [],
      })
      .mockResolvedValueOnce({
        dry_run: false,
        rows_total: 3,
        rows_valid: 2,
        duplicates: [],
        created_entry_ids: ["mce_1", "mce_2"],
        warnings: [],
        import_batch_id: "b1",
      });
    render(
      <CostImportDialog isOpen onClose={jest.fn()} onImported={onImported} />,
    );
    pick();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Check file" }));
    });
    expect(
      screen.getByText("Dry run: 2 of 3 rows can be imported."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Row 3:.*repeats row 1/)).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Import 2 rows" }));
    });
    expect(mockImport).toHaveBeenLastCalledWith(file, false);
    expect(onImported).toHaveBeenCalledWith(2);
    expect(screen.getByText("Imported 2 rows.")).toBeInTheDocument();
  });
});

// ─── MarginSummaryPanel ──────────────────────────────────────────────────────

function block(overrides: Partial<SummaryBlock> = {}): SummaryBlock {
  return {
    records: 2,
    gallons_ugal: 2_000_000_000,
    revenue_cents: 600_000,
    revenue_cents_with_cost: 300_000,
    revenue_cents_missing_cost: 300_000,
    cost_cents: 250_000,
    margin_cents: 50_000,
    margin_bp: 1667,
    margin_pct: "16.67",
    flag_counts: {},
    missing_cost_share_bp: 5000,
    ...overrides,
  };
}

describe("MarginSummaryPanel", () => {
  it("labels costed and uncosted revenue and shows sources not computed", async () => {
    const summary: MarginSummary = {
      group_by: "day",
      start_date: "2026-09-02",
      end_date: "2026-10-01",
      timezone: "America/Chicago",
      groups: [{ key: "2026-10-01", ...block() }],
      totals: block(),
      skipped_sources: { count: 4, sample: [] },
    };
    mockSummary.mockResolvedValue(summary);
    render(<MarginSummaryPanel />);
    expect(
      await screen.findByText("Sources not computed: 4"),
    ).toBeInTheDocument();
    const totals = screen.getByRole("table", { name: "Totals" });
    expect(
      within(totals).getByRole("rowheader", { name: "Revenue (costed)" }),
    ).toBeInTheDocument();
    expect(
      within(totals).getByRole("rowheader", { name: "Revenue (no cost)" }),
    ).toBeInTheDocument();
    expect(
      within(totals).getByRole("rowheader", { name: "Cost (costed records)" }),
    ).toBeInTheDocument();
    const costRow = within(totals)
      .getByRole("rowheader", { name: "Cost (costed records)" })
      .closest("tr");
    expect(costRow?.textContent).toContain("$2,500.00");
  });

  it("asks for a shorter range on 422", async () => {
    mockSummary.mockRejectedValue(new ApiError("bad", 422));
    render(<MarginSummaryPanel />);
    const alert = await screen.findByText(/at most 92 days/);
    expect(alert).toHaveAttribute("role", "alert");
  });
});

// ─── MarginRecomputePanel ────────────────────────────────────────────────────

describe("MarginRecomputePanel", () => {
  function fill(reason: string) {
    fireEvent.change(screen.getByLabelText("Sale date (as of) from"), {
      target: { value: "2026-09-01" },
    });
    fireEvent.change(screen.getByLabelText("Sale date (as of) to"), {
      target: { value: "2026-09-30" },
    });
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: reason },
    });
  }

  async function submit() {
    await act(async () => {
      fireEvent.submit(
        screen
          .getByRole("button", { name: "Start recompute" })
          .closest("form") as HTMLFormElement,
      );
    });
  }

  it("has the sale-date labels, help text and a required reason", async () => {
    render(<MarginRecomputePanel />);
    expect(screen.getByLabelText("Sale date (as of) from")).toHaveAttribute(
      "type",
      "date",
    );
    expect(screen.getByLabelText("Sale date (as of) to")).toHaveAttribute(
      "type",
      "date",
    );
    expect(screen.getByText(RECOMPUTE_HELP)).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toBeRequired();
    expect(screen.getByLabelText("Only records with no cost")).toBeChecked();
    fill("   ");
    await submit();
    expect(mockStart).not.toHaveBeenCalled();
    expect(screen.getByText("Enter a reason.")).toHaveAttribute(
      "role",
      "alert",
    );
  });

  it("shows 409 as an alert", async () => {
    mockStart.mockRejectedValue(
      new ApiError("running", 409, "MARGIN_RECOMPUTE_RUNNING"),
    );
    render(<MarginRecomputePanel />);
    fill("late BOLs");
    await submit();
    expect(screen.getByText("A recompute is already running")).toHaveAttribute(
      "role",
      "alert",
    );
  });

  it("polls every 5 s and shows the completed counts in a status", async () => {
    jest.useFakeTimers();
    mockStart.mockResolvedValue({ run_id: "run_1" });
    mockRun
      .mockResolvedValueOnce({
        run_id: "run_1",
        status: "running",
        start_date: "",
        end_date: "",
        counts: {},
      })
      .mockResolvedValueOnce({
        run_id: "run_1",
        status: "completed",
        start_date: "",
        end_date: "",
        counts: {
          sources: 5,
          written: 3,
          skipped: 2,
          invalid_inputs: 0,
          errors: 0,
        },
      });
    render(<MarginRecomputePanel />);
    fill("late BOLs");
    await submit();
    expect(mockStart).toHaveBeenCalledWith({
      start_date: "2026-09-01",
      end_date: "2026-09-30",
      stages: ["invoice", "delivery"],
      only_missing: true,
      reason: "late BOLs",
    });
    await act(async () => {
      jest.advanceTimersByTime(RECOMPUTE_POLL_MS);
    });
    expect(mockRun).toHaveBeenCalledTimes(1);
    await act(async () => {
      jest.advanceTimersByTime(RECOMPUTE_POLL_MS);
    });
    await waitFor(() => expect(mockRun).toHaveBeenCalledTimes(2));
    const status = screen.getByText(
      /Recompute completed: 5 sources, 3 written/,
    );
    expect(status).toHaveAttribute("role", "status");
    jest.useRealTimers();
  });
});

// ─── MarginAlertsPanel ───────────────────────────────────────────────────────

function alert(overrides: Partial<MarginAlert>): MarginAlert {
  return {
    alert_id: "a1",
    alert_type: "negative_margin",
    severity: "high",
    status: "open",
    record_id: "mr_1",
    order_id: "ORD-1",
    run_id: null,
    proposal_id: null,
    customer_id: "CUST-1",
    product_code: "DIESEL_2",
    details: {},
    created_at: "2026-10-01T15:00:00+00:00",
    resolved_by: null,
    resolved_at: null,
    resolution_note: null,
    ...overrides,
  };
}

describe("MarginAlertsPanel", () => {
  it("groups by order with Other last and resolves an alert", async () => {
    const onChanged = jest.fn();
    mockAlerts.mockResolvedValue({
      items: [
        alert({ alert_id: "a1" }),
        alert({
          alert_id: "d1",
          alert_type: "recompute_digest",
          order_id: null,
          severity: "info",
          run_id: "r1",
        }),
        alert({
          alert_id: "p1",
          alert_type: "leakage_proposal",
          status: "pending_review",
          order_id: "ORD-2",
        }),
      ],
      next_cursor: null,
      total: 3,
    });
    mockResolve.mockResolvedValue(alert({ status: "acknowledged" }));
    render(<MarginAlertsPanel onChanged={onChanged} />);
    const headings = (await screen.findAllByRole("heading", { level: 3 })).map(
      (h) => h.textContent,
    );
    expect(headings).toEqual(["Order ORD-1", "Order ORD-2", "Other"]);
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    await act(async () => {
      const group = screen.getByRole("heading", { name: "Order ORD-1" })
        .parentElement as HTMLElement;
      fireEvent.click(
        within(group).getByRole("button", { name: "Acknowledge" }),
      );
    });
    expect(mockResolve).toHaveBeenCalledWith("a1", "acknowledge");
    expect(onChanged).toHaveBeenCalled();
  });
});

// ─── MarginHub ───────────────────────────────────────────────────────────────

describe("MarginHub", () => {
  it("is a tablist with arrow-key navigation", async () => {
    render(<MarginHub />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual([
      "Records",
      "Summary",
      "Alerts",
      "Cost basis",
      "Cost entries",
      "Settings",
    ]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    tabs[0].focus();
    mockSummary.mockResolvedValue({
      group_by: "day",
      start_date: "",
      end_date: "",
      timezone: "UTC",
      groups: [],
      totals: block(),
      skipped_sources: { count: 0, sample: [] },
    });
    await act(async () => {
      fireEvent.keyDown(tabs[0], { key: "ArrowRight" });
    });
    expect(screen.getByRole("tab", { name: "Summary" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(document.activeElement).toBe(
      screen.getByRole("tab", { name: "Summary" }),
    );
    await act(async () => {
      fireEvent.keyDown(screen.getByRole("tab", { name: "Summary" }), {
        key: "End",
      });
    });
    expect(screen.getByRole("tab", { name: "Settings" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tabpanel")).toHaveAttribute(
      "aria-labelledby",
      "margin-tab-settings",
    );
  });
});
