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

jest.mock("../../../services/commerceApi", () => ({
  getArAging: jest.fn(),
  getArAgingHistory: jest.fn(),
}));

import { getArAging, getArAgingHistory } from "../../../services/commerceApi";
import ARAgingDashboard from "../ARAgingDashboard";

const mockGetArAging = getArAging as jest.MockedFunction<typeof getArAging>;
const mockGetArAgingHistory = getArAgingHistory as jest.MockedFunction<typeof getArAgingHistory>;

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
    mockGetArAging.mockResolvedValue({ data: agingSummaryFixture(), request_id: "r1" } as any);
    mockGetArAgingHistory.mockResolvedValue({ data: agingHistoryFixture(), request_id: "r2" } as any);

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(screen.getByText("AR Aging Dashboard")).toBeInTheDocument();
    });

    // Summary stats - total outstanding
    expect(screen.getAllByText("$115,000.00").length).toBeGreaterThanOrEqual(1);
    // Accounts with balance count
    expect(screen.getByText("2")).toBeInTheDocument();

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

  it("renders bucket chart with correct aria label", async () => {
    mockGetArAging.mockResolvedValue({ data: agingSummaryFixture(), request_id: "r1" } as any);
    mockGetArAgingHistory.mockResolvedValue({ data: agingHistoryFixture(), request_id: "r2" } as any);

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(
        screen.getByRole("img", { name: /Aging bucket distribution chart/i }),
      ).toBeInTheDocument();
    });
  });

  it("renders bucket legend with amounts", async () => {
    mockGetArAging.mockResolvedValue({ data: agingSummaryFixture(), request_id: "r1" } as any);
    mockGetArAgingHistory.mockResolvedValue({ data: agingHistoryFixture(), request_id: "r2" } as any);

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(screen.getAllByText(/0–30/).length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText(/31–60/).length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText(/61–90/).length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText(/90\+/).length).toBeGreaterThanOrEqual(1);
    });
  });

  it("calls onViewAccount when View button is clicked in top accounts", async () => {
    mockGetArAging.mockResolvedValue({ data: agingSummaryFixture(), request_id: "r1" } as any);
    mockGetArAgingHistory.mockResolvedValue({ data: agingHistoryFixture(), request_id: "r2" } as any);

    const onViewAccount = jest.fn();
    render(<ARAgingDashboard onViewAccount={onViewAccount} />);

    await waitFor(() => {
      expect(screen.getByText("Acme Main")).toBeInTheDocument();
    });

    const viewButtons = screen.getAllByRole("button", { name: /View/i });
    fireEvent.click(viewButtons[0]);

    expect(onViewAccount).toHaveBeenCalledWith("acc_001");
  });

  it("renders aging history table", async () => {
    mockGetArAging.mockResolvedValue({ data: agingSummaryFixture(), request_id: "r1" } as any);
    mockGetArAgingHistory.mockResolvedValue({ data: agingHistoryFixture(), request_id: "r2" } as any);

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(screen.getByText("Aging History")).toBeInTheDocument();
    });

    // History dates
    expect(screen.getByText("2024-06-14")).toBeInTheDocument();
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
    mockGetArAging.mockResolvedValue({ data: emptyAging, request_id: "r1" } as any);
    mockGetArAgingHistory.mockResolvedValue({ data: [], request_id: "r2" } as any);

    render(<ARAgingDashboard />);

    await waitFor(() => {
      expect(screen.getByText("No accounts with outstanding balances.")).toBeInTheDocument();
    });
  });
});
