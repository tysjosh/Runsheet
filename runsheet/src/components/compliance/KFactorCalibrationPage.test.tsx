/**
 * Regression test for :file:`KFactorCalibrationPage.tsx`.
 *
 * The K-factor dashboard endpoint (``GET /compliance/kfactor/dashboard``)
 * returns a **flat array of per-tank entries** under ``data`` (plus a
 * ``count``) — NOT a wrapped ``{ entries, total_review_needed,
 * total_insufficient_data }`` object. Each entry carries
 * ``current_kfactor`` / ``suggested_kfactor`` / ``variance_percent`` /
 * ``read_only`` (not the ``current_k_factor`` / ``status`` /
 * ``cumulative_variance_percent`` the page previously assumed).
 *
 * An earlier version read ``response.data.entries`` and the wrong field
 * names, so the dashboard silently rendered empty. These tests pin the
 * real backend shape: the page derives summary counts + a display status
 * client-side and renders one row per tank.
 */

import { render, screen, waitFor } from "@testing-library/react";

jest.mock("../../services/complianceApi", () => {
  const actual = jest.requireActual("../../services/complianceApi");
  return {
    ...actual,
    getKFactorDashboard: jest.fn(),
    approveKFactorAdjustment: jest.fn(),
  };
});

import {
  getKFactorDashboard,
  type KFactorEntry,
} from "../../services/complianceApi";
import KFactorCalibrationPage from "./KFactorCalibrationPage";

const mockGetDashboard = getKFactorDashboard as jest.MockedFunction<
  typeof getKFactorDashboard
>;

/** Mirrors the live backend payload (flat per-tank rows under `data`). */
function entries(): KFactorEntry[] {
  return [
    {
      // Over-threshold variance with a suggestion → "review_needed".
      tank_id: "TNK-001",
      customer_id: "CUST-1",
      current_kfactor: 0.12,
      suggested_kfactor: 0.15,
      variance_percent: 25,
      last_delivery_date: "2026-05-01",
      delivery_count: 5,
      read_only: false,
      read_only_reason: null,
    },
    {
      // Fewer than 3 deliveries → read_only → "insufficient_data".
      tank_id: "TNK-002",
      customer_id: "CUST-2",
      current_kfactor: 0.2,
      suggested_kfactor: null,
      variance_percent: null,
      last_delivery_date: null,
      delivery_count: 1,
      read_only: true,
      read_only_reason: "Insufficient data for recalibration",
    },
    {
      // Within threshold, enough deliveries → "ok".
      tank_id: "TNK-003",
      customer_id: "CUST-3",
      current_kfactor: 0.18,
      suggested_kfactor: null,
      variance_percent: 2,
      last_delivery_date: "2026-05-10",
      delivery_count: 8,
      read_only: false,
      read_only_reason: null,
    },
  ];
}

afterEach(() => {
  jest.clearAllMocks();
});

describe("KFactorCalibrationPage", () => {
  it("renders one row per tank from the flat data array", async () => {
    mockGetDashboard.mockResolvedValue({
      data: entries(),
      count: 3,
      request_id: "req-1",
    });

    render(<KFactorCalibrationPage />);

    expect(await screen.findByText("TNK-001")).toBeInTheDocument();
    expect(screen.getByText("TNK-002")).toBeInTheDocument();
    expect(screen.getByText("TNK-003")).toBeInTheDocument();
  });

  it("derives summary counts (review-needed / insufficient-data) client-side", async () => {
    mockGetDashboard.mockResolvedValue({
      data: entries(),
      count: 3,
      request_id: "req-2",
    });

    render(<KFactorCalibrationPage />);

    // Both summary cards render their labels; the derived counts (1 review
    // needed, 1 insufficient data) are computed client-side from the flat
    // entry list rather than read from a backend totals object. Note
    // "Review Needed" / "Insufficient Data" also appear as table status
    // badges, so allow multiple matches.
    expect(
      (await screen.findAllByText("Review Needed")).length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText("Insufficient Data").length).toBeGreaterThan(0);
    // Two summary cards each show a "1" count.
    expect(screen.getAllByText("1").length).toBeGreaterThanOrEqual(2);
  });

  it("only offers Approve for review-needed tanks with a suggestion", async () => {
    mockGetDashboard.mockResolvedValue({
      data: entries(),
      count: 3,
      request_id: "req-3",
    });

    render(<KFactorCalibrationPage />);

    await screen.findByText("TNK-001");
    // Exactly one Approve button (TNK-001 only).
    expect(screen.getAllByRole("button", { name: /approve/i })).toHaveLength(1);
  });

  it("does not crash on an empty dashboard payload", async () => {
    mockGetDashboard.mockResolvedValue({
      data: [],
      count: 0,
      request_id: "req-4",
    });

    render(<KFactorCalibrationPage />);

    await waitFor(() => expect(mockGetDashboard).toHaveBeenCalled());
    expect(
      await screen.findByText(/no k-factor calibration data available/i),
    ).toBeInTheDocument();
  });
});
