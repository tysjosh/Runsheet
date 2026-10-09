/**
 * Scheduling metrics page.
 *
 * F4: the API's completion_rate is already a percentage (16.67 for 1 of 6),
 *     so the page must not multiply it by 100 (staging showed "1667.0%").
 * F11: bucket labels are UTC dates, not local `toLocaleString` (staging
 *      showed the 10-06 bucket as "10/5/2026, 8:00 PM").
 */
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";

jest.mock("../../services/schedulingApi", () => ({
  getJobMetrics: jest.fn(),
  getCompletionMetrics: jest.fn(),
  getAssetUtilization: jest.fn(),
  getDelayMetrics: jest.fn(),
}));

import {
  getAssetUtilization,
  getCompletionMetrics,
  getDelayMetrics,
  getJobMetrics,
} from "../../services/schedulingApi";
import SchedulingMetricsPage, {
  completionBarWidth,
  formatBucketLabel,
} from "./SchedulingMetricsPage";

beforeEach(() => {
  (getJobMetrics as jest.Mock).mockReset().mockResolvedValue({
    bucket: "daily",
    data: [
      {
        timestamp: "2026-10-06T00:00:00.000Z",
        total: 7,
        counts_by_status: { completed: 1 },
        counts_by_type: { fuel_delivery: 7 },
      },
    ],
  });
  (getCompletionMetrics as jest.Mock).mockReset().mockResolvedValue({
    data: [
      {
        job_type: "fuel_delivery",
        total: 6,
        completed: 1,
        failed: 0,
        completion_rate: 16.67,
        avg_completion_minutes: 55,
      },
    ],
  });
  (getAssetUtilization as jest.Mock)
    .mockReset()
    .mockResolvedValue({ data: [] });
  (getDelayMetrics as jest.Mock).mockReset().mockResolvedValue({
    data: { total_delayed: 0, avg_delay_minutes: 0, delays_by_job_type: [] },
  });
});

it("F4: shows the completion rate as returned (16.7%), never x100", async () => {
  render(<SchedulingMetricsPage />);
  expect(await screen.findByText("16.7%")).toBeInTheDocument();
  expect(screen.queryByText("1667.0%")).not.toBeInTheDocument();
});

it("F4: the bar width is the percentage, clamped", () => {
  expect(completionBarWidth(16.67)).toBe("16.67%");
  expect(completionBarWidth(140)).toBe("100%");
  expect(completionBarWidth(-3)).toBe("0%");
});

it("F11: a daily bucket renders as its UTC date", async () => {
  render(<SchedulingMetricsPage />);
  expect(await screen.findByText("2026-10-06")).toBeInTheDocument();
  expect(screen.queryByText(/10\/5\/2026/)).not.toBeInTheDocument();
  expect(screen.getByText("Dates are in UTC")).toBeInTheDocument();
});

describe("formatBucketLabel", () => {
  const originalTz = process.env.TZ;
  afterEach(() => {
    process.env.TZ = originalTz;
  });

  it("formats daily and hourly buckets in UTC", () => {
    expect(formatBucketLabel("2026-10-06T00:00:00.000Z", "daily")).toBe(
      "2026-10-06",
    );
    expect(formatBucketLabel("2026-10-06T13:00:00.000Z", "hourly")).toBe(
      "2026-10-06 13:00 UTC",
    );
  });

  it("does not shift the day in a US time zone", () => {
    process.env.TZ = "America/New_York";
    expect(formatBucketLabel("2026-10-06T00:00:00.000Z", "daily")).toBe(
      "2026-10-06",
    );
  });

  it("passes an unparseable value through", () => {
    expect(formatBucketLabel("garbage", "daily")).toBe("garbage");
  });
});
