/**
 * Ops Monitoring after UI revamp task 0.2: only the poison-queue card, and a
 * failed load shows on the card instead of blanking the page.
 */
import { render, screen } from "@testing-library/react";

jest.mock("../../services/opsApi", () => ({
  getPoisonQueueMonitoring: jest.fn(),
}));

import { getPoisonQueueMonitoring } from "../../services/opsApi";
import OpsMonitoringDashboard, {
  getMetricStatus,
} from "./OpsMonitoringDashboard";

const poisonMock = getPoisonQueueMonitoring as jest.MockedFunction<
  typeof getPoisonQueueMonitoring
>;

afterEach(() => {
  jest.clearAllMocks();
});

describe("OpsMonitoringDashboard", () => {
  it("renders only the poison-queue card", async () => {
    poisonMock.mockResolvedValue({
      data: {
        queue_depth: 3,
        oldest_event_age_seconds: 12,
        status_breakdown: {},
      },
      request_id: "r",
    } as never);
    render(<OpsMonitoringDashboard />);

    expect(await screen.findByText("Queue Depth")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.queryByText("Ingestion Pipeline")).not.toBeInTheDocument();
    expect(screen.queryByText("Indexing Health")).not.toBeInTheDocument();
  });

  it("shows a network failure on the card in plain words", async () => {
    poisonMock.mockRejectedValue(new Error("Failed to fetch"));
    render(<OpsMonitoringDashboard />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Couldn't load poison queue metrics. Retrying in 30 seconds.",
    );
    expect(screen.getByText("Poison Queue")).toBeInTheDocument();
  });

  it("shows the server's message for other failures", async () => {
    poisonMock.mockRejectedValue(new Error("Caller lacks a required role"));
    render(<OpsMonitoringDashboard />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Caller lacks a required role",
    );
  });
});

describe("getMetricStatus", () => {
  it("grades above and below thresholds", () => {
    const above = { direction: "above" as const, warning: 50, critical: 100 };
    expect(getMetricStatus(10, above)).toBe("healthy");
    expect(getMetricStatus(60, above)).toBe("degraded");
    expect(getMetricStatus(101, above)).toBe("critical");
    const below = {
      direction: "below" as const,
      warning: 0.99,
      critical: 0.95,
    };
    expect(getMetricStatus(1, below)).toBe("healthy");
    expect(getMetricStatus(0.97, below)).toBe("degraded");
    expect(getMetricStatus(0.9, below)).toBe("critical");
  });
});
