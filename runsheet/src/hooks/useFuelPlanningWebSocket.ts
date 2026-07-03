/**
 * Specialized WebSocket hook for fuel-planning real-time updates.
 *
 * Connects to the backend ``/ws/fuel-planning`` channel managed by
 * :class:`fuel.services.fuel_planning_ws_manager.FuelPlanningWSManager`
 * and exposes typed state + per-event callbacks for the fuel-ops
 * hardening planning events:
 *
 *   * ``customer_tank_forecast_ready`` — a new Customer_Tank forecast
 *     completed (Task 3.6, Req 1.6.4).
 *   * ``emergency_stop_inserted`` — the Route_Planning_Agent inserted
 *     an emergency stop into an active route (Task 4.9, Req 2.4.6).
 *   * ``replan_diff_ready`` — the Exception_Replanning_Agent persisted
 *     a Replan_Diff for any replan (Task 4.10, Req 2.5.4).
 *   * ``cross_contamination_violation`` — the Compartment_Loading_Agent
 *     rejected a compartment assignment (Task 6.5, Req 7.2.6).
 *   * ``storm_mode_activated`` / ``storm_mode_cleared`` — the
 *     Storm_Mode_Evaluator transitioned state (Task 10.3, Req 9.1.4,
 *     9.1.5).
 *   * ``sourcing_recommendation_ready`` — the Sourcing_Recommender
 *     persisted a ranked list (Task 7.10, Req 8.5.4).
 *
 * Mirrors the pattern established by
 * :func:`useSchedulingWebSocket` and :func:`useInventoryWebSocket`:
 * wraps the base ``useWebSocket`` hook, tracks the last-received
 * event of each type, and invokes optional per-event callbacks. The
 * envelope shape (``{type, data, timestamp}``) matches what the
 * base :class:`BaseWSManager.broadcast` emits for every manager on
 * the platform, so the dispatcher UI can reuse the same parser.
 *
 * Validates: Requirements 1.6.4, 2.4.6, 2.5.4, 7.2.6, 8.5.4, 9.1.4,
 * 9.1.5.
 */

import { useCallback, useMemo, useState } from "react";
import { getAuthToken } from "../utils/auth";
import {
  useWebSocket,
  type WebSocketOptions,
  type WebSocketState,
} from "./useWebSocket";

// Derive WebSocket URL from the API base URL (same convention as the
// other specialized hooks). The ``/api`` suffix is stripped and the
// protocol swapped to ``ws`` / ``wss``.
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";
const WS_BASE = API_BASE_URL.replace(/\/api$/, "").replace("http", "ws");
const FUEL_PLANNING_WS_URL = `${WS_BASE}/ws/fuel-planning`;

/**
 * Build WebSocket URL with JWT token for authentication
 */
async function buildFuelPlanningWebSocketUrl(): Promise<string> {
  const token = await getAuthToken();
  return token
    ? `${FUEL_PLANNING_WS_URL}?token=${encodeURIComponent(token)}`
    : FUEL_PLANNING_WS_URL;
}

// ─── Event Types ─────────────────────────────────────────────────────────────

/**
 * Event types the ``/ws/fuel-planning`` channel can deliver. Mirrors
 * the ``EVENT`` constants on
 * :class:`FuelPlanningWSManager` plus the Capability 7 / 9 signals
 * that are broadcast through the same channel so dispatcher UIs have
 * a single subscription surface.
 */
export type FuelPlanningEventType =
  | "customer_tank_forecast_ready"
  | "emergency_stop_inserted"
  | "replan_diff_ready"
  | "cross_contamination_violation"
  | "storm_mode_activated"
  | "storm_mode_cleared"
  | "sourcing_recommendation_ready";

/**
 * Base envelope shape the backend broadcasts. Mirrors
 * :meth:`FuelPlanningWSManager.broadcast_event`.
 */
export interface FuelPlanningWebSocketMessage {
  type: FuelPlanningEventType | "connection" | "heartbeat";
  timestamp?: string;
  data?: unknown;
  status?: string;
  message?: string;
}

