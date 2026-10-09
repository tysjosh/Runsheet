/**
 * Analytics Overview.
 *
 * OI-50: no time-range selector (the KPIs are one daily snapshot).
 * F2: a null metric value renders "Not available" instead of crashing.
 * F3: no fabricated widgets (Customer Satisfaction 4.2, Fleet gauge 92, the
 *     Previous/Current trend, trend badges, Export Report). The trend is the
 *     real daily series; "as of" shows the snapshot age; null data shows an
 *     empty state.
 * F13: a failed request shows an error state with Retry.
 */
import "@testing-library/jest-dom";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../services/api", () => ({
  apiService: {
    getAnalyticsMetrics: jest.fn(),
    getAnalyticsRoutePerformance: jest.fn(),
    getAnalyticsTimeSeries: jest.fn(),
  },
}));

import { apiService } from "../services/api";
import Analytics, { parseMetricValue } from "./Analytics";

const mockMetrics = apiService.getAnalyticsMetrics as jest.Mock;
const mockRoutes = apiService.getAnalyticsRoutePerformance as jest.Mock;
const mockSeries = apiService.getAnalyticsTimeSeries as jest.Mock;

const SNAPSHOT = {
  delivery_performance: { title: "Delivery Performance", value: "25.0%" },
  average_delay: { title: "Average Delay", value: "1.0 hrs" },
  fleet_utilization: { title: "Fleet Utilization", value: "66.7%" },
};

beforeEach(() => {
  mockMetrics.mockReset().mockResolvedValue({
    data: SNAPSHOT,
    as_of: "2026-10-08T19:41:00+00:00",
    success: true,
  });
  mockRoutes.mockReset().mockResolvedValue({ data: [] });
  mockSeries.mockReset().mockResolvedValue({
    data: [
      { timestamp: "2026-10-07T00:00:00.000Z", value: null },
      { timestamp: "2026-10-08T00:00:00.000Z", value: 25 },
    ],
  });
});

function expectNoFabricatedWidgets() {
  for (const text of [
    "Customer Satisfaction",
    "4.2",
    "92",
    "Export Report",
    "Previous",
  ]) {
    expect(screen.queryByText(text, { exact: false })).not.toBeInTheDocument();
  }
}

it("renders no time-range selector", async () => {
  render(<Analytics />);
  expect(await screen.findByText("Delivery Performance")).toBeInTheDocument();
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

it("renders the snapshot values with an as-of label and no fabricated widgets", async () => {
  render(<Analytics />);
  expect(await screen.findByText("25.0%")).toBeInTheDocument();
  expect(screen.getByText("1.0 hrs")).toBeInTheDocument();
  expect(screen.getAllByText("66.7%").length).toBeGreaterThan(0);
  const asOf = screen.getByText(/As of/);
  expect(within(asOf).getByText(/2026|Oct/)).toHaveAttribute(
    "dateTime",
    "2026-10-08T19:41:00+00:00",
  );
  expectNoFabricatedWidgets();
});

it("F2: null metric values render 'Not available' without throwing", async () => {
  mockMetrics.mockResolvedValue({
    data: {
      delivery_performance: { title: "Delivery Performance", value: null },
      average_delay: { title: "Average Delay", value: "0.0 hrs" },
      fleet_utilization: { title: "Fleet Utilization", value: null },
    },
    as_of: "2026-10-05T09:09:00+00:00",
  });
  render(<Analytics />);
  expect(await screen.findByText("Delivery Performance")).toBeInTheDocument();
  // KPI card + the Fleet Utilization gauge panel.
  expect(screen.getAllByText("Not available").length).toBeGreaterThanOrEqual(3);
  expectNoFabricatedWidgets();
});

it("F3: no snapshot shows the empty state and no gauges or trend", async () => {
  mockMetrics.mockResolvedValue({ data: null, as_of: null, success: true });
  render(<Analytics />);
  expect(
    await screen.findByText(/No analytics snapshot yet/),
  ).toBeInTheDocument();
  expect(screen.queryByText("Fleet Utilization")).not.toBeInTheDocument();
  expect(screen.queryByText(/last 30 days/)).not.toBeInTheDocument();
  expect(screen.queryByText(/As of/)).not.toBeInTheDocument();
  expect(mockSeries).not.toHaveBeenCalled();
  expectNoFabricatedWidgets();
});

it("requests the real time series for the selected metric", async () => {
  render(<Analytics />);
  await waitFor(() =>
    expect(mockSeries).toHaveBeenCalledWith("delivery_performance", "30d"),
  );
  expect(
    await screen.findByText("Delivery Performance, last 30 days"),
  ).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /Average Delay/ }));
  await waitFor(() =>
    expect(mockSeries).toHaveBeenCalledWith("average_delay", "30d"),
  );
  expect(
    await screen.findByText("Average Delay, last 30 days"),
  ).toBeInTheDocument();
});

it("shows 'Not enough history yet' when every day is empty", async () => {
  mockSeries.mockResolvedValue({
    data: [{ timestamp: "2026-10-08T00:00:00.000Z", value: null }],
  });
  render(<Analytics />);
  expect(await screen.findByText("Not enough history yet")).toBeInTheDocument();
});

it("F13: a failed request shows an alert and Retry re-requests", async () => {
  mockMetrics.mockRejectedValueOnce(new Error("HTTP error! status: 500"));
  jest.spyOn(console, "error").mockImplementation(() => {});
  render(<Analytics />);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Analytics could not be loaded.");
  expect(
    screen.queryByText(/No analytics snapshot yet/),
  ).not.toBeInTheDocument();

  fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
  expect(await screen.findByText("25.0%")).toBeInTheDocument();
  expect(mockMetrics).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  (console.error as jest.Mock).mockRestore();
});

it("hides 'Needs attention' with a single route", async () => {
  mockRoutes.mockResolvedValue({
    data: [{ name: "RUN-1 · Ada", performance: 50, orders_scored: 2 }],
  });
  render(<Analytics />);
  expect(await screen.findByText(/Best route/)).toBeInTheDocument();
  expect(screen.queryByText(/Needs attention/)).not.toBeInTheDocument();
});

it("weights the average route performance by orders scored", async () => {
  mockRoutes.mockResolvedValue({
    data: [
      { name: "RUN-1", performance: 100, orders_scored: 1 },
      { name: "RUN-2", performance: 0, orders_scored: 3 },
    ],
  });
  render(<Analytics />);
  expect(await screen.findByText(/Needs attention/)).toBeInTheDocument();
  const line = screen.getByText(/Average route performance/);
  expect(line).toHaveTextContent("25.0%"); // not 50.0%
});

describe("parseMetricValue", () => {
  it("is null-safe", () => {
    expect(parseMetricValue(null)).toBeNull();
    expect(parseMetricValue(undefined)).toBeNull();
    expect(parseMetricValue("80.0%")).toBe(80);
    expect(parseMetricValue("n/a")).toBeNull();
  });
});
