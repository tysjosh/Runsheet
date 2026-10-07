/**
 * Tests for the G1 "Driver activity" section on the job detail page.
 *
 * Backed by ``GET /scheduling/jobs/{id}/driver-activity``, which merges the
 * job's driver messages and exceptions newest first. Covers the loaded list,
 * the empty state, the error text from a 403 envelope, the type filter and
 * pagination.
 */
import { act, fireEvent, render, screen, within } from "@testing-library/react";

jest.mock("../../services/schedulingApi", () => ({
  getJobDriverActivity: jest.fn(),
}));

import { ApiError } from "../../services/api";
import { extractApiErrorMessage } from "../../services/apiErrors";
import {
  type DriverActivityItem,
  getJobDriverActivity,
} from "../../services/schedulingApi";
import DriverActivitySection from "./DriverActivitySection";

const mockGetActivity = getJobDriverActivity as jest.MockedFunction<
  typeof getJobDriverActivity
>;

const message: DriverActivityItem = {
  id: "MSG-1",
  type: "message",
  timestamp: "2026-10-05T10:15:00Z",
  driver_id: "DRV-3",
  sender_role: "driver",
  job_id: "JOB-1",
  order_id: null,
  text: "Gate is locked, waiting for site contact",
};

const exception: DriverActivityItem = {
  id: "EXC-1",
  type: "exception",
  timestamp: "2026-10-05T09:00:00Z",
  driver_id: "DRV-3",
  job_id: "JOB-1",
  order_id: "ORD-9",
  text: "Tank fill pipe damaged",
  exception_type: "site_access",
  severity: "high",
};

function page(
  items: DriverActivityItem[],
  { page: p = 1, total = items.length, totalPages = 1 } = {},
) {
  return {
    data: items,
    pagination: { page: p, size: 20, total, total_pages: totalPages },
    request_id: "req-1",
  };
}

beforeEach(() => {
  mockGetActivity.mockReset();
});

describe("DriverActivitySection", () => {
  it("renders a labelled section with the job's messages and exceptions", async () => {
    mockGetActivity.mockResolvedValue(page([message, exception]));
    render(<DriverActivitySection jobId="JOB-1" />);

    const region = screen.getByRole("region", { name: "Driver activity" });
    expect(
      within(region).getByRole("heading", {
        level: 2,
        name: "Driver activity",
      }),
    ).toBeInTheDocument();

    expect(
      await within(region).findByText(
        "Gate is locked, waiting for site contact",
      ),
    ).toBeInTheDocument();
    const table = within(region).getByRole("table");
    const rows = within(table).getAllByRole("row");
    // header + 2 rows
    expect(rows).toHaveLength(3);
    expect(within(rows[1]).getByText("Message")).toBeInTheDocument();
    expect(within(rows[1]).getByText("DRV-3")).toBeInTheDocument();
    expect(within(rows[2]).getByText(/Exception/)).toBeInTheDocument();
    expect(
      within(rows[2]).getByText("Tank fill pipe damaged"),
    ).toBeInTheDocument();
    expect(mockGetActivity).toHaveBeenCalledWith("JOB-1", {
      page: 1,
      size: 20,
    });
  });

  it("shows a loading status while the request is in flight", () => {
    mockGetActivity.mockReturnValue(new Promise(() => {}));
    render(<DriverActivitySection jobId="JOB-1" />);
    expect(screen.getByRole("status")).toHaveTextContent(
      /Loading driver activity/,
    );
  });

  it("shows an empty state when the job has no driver activity", async () => {
    mockGetActivity.mockResolvedValue(page([]));
    render(<DriverActivitySection jobId="JOB-1" />);
    expect(
      await screen.findByText("No driver messages or exceptions for this job"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows the message from a 403 error envelope", async () => {
    const envelope = {
      error_code: "INSUFFICIENT_ROLE",
      message: "This action requires one of the roles: admin, dispatcher",
      request_id: "req-403",
    };
    mockGetActivity.mockRejectedValue(
      new ApiError(
        extractApiErrorMessage(envelope, "HTTP error! status: 403"),
        403,
        "INSUFFICIENT_ROLE",
      ),
    );
    render(<DriverActivitySection jobId="JOB-1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This action requires one of the roles: admin, dispatcher",
    );
  });

  it("refetches with type=exception when the type filter changes", async () => {
    mockGetActivity.mockResolvedValue(page([message, exception]));
    render(<DriverActivitySection jobId="JOB-1" />);
    await screen.findByText("Gate is locked, waiting for site contact");

    mockGetActivity.mockResolvedValue(page([exception]));
    fireEvent.change(screen.getByLabelText("Activity type"), {
      target: { value: "exception" },
    });

    expect(await screen.findByText("Tank fill pipe damaged")).toBeVisible();
    expect(mockGetActivity).toHaveBeenLastCalledWith("JOB-1", {
      page: 1,
      size: 20,
      type: "exception",
    });
  });

  it("pages through results with Next and Previous", async () => {
    mockGetActivity.mockResolvedValue(
      page([message], { total: 21, totalPages: 2 }),
    );
    render(<DriverActivitySection jobId="JOB-1" />);
    await screen.findByText("Gate is locked, waiting for site contact");
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Previous page" }),
    ).toBeDisabled();

    mockGetActivity.mockResolvedValue(
      page([exception], { page: 2, total: 21, totalPages: 2 }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));

    expect(await screen.findByText("Tank fill pipe damaged")).toBeVisible();
    expect(mockGetActivity).toHaveBeenLastCalledWith("JOB-1", {
      page: 2,
      size: 20,
    });
    expect(screen.getByText("Page 2 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });

  it("ignores a slow earlier response that resolves after a newer one (OI-29)", async () => {
    mockGetActivity.mockResolvedValueOnce(page([message, exception]));
    render(<DriverActivitySection jobId="JOB-1" />);
    await screen.findByText("Gate is locked, waiting for site contact");

    // The first filter change hangs until we release it; the second resolves now.
    let releaseSlow: (v: ReturnType<typeof page>) => void = () => {};
    mockGetActivity.mockReturnValueOnce(
      new Promise((resolve) => {
        releaseSlow = resolve;
      }),
    );
    mockGetActivity.mockResolvedValueOnce(page([exception]));
    const filter = screen.getByLabelText("Activity type");
    fireEvent.change(filter, { target: { value: "message" } });
    fireEvent.change(filter, { target: { value: "exception" } });

    expect(await screen.findByText("Tank fill pipe damaged")).toBeVisible();

    await act(async () => {
      releaseSlow(page([message]));
    });
    expect(screen.getByText("Tank fill pipe damaged")).toBeVisible();
    expect(
      screen.queryByText("Gate is locked, waiting for site contact"),
    ).not.toBeInTheDocument();
  });

  it("caps paging at the backend's 1000-row window (OI-29)", async () => {
    mockGetActivity.mockResolvedValue(
      page([message], { page: 1, total: 1600, totalPages: 80 }),
    );
    render(<DriverActivitySection jobId="JOB-1" />);
    await screen.findByText("Gate is locked, waiting for site contact");
    expect(screen.getByText("Page 1 of 50")).toBeInTheDocument();

    const next = screen.getByRole("button", { name: "Next page" });
    for (let i = 2; i <= 50; i++) {
      fireEvent.click(next);
      expect(await screen.findByText(`Page ${i} of 50`)).toBeInTheDocument();
    }
    expect(mockGetActivity).toHaveBeenLastCalledWith("JOB-1", {
      page: 50,
      size: 20,
    });
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });
});