/**
 * Payload for ``customer_tank_forecast_ready``. The first six fields
 * are mandatory per :meth:`FuelPlanningWSManager.broadcast_customer_tank_forecast_ready`;
 * any additional keys supplied in the backend's ``extra`` mapping
 * surface as extra properties on this object.
 */
export interface CustomerTankForecastReadyEvent {
  run_id: string;
  tenant_id: string;
  customer_tank_id: string;
  fuel_type: string;
  runout_risk_24h: number;
  model_name: string;
  [key: string]: unknown;
}

/**
 * Compact diff summary carried inside ``emergency_stop_inserted`` and
 * ``replan_diff_ready``. Mirrors
 * :meth:`Agents.support.replan_diff_models.ReplanDiff.summary_counts`.
 */
export interface ReplanDiffSummary {
  added?: number;
  removed?: number;
  reordered?: number;
  reassigned?: number;
  quantity_changes?: number;
  eta_shifts?: number;
  diff_id?: string;
  original_route_id?: string;
  patched_route_id?: string;
  [key: string]: unknown;
}

/** Payload for ``emergency_stop_inserted``. */
export interface EmergencyStopInsertedEvent {
  run_id: string;
  tenant_id: string;
  route_id: string;
  diff_summary: ReplanDiffSummary;
  /** Optional risk level annotation emitted by the backend (``medium`` | ``high``). */
  risk_level?: "medium" | "high";
  approval_id?: string | null;
  insert_index?: number;
  [key: string]: unknown;
}

/** Payload for ``replan_diff_ready``. */
export interface ReplanDiffReadyEvent {
  event_id: string;
  diff_id: string;
  tenant_id: string;
  summary: ReplanDiffSummary;
  /** Ready-to-fetch URL to ``GET /api/fuel/mvp/replans/{event_id}/diff``. */
  diff_url: string;
  replan_type?: string;
  original_route_id?: string;
  patched_route_id?: string;
  [key: string]: unknown;
}

/**
 * Payload for ``cross_contamination_violation``. Mirrors the compact
 * event envelope described in the fuel-ops-hardening design doc
 * (``{compartment_id, truck_id, previous_product, attempted_product,
 * governing_rule}``). Additional fields (e.g. ``actor_id``,
 * ``tenant_id``) may be supplied by the backend and surface as extra
 * properties.
 */
export interface CrossContaminationViolationEvent {
  compartment_id: string;
  truck_id: string;
  previous_product?: string | null;
  attempted_product: string;
  governing_rule: "allowed" | "blocked" | "requires_cleaning";
  tenant_id?: string;
  [key: string]: unknown;
}

