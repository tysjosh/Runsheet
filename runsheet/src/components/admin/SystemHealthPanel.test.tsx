/**
 * Settings → System health (UI revamp task 3.9): poison-queue metrics for
 * platform_admin, LoadErrorState with Retry on failure.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { ApiError } from "../../services/api";

jest.mock("../../services/opsApi", () => ({
  getPoisonQueueMonitoring: jest.fn(),
}));

import { getPoisonQueueMonitoring } from "../../services/opsApi";
import SystemHealthPanel, { getMetricStatus } from "./SystemHealthPanel";

const mockGet = getPoisonQueueMonitoring as jest.Mock;

afterEach(() => jest.clearAllMocks());

describe("SystemHealthPanel", () => {
  it("shows the poison-queue figures and a status badge", async () => {
    mockGet.mockResolvedValue({
      data: {
        queue_depth: 1200,
        oldest_event_age_seconds: 12_000,
        pending_count: 4,
        permanently_failed_count: 2,
      },
      request_id: "r",
    });
    render(<SystemHealthPanel />);
    expect(await screen.findByText("1,200")).toBeInTheDocument();
    expect(screen.getByText("3 h 20 min")).toBeInTheDocument();
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.queryByText(/ingestion|indexing/i)).toBeNull();
  });

  it("shows LoadErrorState with Retry and recovers on retry", async () => {
    mockGet.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    render(<SystemHealthPanel />);
    const retry = await screen.findByRole("button", { name: /try again/i });
    expect(screen.queryByText("Failed to fetch")).toBeNull();
    mockGet.mockResolvedValueOnce({ data: { queue_depth: 0 } });
    await act(async () => {
      fireEvent.click(retry);
    });
    expect(await screen.findByText("Healthy")).toBeInTheDocument();
  });

  it("shows the 403 access copy", async () => {
    mockGet.mockRejectedValue(new ApiError("Forbidden", 403));
    render(<SystemHealthPanel />);
    expect(
      await screen.findByText("Runsheet staff access required"),
    ).toBeInTheDocument();
  });
});

describe("getMetricStatus", () => {
  it("grades above and below thresholds", () => {
    const c = { direction: "above" as const, warning: 50, critical: 100 };
    expect(getMetricStatus(10, c)).toBe("healthy");
    expect(getMetricStatus(60, c)).toBe("degraded");
    expect(getMetricStatus(200, c)).toBe("critical");
    const b = { direction: "below" as const, warning: 90, critical: 50 };
    expect(getMetricStatus(40, b)).toBe("critical");
  });
});
