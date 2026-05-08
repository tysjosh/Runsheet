/**
 * Unit tests for :func:`useFuelPlanningWebSocket`.
 *
 * The hook wraps the base :func:`useWebSocket` hook — rather than mocking
 * the internal hook (which would drift out of sync with the production
 * behavior), we stub the global ``WebSocket`` constructor so the hook
 * exercises its real message-dispatch path end to end. Each test
 * enqueues a synthetic envelope that matches the backend
 * :class:`FuelPlanningWSManager.broadcast_event` shape and asserts both
 * the ``last*`` state and the per-event callback fire exactly once.
 *
 * Validates: Requirements 1.6.4, 2.4.6, 2.5.4, 7.2.6, 8.5.4, 9.1.4,
 * 9.1.5.
 */

import { act, renderHook } from "@testing-library/react";

import {
  type CrossContaminationViolationEvent,
  type CustomerTankForecastReadyEvent,
  type EmergencyStopInsertedEvent,
  type ReplanDiffReadyEvent,
  type SourcingRecommendationReadyEvent,
  type StormModeActivatedEvent,
  type StormModeClearedEvent,
  useFuelPlanningWebSocket,
} from "./useFuelPlanningWebSocket";

// ─── WebSocket mock ──────────────────────────────────────────────────────────
//
// The base :func:`useWebSocket` hook constructs a ``new WebSocket(url)``
// and wires ``onopen`` / ``onmessage`` / ``onerror`` / ``onclose``
// handlers. This mock captures the constructed instance so tests can
// reach in and call those handlers directly without spinning up a real
// socket server.