/** Payload for ``storm_mode_activated``. */
export interface StormModeActivatedEvent {
  tenant_id: string;
  activation_time: string;
  expected_end_at?: string | null;
  /** Triggering WeatherAlert summaries; the UI renders banner copy off this list. */
  trigger_alerts: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

/** Payload for ``storm_mode_cleared``. */
export interface StormModeClearedEvent {
  tenant_id: string;
  cleared_at: string;
  [key: string]: unknown;
}

/** Payload for ``sourcing_recommendation_ready``. */
export interface SourcingRecommendationReadyEvent {
  recommendation_id: string;
  request_id: string;
  tenant_id: string;
  product_code: string;
  volume_gallons: number;
  candidate_count: number;
  rack_price_fallback: boolean;
  wait_warning_terminal_ids: string[];
  top_terminal_id?: string;
  top_score?: number;
  truck_id?: string;
  run_id?: string;
  [key: string]: unknown;
}

// ─── Hook Options ────────────────────────────────────────────────────────────

/**
 * Configuration options for the fuel-planning WebSocket hook. Every
 * callback is optional — consumers can subscribe only to the events
 * they care about.
 */
export interface FuelPlanningWebSocketOptions {
  /** Whether to automatically connect on mount. Defaults to ``true``. */
  autoConnect?: boolean;
  /** Invoked on every ``customer_tank_forecast_ready`` event. */
  onCustomerTankForecastReady?: (event: CustomerTankForecastReadyEvent) => void;
  /** Invoked on every ``emergency_stop_inserted`` event. */
  onEmergencyStopInserted?: (event: EmergencyStopInsertedEvent) => void;
  /** Invoked on every ``replan_diff_ready`` event. */
  onReplanDiffReady?: (event: ReplanDiffReadyEvent) => void;
  /** Invoked on every ``cross_contamination_violation`` event. */
  onCrossContaminationViolation?: (
    event: CrossContaminationViolationEvent,
  ) => void;
  /** Invoked on every ``storm_mode_activated`` event. */
  onStormModeActivated?: (event: StormModeActivatedEvent) => void;
  /** Invoked on every ``storm_mode_cleared`` event. */
  onStormModeCleared?: (event: StormModeClearedEvent) => void;
  /** Invoked on every ``sourcing_recommendation_ready`` event. */
  onSourcingRecommendationReady?: (
    event: SourcingRecommendationReadyEvent,
  ) => void;
  /** Invoked whenever the underlying connection state changes. */
  onConnectionStatusChange?: (state: WebSocketState) => void;
  /** Invoked when a reconnection attempt is scheduled. */
  onReconnecting?: (attempt: number, delay: number) => void;
  /** Invoked when reconnection gives up (only fires when a finite cap is set). */
  onMaxReconnectAttemptsReached?: () => void;
}

/**
 * Return type for the fuel-planning WebSocket hook. Stores the
 * last-received payload of each event type for consumers that prefer
 * a pull model over callbacks.
 */
export interface UseFuelPlanningWebSocketReturn {
  state: WebSocketState;
  isConnected: boolean;
  reconnectAttempt: number;
  reconnectDelay: number;
  lastCustomerTankForecastReady: CustomerTankForecastReadyEvent | null;
  lastEmergencyStopInserted: EmergencyStopInsertedEvent | null;
  lastReplanDiffReady: ReplanDiffReadyEvent | null;
  lastCrossContaminationViolation: CrossContaminationViolationEvent | null;
  lastStormModeActivated: StormModeActivatedEvent | null;
  lastStormModeCleared: StormModeClearedEvent | null;
  lastSourcingRecommendationReady: SourcingRecommendationReadyEvent | null;
  error: Event | null;
  connect: () => void;
  disconnect: () => void;
  send: (data: unknown) => boolean;
  connectionStatus: string | null;
}

// ─── Hook Implementation ─────────────────────────────────────────────────────

/**
 * Custom hook for fuel-planning real-time updates via WebSocket.
 *
 * Connects to ``/ws/fuel-planning`` and exposes typed per-event state
 * plus optional callbacks. Uses exponential backoff (1s initial, 30s
 * max, unlimited attempts) for auto-reconnection — matches the
 * scheduling and inventory hooks so operators see consistent
 * reconnect behavior across dashboards.
 *
 * @example
 * ```tsx
 * const {
 *   isConnected,
 *   lastEmergencyStopInserted,
 *   lastReplanDiffReady,
 * } = useFuelPlanningWebSocket({
 *   onEmergencyStopInserted: (event) => {
 *     toast.warning(`Route ${event.route_id} patched with emergency stop`);
 *   },
 *   onReplanDiffReady: (event) => {
 *     queryClient.invalidateQueries({ queryKey: ['replan-diff', event.event_id] });
 *   },
 * });
 * ```
 */
export function useFuelPlanningWebSocket(
  options: FuelPlanningWebSocketOptions = {},
): UseFuelPlanningWebSocketReturn {
  const [lastCustomerTankForecastReady, setLastCustomerTankForecastReady] =
    useState<CustomerTankForecastReadyEvent | null>(null);
  const [lastEmergencyStopInserted, setLastEmergencyStopInserted] =
    useState<EmergencyStopInsertedEvent | null>(null);
  const [lastReplanDiffReady, setLastReplanDiffReady] =
    useState<ReplanDiffReadyEvent | null>(null);
  const [lastCrossContaminationViolation, setLastCrossContaminationViolation] =
    useState<CrossContaminationViolationEvent | null>(null);
  const [lastStormModeActivated, setLastStormModeActivated] =
    useState<StormModeActivatedEvent | null>(null);
  const [lastStormModeCleared, setLastStormModeCleared] =
    useState<StormModeClearedEvent | null>(null);
  const [lastSourcingRecommendationReady, setLastSourcingRecommendationReady] =
    useState<SourcingRecommendationReadyEvent | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);

