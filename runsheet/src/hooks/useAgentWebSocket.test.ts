/**
 * Unit tests for :func:`useAgentWebSocket`.
 *
 * Like ``useFuelPlanningWebSocket.test.ts``, this stubs the global
 * ``WebSocket`` constructor so the hook runs its real dispatch path.
 *
 * Validates: Requirement 12.6 (design K13) — ``approval_execution_updated``
 * is forwarded through ``onApprovalEvent``.
 */
import { act, renderHook } from "@testing-library/react";
import type { ApprovalEntry } from "../services/agentApi";
import { useAgentWebSocket } from "./useAgentWebSocket";

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static last: MockWebSocket | null = null;
  url: string;
  readyState: number = MockWebSocket.OPEN;
  onopen: ((event?: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  send = jest.fn();
  close = jest.fn();
  constructor(url: string) {
    this.url = url;
    MockWebSocket.last = this;
    setTimeout(() => this.onopen?.(new Event("open")), 0);
  }
  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

beforeAll(() => {
  (global as unknown as { WebSocket: unknown }).WebSocket = MockWebSocket;
});

beforeEach(() => {
  MockWebSocket.last = null;
});

async function flushOpen(): Promise<void> {
  // getUrl awaits the auth token before constructing the socket, so drain a
  // few macrotasks (same reasoning as useFuelPlanningWebSocket.test.ts).
  for (let i = 0; i < 5; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

const approval = {
  action_id: "act-1",
  tool_name: "apply_loading_plan",
  status: "failed",
  tenant_id: "tenant-a",
  execution_result: { outcome: "failed", message: "Order o-1 changed" },
} as unknown as ApprovalEntry;

describe("useAgentWebSocket", () => {
  it("forwards approval_execution_updated through onApprovalEvent", async () => {
    const onApprovalEvent = jest.fn();
    const warn = jest.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useAgentWebSocket({ onApprovalEvent }));
    await flushOpen();
    expect(MockWebSocket.last).not.toBeNull();

    act(() => {
      MockWebSocket.last?.emit({
        type: "approval_execution_updated",
        data: approval,
        timestamp: "2026-10-04T00:00:00Z",
      });
    });

    expect(onApprovalEvent).toHaveBeenCalledTimes(1);
    expect(onApprovalEvent).toHaveBeenCalledWith({
      type: "approval_execution_updated",
      approval,
    });
    expect(result.current.lastApprovalEvent).toEqual({
      type: "approval_execution_updated",
      approval,
    });
    expect(warn).not.toHaveBeenCalledWith(
      "Unknown agent WebSocket message type:",
      "approval_execution_updated",
    );
    warn.mockRestore();
  });
});
