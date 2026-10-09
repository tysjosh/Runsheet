/**
 * Tests for ARAgingDashboard component.
 *
 * Covers:
 * - Happy path: aging summary + history render
 * - Bucket chart display
 * - Top accounts table (up to 50)
 * - Account selection callback
 * - Error state rendering
 * - Loading state rendering
 * - Empty state for no accounts with balance
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockPush = jest.fn();
const mockBack = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, back: mockBack }),
}));

jest.mock("../../../services/commerceApi", () => ({
  getArAging: jest.fn(),
  getArAgingHistory: jest.fn(),
}));

import { ApiError } from "../../../services/api";
import { getArAging, getArAgingHistory } from "../../../services/commerceApi";
import ARAgingDashboard from "../ARAgingDashboard";

const mockGetArAging = getArAging as jest.MockedFunction<typeof getArAging>;
const mockGetArAgingHistory = getArAgingHistory as jest.MockedFunction<
  typeof getArAgingHistory
>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

function agingSummaryFixture(overrides: Record<string, unknown> = {}) {
  return {
    bucket_0_30_cents: 5000000,
    bucket_31_60_cents: 3000000,
    bucket_61_90_cents: 2000000,
    bucket_90_plus_cents: 1500000,
    total_open_cents: 11500000,
    by_account: [
      {
        account_id: "acc_001",
        display_name: "Acme Main",
        total_open_cents: 3500000,
        bucket_0_30_cents: 1500000,
        bucket_31_60_cents: 1000000,
        bucket_61_90_cents: 500000,
        bucket_90_plus_cents: 500000,
      },
      {
        account_id: "acc_002",
        display_name: "Beta Energy",
        total_open_cents: 2800000,
        bucket_0_30_cents: 1000000,
        bucket_31_60_cents: 800000,
        bucket_61_90_cents: 500000,
        bucket_90_plus_cents: 500000,
      },
    ],
    ...overrides,
  };
}

function agingHistoryFixture() {
  return [
    {
      snapshot_id: "snap_001",
      tenant_id: "tenant-a",
      snapshot_date: "2024-06-15",
      total_open_cents: 11500000,
      bucket_0_30_cents: 5000000,
      bucket_31_60_cents: 3000000,
      bucket_61_90_cents: 2000000,
      bucket_90_plus_cents: 1500000,
      account_count_with_balance: 45,
    },
    {
      snapshot_id: "snap_002",
      tenant_id: "tenant-a",
      snapshot_date: "2024-06-14",
      total_open_cents: 11000000,
      bucket_0_30_cents: 4800000,
      bucket_31_60_cents: 2900000,
      bucket_61_90_cents: 1900000,
      bucket_90_plus_cents: 1400000,
      account_count_with_balance: 43,
    },
  ];
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("ARAgingDashboard", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("renders aging summary and top accounts table", async () => {
    mockGetArAging.mockResolvedValue({
      data: agingSummaryFixture(),
      request_id: "r1",
    } as any);
    mockGetArAgingHistory.mockResolvedValue({
      data: agingHistoryFixture(),
      request_id: "r2",
    } as any);

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(screen.getByText("AR Aging Dashboard")).toBeInTheDocument();
    });

    // Toolbar summary: total outstanding and accounts with a balance (moved
    // out of the hub title row, where it clipped under nine Billing tabs).
    const summary = document.querySelector("[data-aging-summary]");
    expect(summary).toHaveTextContent("$115,000.00 outstanding · 2 accounts");
    expect(summary).toHaveAttribute(
      "title",
      "$115,000.00 outstanding · 2 accounts",
    );

    // Top accounts
    expect(screen.getByText("Acme Main")).toBeInTheDocument();
    expect(screen.getByText("Beta Energy")).toBeInTheDocument();
  });

  it("shows loading state initially", () => {
    mockGetArAging.mockReturnValue(new Promise(() => {}));
    mockGetArAgingHistory.mockReturnValue(new Promise(() => {}));

    render(<ARAgingDashboard />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("shows error state on fetch failure", async () => {
    mockGetArAging.mockRejectedValue(new Error("Service unavailable"));
    mockGetArAgingHistory.mockRejectedValue(new Error("Service unavailable"));

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/Service unavailable/)).toBeInTheDocument();
    });
  });

  it("shows the page header and a staff-access state on 403", async () => {
    const forbidden = new ApiError(
      "Caller lacks a required role for this operation",
      403,
      "INSUFFICIENT_ROLE",
    );
    mockGetArAging.mockRejectedValue(forbidden);
    mockGetArAgingHistory.mockRejectedValue(forbidden);

    render(<ARAgingDashboard />);

    expect(
      await screen.findByRole("heading", {
        name: "Runsheet staff access required",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "AR Aging Dashboard" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Back to Today/ }));
    expect(mockPush).toHaveBeenCalledWith("/dashboard");
    expect(screen.getByRole("link", { name: "Go to Billing" })).toHaveAttribute(
      "href",
      "/dashboard/billing",
    );
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("renders bucket chart with correct aria label", async () => {
    mockGetArAging.mockResolvedValue({
      data: agingSummaryFixture(),
      request_id: "r1",
    } as any);
    mockGetArAgingHistory.mockResolvedValue({
      data: agingHistoryFixture(),
      request_id: "r2",
    } as any);

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(
        screen.getByRole("img", { name: /Aging bucket distribution chart/i }),
      ).toBeInTheDocument();
    });
  });

  it("renders bucket legend with amounts", async () => {
    mockGetArAging.mockResolvedValue({
      data: agingSummaryFixture(),
      request_id: "r1",
    } as any);
    mockGetArAgingHistory.mockResolvedValue({
      data: agingHistoryFixture(),
      request_id: "r2",
    } as any);

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(screen.getAllByText(/1–30/).length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText(/31–60/).length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText(/61–90/).length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText(/90\+/).length).toBeGreaterThanOrEqual(1);
    });
  });

  // F12: aging is by days past due_date, with a Current (not yet due) bucket.
  it("shows the Current bucket first and labels buckets as days past due", async () => {
    mockGetArAging.mockResolvedValue({
      data: agingSummaryFixture({
        bucket_current_cents: 700000,
        total_open_cents: 12200000,
      }),
      request_id: "r1",
    } as any);
    mockGetArAgingHistory.mockResolvedValue({
      data: agingHistoryFixture(),
      request_id: "r2",
    } as any);
    render(<ARAgingDashboard />);
    expect(
      await screen.findByText("Current (not yet due)"),
    ).toBeInTheDocument();
    for (const label of [
      "1–30 days past due",
      "31–60 days past due",
      "61–90 days past due",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getAllByText("90+ days past due").length).toBeGreaterThan(0);
    expect(screen.getByText("$7,000.00")).toBeInTheDocument();
    expect(screen.queryByText(/0–30/)).not.toBeInTheDocument();
  });

  it("renders '—' for Current on history snapshots from before due-date aging", async () => {
    mockGetArAging.mockResolvedValue({
      data: agingSummaryFixture({ bucket_current_cents: 0 }),
      request_id: "r1",
    } as any);
    mockGetArAgingHistory.mockResolvedValue({
      data: [
        { ...agingHistoryFixture()[0], bucket_current_cents: null },
        {
          ...agingHistoryFixture()[0],
          snapshot_id: "snap_new",
          snapshot_date: "2026-10-09",
          bucket_current_cents: 250000,
        },
      ],
      request_id: "r2",
    } as any);
    render(<ARAgingDashboard />);
    // Phase 3: history lives in the History drawer.
    fireEvent.click(await screen.findByRole("button", { name: /History/ }));
    const table = screen.getByRole("table", { name: "Aging history" });
    const rows = Array.from(
      (table as HTMLTableElement).querySelectorAll("tbody tr"),
    );
    const cells = (r: Element) =>
      Array.from(r.querySelectorAll("td")).map((td) => td.textContent);
    // Column order: Date, Current, 1–30, ...
    expect(cells(rows[0])[1]).toBe("—");
    expect(cells(rows[1])[1]).toBe("$2,500.00");
  });

  it("calls onViewAccount when View button is clicked in top accounts", async () => {
    mockGetArAging.mockResolvedValue({
      data: agingSummaryFixture(),
      request_id: "r1",
    } as any);
    mockGetArAgingHistory.mockResolvedValue({
      data: agingHistoryFixture(),
      request_id: "r2",
    } as any);

    const onViewAccount = jest.fn();
    render(<ARAgingDashboard onViewAccount={onViewAccount} />);

    await waitFor(() => {
      expect(screen.getByText("Acme Main")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Acme Main"));

    expect(onViewAccount).toHaveBeenCalledWith("acc_001");
  });

  it("renders aging history table", async () => {
    mockGetArAging.mockResolvedValue({
      data: agingSummaryFixture(),
      request_id: "r1",
    } as any);
    mockGetArAgingHistory.mockResolvedValue({
      data: agingHistoryFixture(),
      request_id: "r2",
    } as any);

    render(<ARAgingDashboard />);

    fireEvent.click(await screen.findByRole("button", { name: "History" }));
    expect(
      await screen.findByRole("dialog", { name: "Aging History" }),
    ).toBeInTheDocument();

    // History dates (calendar days, never shifted)
    expect(screen.getByText("Fri 14 Jun 2024")).toBeInTheDocument();
  });

  it("shows empty state when no accounts have balance", async () => {
    const emptyAging = agingSummaryFixture({
      by_account: [],
      bucket_0_30_cents: 0,
      bucket_31_60_cents: 0,
      bucket_61_90_cents: 0,
      bucket_90_plus_cents: 0,
      total_open_cents: 0,
    });
    mockGetArAging.mockResolvedValue({
      data: emptyAging,
      request_id: "r1",
    } as any);
    mockGetArAgingHistory.mockResolvedValue({
      data: [],
      request_id: "r2",
    } as any);

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(
        screen.getByText("No accounts with outstanding balances."),
      ).toBeInTheDocument();
    });
  });
});