  const handleMessage = useCallback(
    (data: unknown) => {
      const message = data as FuelPlanningWebSocketMessage;

      switch (message.type) {
        case "connection":
          setConnectionStatus(message.message || message.status || "connected");
          break;

        case "heartbeat":
          // Connection-liveness ping — no state to update.
          break;

        case "customer_tank_forecast_ready": {
          const event = message.data as CustomerTankForecastReadyEvent;
          setLastCustomerTankForecastReady(event);
          options.onCustomerTankForecastReady?.(event);
          break;
        }

        case "emergency_stop_inserted": {
          const event = message.data as EmergencyStopInsertedEvent;
          setLastEmergencyStopInserted(event);
          options.onEmergencyStopInserted?.(event);
          break;
        }

        case "replan_diff_ready": {
          const event = message.data as ReplanDiffReadyEvent;
          setLastReplanDiffReady(event);
          options.onReplanDiffReady?.(event);
          break;
        }

        case "cross_contamination_violation": {
          const event = message.data as CrossContaminationViolationEvent;
          setLastCrossContaminationViolation(event);
          options.onCrossContaminationViolation?.(event);
          break;
        }

        case "storm_mode_activated": {
          const event = message.data as StormModeActivatedEvent;
          setLastStormModeActivated(event);
          options.onStormModeActivated?.(event);
          break;
        }

        case "storm_mode_cleared": {
          const event = message.data as StormModeClearedEvent;
          setLastStormModeCleared(event);
          options.onStormModeCleared?.(event);
          break;
        }

        case "sourcing_recommendation_ready": {
          const event = message.data as SourcingRecommendationReadyEvent;
          setLastSourcingRecommendationReady(event);
          options.onSourcingRecommendationReady?.(event);
          break;
        }

        default:
          console.warn(
            "Unknown fuel-planning WebSocket message type:",
            message.type,
          );
      }
    },
    [options],
  );

  const handleConnect = useCallback(() => {
    options.onConnectionStatusChange?.("connected");
  }, [options]);

  const handleDisconnect = useCallback(() => {
    options.onConnectionStatusChange?.("disconnected");
    setConnectionStatus(null);
  }, [options]);

  const wsOptions: WebSocketOptions = useMemo(
    () => ({
      autoConnect: options.autoConnect ?? true,
      initialReconnectDelay: 1000, // 1s — matches scheduling/inventory
      maxReconnectDelay: 30000, // 30s cap
      maxReconnectAttempts: 0, // Infinite
      backoffMultiplier: 2,
      getUrl: buildFuelPlanningWebSocketUrl, // Refresh token on each connection
      onConnect: handleConnect,
      onDisconnect: handleDisconnect,
      onMessage: handleMessage,
      onReconnecting: options.onReconnecting,
      onMaxReconnectAttemptsReached: options.onMaxReconnectAttemptsReached,
    }),
    [handleConnect, handleDisconnect, handleMessage, options],
  );

  const {
    state,
    isConnected,
    reconnectAttempt,
    reconnectDelay,
    error,
    connect,
    disconnect,
    send,
  } = useWebSocket("", wsOptions);

  return {
    state,
    isConnected,
    reconnectAttempt,
    reconnectDelay,
    lastCustomerTankForecastReady,
    lastEmergencyStopInserted,
    lastReplanDiffReady,
    lastCrossContaminationViolation,
    lastStormModeActivated,
    lastStormModeCleared,
    lastSourcingRecommendationReady,
    error,
    connect,
    disconnect,
    send,
    connectionStatus,
  };
}

export default useFuelPlanningWebSocket;
