/**
 * OI-50: the Analytics page offers no time-range selector.
 *
 * The backend ignores `timeRange` (B6), so a "Last 24 Hours / 7 / 30 / 90
 * Days" picker relabelled the same numbers. The selector and its badge are
 * gone and the metrics request carries no range.
 */
import "@testing-library/jest-dom";
import { render, screen, waitFor } from "@testing-library/react";

jest.mock("../services/api", () => ({
  apiService: {
    getAnalyticsMetrics: jest.fn(),
    getAnalyticsRoutePerformance: jest.fn(),
  },
}));

import { apiService } from "../services/api";
import Analytics from "./Analytics";

const mockMetrics = apiService.getAnalyticsMetrics as jest.Mock;
const mockRoutes = apiService.getAnalyticsRoutePerformance as jest.Mock;

beforeEach(() => {
  mockMetrics.mockReset().mockResolvedValue({
    data: {
      delivery_performance: {
        title: "On-time deliveries",
        value: "92%",
        change: "+2%",
        trend: "up",
      },
    },
  });
  mockRoutes.mockReset().mockResolvedValue({ data: [] });
});

it("renders no time-range selector", async () => {
  render(<Analytics />);
  expect(await screen.findByText("On-time deliveries")).toBeInTheDocument();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  for (const label of [
    "Last 24 Hours",
    "Last 7 Days",
    "Last 30 Days",
    "Last 90 Days",
  ]) {
    expect(screen.queryByText(label)).not.toBeInTheDocument();
  }
  expect(screen.queryByText("7d")).not.toBeInTheDocument();
});

it("requests metrics once, with no time range", async () => {
  render(<Analytics />);
  await waitFor(() => expect(mockMetrics).toHaveBeenCalledTimes(1));
  expect(mockMetrics).toHaveBeenCalledWith();
});
