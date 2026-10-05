/**
 * Tests for :file:`ApprovalQueue.tsx` — loading-plan execution results
 * (design K13, Jest plan L1082).
 *
 * The REST helpers and the agent WebSocket hook are mocked; ``ApiError``
 * stays real so the component's ``instanceof`` check is exercised.
 *
 * Validates: Requirements 12.1-12.6.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

jest.mock("../../services/agentApi", () => ({
  __esModule: true,
  getApprovals: jest.fn(),
  approveAction: jest.fn(),
  rejectAction: jest.fn(),
}));

type ApprovalHandler = (event: { type: string; approval: unknown }) => void;
let wsHandler: ApprovalHandler | null = null;
jest.mock("../../hooks/useAgentWebSocket", () => ({
  __esModule: true,
  useAgentWebSocket: (opts: { onApprovalEvent?: ApprovalHandler }) => {
    wsHandler = opts.onApprovalEvent ?? null;
    return {};
  },
}));

import type { ApprovalEntry } from "../../services/agentApi";
import {
  approveAction,
  getApprovals,
  rejectAction,
} from "../../services/agentApi";
import { ApiError } from "../../services/api";
import ApprovalQueue from "./ApprovalQueue";

const mockGetApprovals = getApprovals as jest.MockedFunction<
  typeof getApprovals
>;
const mockApprove = approveAction as jest.MockedFunction<typeof approveAction>;
const mockReject = rejectAction as jest.MockedFunction<typeof rejectAction>;

// ─── Fixtures ────────────────────────────────────────────────────────────────

const NOW = Date.parse("2026-10-04T12:00:00Z");

function entry(overrides: Partial<ApprovalEntry> = {}): ApprovalEntry {
  return {
    action_id: "act-1",
    action_type: "mutation",
    tool_name: "apply_loading_plan",
    parameters: {},
    risk_level: "medium",
    proposed_by: "compartment_loading_agent",
    proposed_at: "2026-10-04T11:00:00Z",
    status: "pending",
    reviewed_by: null,
    reviewed_at: null,
    expiry_time: "2026-10-04T13:00:00Z",
    impact_summary: "Load 2 orders onto truck T-1",
    tenant_id: "tenant-a",
    ...overrides,
  };
}

function listOf(entries: ApprovalEntry[]) {
  return { entries, total: entries.length, page: 1, size: 50 };
}

function row(actionId: string): HTMLElement {
  return screen.getByTestId(`approval-row-${actionId}`);
}

async function renderQueue(entries: ApprovalEntry[]) {
  mockGetApprovals.mockResolvedValue(listOf(entries));
  render(<ApprovalQueue />);
  await waitFor(() => expect(mockGetApprovals).toHaveBeenCalled());
  await waitFor(() =>
    expect(screen.queryByText(/^Approval Queue$/)).toBeInTheDocument(),
  );
  // Wait for the spinner to go away.
  await waitFor(() =>
    expect(document.querySelector(".animate-spin.w-5")).toBeNull(),
  );
}

beforeEach(() => {
  mockGetApprovals.mockReset();
  mockApprove.mockReset();
  mockReject.mockReset();
  wsHandler = null;
});

afterEach(() => {
  jest.useRealTimers();
});

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("ApprovalQueue — loading-plan execution results", () => {
  it("loads with include_unresolved", async () => {
    await renderQueue([]);
    expect(mockGetApprovals).toHaveBeenCalledWith("default", 1, 50, true);
  });

  it("shows 'Nothing needs your review' when nothing is unresolved", async () => {
    await renderQueue([]);
    expect(screen.getByText("Nothing needs your review")).toBeInTheDocument();
  });

  it("renders incomplete/failed rows without the empty state", async () => {
    await renderQueue([
      entry({ action_id: "inc", status: "incomplete" }),
      entry({ action_id: "fail", status: "failed" }),
    ]);
    expect(screen.queryByText("No pending approvals")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Nothing needs your review"),
    ).not.toBeInTheDocument();
    expect(row("inc")).toBeInTheDocument();
    expect(row("fail")).toBeInTheDocument();
  });

  it("counts only pending rows in the badge", async () => {
    await renderQueue([
      entry({ action_id: "p1" }),
      entry({ action_id: "inc", status: "incomplete" }),
      entry({ action_id: "fail", status: "failed" }),
    ]);
    expect(screen.getByText("1 pending")).toBeInTheDocument();
  });

  it("removes the row when approval succeeds", async () => {
    mockApprove.mockResolvedValue({ status: "executed" });
    await renderQueue([entry()]);
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", {
          name: "Approve apply_loading_plan action",
        }),
      );
    });
    expect(mockApprove).toHaveBeenCalledWith("act-1");
    await waitFor(() =>
      expect(screen.queryByTestId("approval-row-act-1")).toBeNull(),
    );
  });

  it("shows the 409 message in an alert and keeps the row", async () => {
    mockApprove.mockRejectedValue(
      new ApiError(
        "Order o-1 changed since the plan was made",
        409,
        "LOADING_PLAN_EXECUTION_FAILED",
      ),
    );
    await renderQueue([entry()]);
    // The refetch after the 409 returns the now-failed entry.
    mockGetApprovals.mockResolvedValue(
      listOf([
        entry({
          status: "failed",
          execution_result: {
            outcome: "failed",
            message: "Order o-1 changed since the plan was made",
          },
        }),
      ]),
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", {
          name: "Approve apply_loading_plan action",
        }),
      );
    });
    await waitFor(() => expect(mockGetApprovals).toHaveBeenCalledTimes(2));
    expect(within(row("act-1")).getByRole("alert")).toHaveTextContent(
      "Order o-1 changed since the plan was made",
    );
  });

  it("shows a generic message for a non-409 approve error", async () => {
    const err = jest.spyOn(console, "error").mockImplementation(() => {});
    mockApprove.mockRejectedValue(new ApiError("boom", 500));
    await renderQueue([entry()]);
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", {
          name: "Approve apply_loading_plan action",
        }),
      );
    });
    expect(within(row("act-1")).getByRole("alert")).toHaveTextContent(
      "Could not approve this action. Try again.",
    );
    expect(err).toHaveBeenCalled();
    expect(mockGetApprovals).toHaveBeenCalledTimes(1);
    err.mockRestore();
  });

  it("incomplete with writes shows Retry and no Reject", async () => {
    await renderQueue([
      entry({
        status: "incomplete",
        execution_result: {
          outcome: "incomplete",
          writes_made: true,
          message: "1 of 2 orders applied",
        },
      }),
    ]);
    const r = row("act-1");
    expect(within(r).getByRole("button", { name: /Retry/ })).toBeEnabled();
    expect(within(r).queryByRole("button", { name: /Reject/ })).toBeNull();
    expect(within(r).getByRole("alert")).toHaveTextContent(
      "1 of 2 orders applied",
    );
  });

  it("incomplete whose latest outcome is in_progress with writes shows Retry, no Reject", async () => {
    await renderQueue([
      entry({
        status: "incomplete",
        execution_result: {
          outcome: "in_progress",
          writes_made: true,
          message: "Another attempt is running",
        },
      }),
    ]);
    const r = row("act-1");
    expect(
      within(r).getByRole("button", { name: /Retry/ }),
    ).toBeInTheDocument();
    expect(within(r).queryByRole("button", { name: /Reject/ })).toBeNull();
  });

  it("incomplete with writes_made false offers Reject", async () => {
    await renderQueue([
      entry({
        status: "incomplete",
        execution_result: { outcome: "incomplete", writes_made: false },
      }),
    ]);
    const r = row("act-1");
    expect(
      within(r).getByRole("button", { name: /Retry/ }),
    ).toBeInTheDocument();
    expect(
      within(r).getByRole("button", { name: /Reject/ }),
    ).toBeInTheDocument();
  });

  it("failed shows the reason and Dismiss, which rejects", async () => {
    mockReject.mockResolvedValue({ status: "rejected" });
    await renderQueue([
      entry({
        status: "failed",
        execution_result: {
          outcome: "failed",
          reason: "stale_order",
          message: "Order o-2 is no longer open",
        },
      }),
    ]);
    const r = row("act-1");
    expect(within(r).getByRole("alert")).toHaveTextContent(
      "Order o-2 is no longer open",
    );
    expect(within(r).queryByRole("button", { name: /Approve/ })).toBeNull();
    await act(async () => {
      fireEvent.click(
        within(r).getByRole("button", { name: "Dismiss failed loading plan" }),
      );
    });
    expect(mockReject).toHaveBeenCalledWith("act-1");
    await waitFor(() =>
      expect(screen.queryByTestId("approval-row-act-1")).toBeNull(),
    );
  });

  it("pending with a mode_unavailable result shows the message and keeps Approve and Reject", async () => {
    await renderQueue([
      entry({
        status: "pending",
        execution_result: {
          outcome: "failed",
          reason: "mode_unavailable",
          writes_made: false,
          message: "Loading plans can't be applied right now",
        },
      }),
    ]);
    const r = row("act-1");
    expect(within(r).getByRole("alert")).toHaveTextContent(
      "Loading plans can't be applied right now",
    );
    expect(within(r).getByRole("button", { name: /Approve/ })).toBeEnabled();
    expect(within(r).getByRole("button", { name: /Reject/ })).toBeEnabled();
  });

  it("an approved loading row moves from Applying… to Retry after the lease", async () => {
    jest.useFakeTimers({ now: NOW });
    mockApprove.mockResolvedValue({ status: "executed" });
    await renderQueue([
      entry({
        status: "approved",
        execution_result: {
          state: "in_progress",
          claimed_at: new Date(NOW - 30_000).toISOString(),
        },
      }),
    ]);
    let r = row("act-1");
    expect(r).toHaveAttribute("aria-busy", "true");
    expect(within(r).getByText("Applying…")).toBeInTheDocument();
    for (const b of within(r).getAllByRole("button")) {
      expect(b).toBeDisabled();
    }

    // 30 s + 91 s = 121 s since the claim; the 15 s interval re-renders.
    act(() => {
      jest.advanceTimersByTime(91_000);
    });
    r = row("act-1");
    expect(r).not.toHaveAttribute("aria-busy");
    expect(
      within(r).getByText(
        "This plan stopped while it was being applied. Retry to finish it.",
      ),
    ).toBeInTheDocument();
    expect(within(r).queryByRole("button", { name: /Reject/ })).toBeNull();
    const retry = within(r).getByRole("button", { name: /Retry/ });
    expect(retry).toBeEnabled();
    await act(async () => {
      fireEvent.click(retry);
    });
    expect(mockApprove).toHaveBeenCalledWith("act-1");
  });

  it("a legacy approved loading row (no claimed_at) offers Dismiss", async () => {
    mockReject.mockResolvedValue({ status: "rejected" });
    await renderQueue([entry({ status: "approved" })]);
    const r = row("act-1");
    expect(
      within(r).getByText(/Approved before plans could be applied/),
    ).toBeInTheDocument();
    expect(within(r).queryByRole("button", { name: /Retry/ })).toBeNull();
    await act(async () => {
      fireEvent.click(within(r).getByRole("button", { name: /Dismiss/ }));
    });
    expect(mockReject).toHaveBeenCalledWith("act-1");
  });

  it("disables the row's buttons while a request is in flight", async () => {
    let resolve: (v: Record<string, unknown>) => void = () => {};
    mockApprove.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    await renderQueue([entry()]);
    const r = row("act-1");
    act(() => {
      fireEvent.click(within(r).getByRole("button", { name: /Approve/ }));
    });
    for (const b of within(r).getAllByRole("button")) {
      expect(b).toBeDisabled();
    }
    await act(async () => {
      resolve({ status: "executed" });
    });
  });

  it("applies WS events whatever their tenant_id", async () => {
    await renderQueue([entry()]);
    expect(wsHandler).not.toBeNull();
    act(() => {
      wsHandler?.({
        type: "approval_created",
        approval: entry({ action_id: "act-2", tenant_id: "other-tenant" }),
      });
    });
    expect(row("act-2")).toBeInTheDocument();

    act(() => {
      wsHandler?.({
        type: "approval_execution_updated",
        approval: entry({
          status: "failed",
          tenant_id: "other-tenant",
          execution_result: { outcome: "failed", message: "Truck changed" },
        }),
      });
    });
    expect(within(row("act-1")).getByRole("alert")).toHaveTextContent(
      "Truck changed",
    );

    act(() => {
      wsHandler?.({
        type: "approval_execution_updated",
        approval: entry({ status: "executed" }),
      });
    });
    expect(screen.queryByTestId("approval-row-act-1")).toBeNull();
  });
});
