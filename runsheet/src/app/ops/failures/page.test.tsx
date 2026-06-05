/**
 * Regression test for the Failure Analytics page.
 *
 * The ops failure-metrics endpoint (``GET /ops/metrics/failures``) returns
 * each time bucket as ``{ timestamp, values }`` where ``values`` holds
 * ``total_failures`` plus one count per ``failure_reason`` — it does NOT
 * carry the ``count`` / ``breakdown`` fields the chart components and the
 * reason dropdown read. The page must derive those from ``values``;
 * otherwise the bar chart, trend chart, and reason filter all render empty
 * even when failures exist.
 *
 * These tests pin the real backend bucket shape and verify the derivation
 * surfaces failure reasons and counts.
 */

import { render, screen, within } from "@testing-library/react";

jest.mock("../../../services/opsApi", () => {
  const actual = jest.requireActual("../../../services/opsApi");
  return {
    ...actual,
    getFailureMetrics: jest.fn(),
    getShipmentFailures: jest.fn(),
    getShipmentById: jest.fn(),
  };
});

import {
  getFailureMetrics,
  getShipmentFailures,
  type MetricsResponse,
  type OpsShipment,
  type PaginatedResponse,
} from "../../../services/opsApi";
import OpsFailureAnalyticsPage from "./page";

const mockGetFailureMetrics = getFailureMetrics as jest.MockedFunction<
  typeof getFailureMetrics
>;
const mockGetShipmentFailures = getShipmentFailures as jest.MockedFunction<
  typeof getShipmentFailures
>;

/** Mirrors the live backend failures-metric payload (raw `values`). */
function metricsFixture(): MetricsResponse {
  return {
    data: [
      {
        timestamp: "2026-06-01T00:00:00Z",
        values: {
          total_failures: 5,
          customer_unavailable: 3,
          access_denied: 2,
        },
      },
      {
        timestamp: "2026-06-02T00:00:00Z",
        values: { total_failures: 1, weather: 1 },
      },
    ],
    bucket: "daily",
    start_date: "",
    end_date: "",
    request_id: "req-1",
  };
}

function failuresFixture(): PaginatedResponse<OpsShipment> {
  return {
    data: [
      {
        shipment_id: "SHP-1",
        status: "failed",
        tenant_id: "demo-tenant",
        rider_id: "rider-1",
        failure_reason: "customer_unavailable",
        updated_at: "2026-06-01T10:00:00Z",
      },
    ],
    pagination: { page: 1, size: 20, total: 1, total_pages: 1 },
    request_id: "req-2",
  };
}

afterEach(() => {
  jest.clearAllMocks();
});

describe("OpsFailureAnalyticsPage", () => {
  it("derives failure reasons from bucket `values` for the bar chart", async () => {
    mockGetFailureMetrics.mockResolvedValue(metricsFixture());
    mockGetShipmentFailures.mockResolvedValue(failuresFixture());

    render(<OpsFailureAnalyticsPage />);

    // Bar chart aggregates per-reason counts across buckets. Without the
    // values→breakdown derivation it would show "No failure data". Each
    // reason also appears as a dropdown <option>, so allow multiple matches.
    expect(await screen.findByText("Failures by Reason")).toBeInTheDocument();
    expect(screen.getAllByText("customer_unavailable").length).toBeGreaterThan(
      0,
    );
    expect(screen.getAllByText("access_denied").length).toBeGreaterThan(0);
    expect(screen.getAllByText("weather").length).toBeGreaterThan(0);
  });

  it("populates the failure-reason dropdown from the derived breakdown", async () => {
    mockGetFailureMetrics.mockResolvedValue(metricsFixture());
    mockGetShipmentFailures.mockResolvedValue(failuresFixture());

    render(<OpsFailureAnalyticsPage />);

    const dropdown = (await screen.findByLabelText(
      "Filter by failure type",
    )) as HTMLSelectElement;
    const optionValues = within(dropdown)
      .getAllByRole("option")
      .map((o) => (o as HTMLOptionElement).value);
    // "" (All) + the three reasons present across buckets.
    expect(optionValues).toEqual(
      expect.arrayContaining([
        "",
        "access_denied",
        "customer_unavailable",
        "weather",
      ]),
    );
  });

  it("renders the failed shipments table from the failures response", async () => {
    mockGetFailureMetrics.mockResolvedValue(metricsFixture());
    mockGetShipmentFailures.mockResolvedValue(failuresFixture());

    render(<OpsFailureAnalyticsPage />);

    expect(await screen.findByText("SHP-1")).toBeInTheDocument();
  });

  it("does not crash when metrics come back empty", async () => {
    mockGetFailureMetrics.mockResolvedValue({
      data: [],
      bucket: "daily",
      start_date: "",
      end_date: "",
      request_id: "req-3",
    });
    mockGetShipmentFailures.mockResolvedValue({
      data: [],
      pagination: { page: 1, size: 20, total: 0, total_pages: 1 },
      request_id: "req-4",
    });

    render(<OpsFailureAnalyticsPage />);

    expect(
      await screen.findByText(/no failure data for the selected time range/i),
    ).toBeInTheDocument();
  });
});