interface MockWebSocketInstance {
  url: string;
  readyState: number;
  onopen: ((event?: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  send: jest.Mock;
  close: jest.Mock;
  /** Convenience: simulate a broadcast message from the server. */
  emit(data: unknown): void;
}

let lastMockSocket: MockWebSocketInstance | null = null;

class MockWebSocket implements Omit<MockWebSocketInstance, "emit"> {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

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
    lastMockSocket = {
      url: this.url,
      readyState: this.readyState,
      get onopen() {
        return self.onopen;
      },
      set onopen(v) {
        self.onopen = v;
      },
      get onmessage() {
        return self.onmessage;
      },
      set onmessage(v) {
        self.onmessage = v;
      },
      get onerror() {
        return self.onerror;
      },
      set onerror(v) {
        self.onerror = v;
      },
      get onclose() {
        return self.onclose;
      },
      set onclose(v) {
        self.onclose = v;
      },
      send: this.send,
      close: this.close,
      emit: (payload: unknown) => {
        this.onmessage?.({
          data: JSON.stringify(payload),
        } as MessageEvent);
      },
    };
    // Mirror how a real browser invokes onopen on the next tick — the
    // hook sets up its handler synchronously during render.
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    const self = this;
    setTimeout(() => self.onopen?.(new Event("open")), 0);
  }
}

beforeAll(() => {
  (global as unknown as { WebSocket: unknown }).WebSocket = MockWebSocket;
});

beforeEach(() => {
  lastMockSocket = null;
});

// ─── Helpers ─────────────────────────────────────────────────────────────────

function envelope<T>(type: string, data: T) {
  return { type, data, timestamp: "2025-01-01T00:00:00Z" };
}

async function flushOpen(): Promise<void> {
  // Let the ``setTimeout(onopen)`` fire.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("useFuelPlanningWebSocket", () => {
  it("connects to the /ws/fuel-planning URL derived from the API base", async () => {
    const { result } = renderHook(() => useFuelPlanningWebSocket());
    await flushOpen();

    expect(lastMockSocket).not.toBeNull();
    expect(lastMockSocket?.url).toMatch(/\/ws\/fuel-planning$/);
    expect(result.current.isConnected).toBe(true);
  });

  it("surfaces customer_tank_forecast_ready via state + callback", async () => {
    const onCustomerTankForecastReady = jest.fn();
    const payload: CustomerTankForecastReadyEvent = {
      run_id: "run-1",
      tenant_id: "tenant-a",
      customer_tank_id: "ct-1",
      fuel_type: "propane",
      runout_risk_24h: 0.42,
      model_name: "PropaneKFactorModel",
    };

    const { result } = renderHook(() =>
      useFuelPlanningWebSocket({ onCustomerTankForecastReady }),
    );
    await flushOpen();

    act(() => {
      lastMockSocket?.emit(envelope("customer_tank_forecast_ready", payload));
    });

    expect(onCustomerTankForecastReady).toHaveBeenCalledTimes(1);
    expect(onCustomerTankForecastReady).toHaveBeenCalledWith(payload);
    expect(result.current.lastCustomerTankForecastReady).toEqual(payload);
  });

  it("surfaces emergency_stop_inserted via state + callback", async () => {
    const onEmergencyStopInserted = jest.fn();
    const payload: EmergencyStopInsertedEvent = {
      run_id: "run-2",
      tenant_id: "tenant-a",
      route_id: "route-7",
      diff_summary: {
        diff_id: "diff-9",
        added: 1,
        removed: 0,
        reordered: 3,
        reassigned: 0,
        quantity_changes: 0,
        eta_shifts: 2,
      },
      risk_level: "high",
    };

    const { result } = renderHook(() =>
      useFuelPlanningWebSocket({ onEmergencyStopInserted }),
    );
    await flushOpen();

    act(() => {
      lastMockSocket?.emit(envelope("emergency_stop_inserted", payload));
    });

    expect(onEmergencyStopInserted).toHaveBeenCalledWith(payload);
    expect(result.current.lastEmergencyStopInserted).toEqual(payload);
  });

  it("surfaces replan_diff_ready with the diff_url link", async () => {
    const onReplanDiffReady = jest.fn();
    const payload: ReplanDiffReadyEvent = {
      event_id: "evt-1",
      diff_id: "diff-1",
      tenant_id: "tenant-a",
      summary: {
        added: 0,
        removed: 1,
        reordered: 2,
        reassigned: 0,
        quantity_changes: 1,
        eta_shifts: 4,
      },
      diff_url: "/api/fuel/mvp/replans/evt-1/diff",
      replan_type: "delay",
    };

    const { result } = renderHook(() =>
      useFuelPlanningWebSocket({ onReplanDiffReady }),
    );
    await flushOpen();

    act(() => {
      lastMockSocket?.emit(envelope("replan_diff_ready", payload));
    });

    expect(onReplanDiffReady).toHaveBeenCalledWith(payload);
    expect(result.current.lastReplanDiffReady?.diff_url).toBe(
      "/api/fuel/mvp/replans/evt-1/diff",
    );
  });

  it("surfaces cross_contamination_violation via state + callback", async () => {
    const onCrossContaminationViolation = jest.fn();
    const payload: CrossContaminationViolationEvent = {
      compartment_id: "comp-1",
      truck_id: "truck-9",
      previous_product: "HEATING_OIL",
      attempted_product: "GASOLINE_REG",
      governing_rule: "blocked",
      tenant_id: "tenant-a",
    };

    const { result } = renderHook(() =>
      useFuelPlanningWebSocket({ onCrossContaminationViolation }),
    );
    await flushOpen();

    act(() => {
      lastMockSocket?.emit(envelope("cross_contamination_violation", payload));
    });

    expect(onCrossContaminationViolation).toHaveBeenCalledWith(payload);
    expect(result.current.lastCrossContaminationViolation).toEqual(payload);
  });

  it("surfaces storm_mode_activated and storm_mode_cleared independently", async () => {
    const onStormModeActivated = jest.fn();
    const onStormModeCleared = jest.fn();
    const activated: StormModeActivatedEvent = {
      tenant_id: "tenant-a",
      activation_time: "2025-01-02T00:00:00Z",
      expected_end_at: "2025-01-03T00:00:00Z",
      trigger_alerts: [{ alert_id: "wx-1", severity: "severe" }],
    };
    const cleared: StormModeClearedEvent = {
      tenant_id: "tenant-a",
      cleared_at: "2025-01-03T06:00:00Z",
    };

    const { result } = renderHook(() =>
      useFuelPlanningWebSocket({
        onStormModeActivated,
        onStormModeCleared,
      }),
    );
    await flushOpen();

    act(() => {
      lastMockSocket?.emit(envelope("storm_mode_activated", activated));
    });
    act(() => {
      lastMockSocket?.emit(envelope("storm_mode_cleared", cleared));
    });

    expect(onStormModeActivated).toHaveBeenCalledWith(activated);
    expect(onStormModeCleared).toHaveBeenCalledWith(cleared);
    expect(result.current.lastStormModeActivated).toEqual(activated);
    expect(result.current.lastStormModeCleared).toEqual(cleared);
  });

  it("surfaces sourcing_recommendation_ready with the top-pick summary", async () => {
    const onSourcingRecommendationReady = jest.fn();
    const payload: SourcingRecommendationReadyEvent = {
      recommendation_id: "rec-1",
      request_id: "req-1",
      tenant_id: "tenant-a",
      product_code: "DIESEL_2",
      volume_gallons: 8000,
      candidate_count: 3,
      rack_price_fallback: false,
      wait_warning_terminal_ids: ["term-2"],
      top_terminal_id: "term-1",
      top_score: 0.87,
    };

    const { result } = renderHook(() =>
      useFuelPlanningWebSocket({ onSourcingRecommendationReady }),
    );
    await flushOpen();

    act(() => {
      lastMockSocket?.emit(envelope("sourcing_recommendation_ready", payload));
    });

    expect(onSourcingRecommendationReady).toHaveBeenCalledWith(payload);
    expect(
      result.current.lastSourcingRecommendationReady?.top_terminal_id,
    ).toBe("term-1");
  });

  it("ignores heartbeat and connection control envelopes", async () => {
    const onCustomerTankForecastReady = jest.fn();
    const { result } = renderHook(() =>
      useFuelPlanningWebSocket({ onCustomerTankForecastReady }),
    );
    await flushOpen();

    act(() => {
      lastMockSocket?.emit({
        type: "connection",
        message: "connected",
        timestamp: "2025-01-01T00:00:00Z",
      });
    });
    act(() => {
      lastMockSocket?.emit({
        type: "heartbeat",
        timestamp: "2025-01-01T00:00:05Z",
      });
    });

    expect(onCustomerTankForecastReady).not.toHaveBeenCalled();
    // Connection status is surfaced so banners can reflect it.
    expect(result.current.connectionStatus).toBe("connected");
    expect(result.current.lastCustomerTankForecastReady).toBeNull();
  });
});
