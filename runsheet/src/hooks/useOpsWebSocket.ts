/**
 * Specialized WebSocket hook for ops real-time updates.
 *
 * Wraps the base useWebSocket hook with ops-specific message handling,
 * subscription filters, and typed state for shipment, rider, and SLA breach events.
 *
 * Validates:
 * - Requirement 16.5: WHEN the WebSocket connection drops, THE Ops_Dashboard
 *   SHALL automatically reconnect with exponential backoff starting at 1 second
 *   with a maximum interval of 30 seconds
 */

import { useCallback, useMemo, useState } from "react";
import type { OpsRider, OpsShipment } from "../services/opsApi";
import {
  useWebSocket,
  type WebSocketOptions,
  type WebSocketState,
} from "./useWebSocket";

// Derive WebSocket URL from API base URL
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
const WS_BASE = API_BASE_URL.replace(/\/api$/, "").replace("http", "ws");
const OPS_WS_URL = `${WS_BASE}/ws/ops`;

/**
 * Event types the ops WebSocket can deliver
 */
export type OpsEventType = "shipment_update" | "rider_update" | "sla_breach" | "fuel_alert";

/**
 * Base message structure from the ops WebSocket endpoint
 */
export interface OpsWebSocketMessage {
  type: OpsEventType | "connection" | "heartbeat";
  timestamp: string;
  data?: unknown;
  status?: string;
  message?: string;
}

/**
 * Data payload for an SLA breach event
 */
export interface SlaBreach {
  shipment_id: string;
  tenant_id: string;
  estimated_delivery: string;
  breach_duration_minutes: number;
  rider_id?: string;
  status: string;
}

/**
 * Data payload for a fuel alert event
 */
export interface FuelAlertEvent {
  station_id: string;
  name: string;
  fuel_type: string;
  status: string;
  current_stock_liters: number;
  capacity_liters: number;
  stock_percentage: number;
  days_until_empty: number;
}

/**
 * Options for the ops WebSocket hook
 */
export interface OpsWebSocketOptions {
  /** Event types to subscribe to. Defaults to all types. */
  subscriptions?: OpsEventType[];
  /** Whether to automatically connect on mount */
  autoConnect?: boolean;
  /** Callback when a shipment update is received */
  onShipmentUpdate?: (shipment: OpsShipment) => void;
  /** Callback when a rider update is received */
  onRiderUpdate?: (rider: OpsRider) => void;
  /** Callback when an SLA breach event is received */
  onSlaBreach?: (breach: SlaBreach) => void;
  /** Callback when a fuel alert event is received */
  onFuelAlert?: (alert: FuelAlertEvent) => void;
  /** Callback when connection state changes */
  onConnectionStatusChange?: (state: WebSocketState) => void;
  /** Callback when reconnection starts */
  onReconnecting?: (attempt: number, delay: number) => void;
  /** Callback when max reconnection attempts reached */
  onMaxReconnectAttemptsReached?: () => void;
}

/**
 * Return type for the useOpsWebSocket hook
 */
export interface UseOpsWebSocketReturn {
  /** Current connection state */
  state: WebSocketState;
  /** Whether the WebSocket is currently connected */
  isConnected: boolean;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt: number;
  /** Time until next reconnection attempt (ms, 0 if not reconnecting) */
  reconnectDelay: number;
  /** Last received shipment update */
  lastShipmentUpdate: OpsShipment | null;
  /** Last received rider update */
  lastRiderUpdate: OpsRider | null;
  /** Last received SLA breach event */
  lastSlaBreach: SlaBreach | null;
  /** Last received fuel alert event */
  lastFuelAlert: FuelAlertEvent | null;
  /** Error if any occurred */
  error: Event | null;
  /** Manually connect to the WebSocket */
  connect: () => void;
  /** Manually disconnect from the WebSocket */
  disconnect: () => void;
  /** Send a message through the WebSocket (e.g. to update subscriptions) */
  send: (data: unknown) => boolean;
  /** Connection status message from server */
  connectionStatus: string | null;
}

/**
 * Custom hook for ops real-time updates via WebSocket.
 *
 * Connects to `/ws/ops` with subscription filters and provides typed state
 * for shipment updates, rider updates, and SLA breach events. Uses exponential
 * backoff (1s initial, 30s max) for auto-reconnection.
 *
 * @param options - Configuration options
 * @returns Ops WebSocket state and control functions
 *
 * @example
 * ```tsx
 * const { isConnected, lastShipmentUpdate, lastSlaBreach } = useOpsWebSocket({
 *   subscriptions: ['shipment_update', 'sla_breach'],
 *   onShipmentUpdate: (shipment) => {
 *     console.log(`Shipment ${shipment.shipment_id} updated to ${shipment.status}`);
 *   },
 *   onSlaBreach: (breach) => {
 *     console.log(`SLA breach on shipment ${breach.shipment_id}`);
 *   },
 * });
 * ```
 */
export function useOpsWebSocket(
  options: OpsWebSocketOptions = {},
): UseOpsWebSocketReturn {
  const [lastShipmentUpdate, setLastShipmentUpdate] =
    useState<OpsShipment | null>(null);
  const [lastRiderUpdate, setLastRiderUpdate] = useState<OpsRider | null>(null);
  const [lastSlaBreach, setLastSlaBreach] = useState<SlaBreach | null>(null);
  const [lastFuelAlert, setLastFuelAlert] = useState<FuelAlertEvent | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);

  const subscriptions = options.subscriptions ?? [
    "shipment_update",
    "rider_update",
    "sla_breach",
  ];

  /**
   * Handle incoming WebSocket messages and dispatch by event type
   */
  const handleMessage = useCallback(
    (data: unknown) => {
      const message = data as OpsWebSocketMessage;

      switch (message.type) {
        case "connection":
          setConnectionStatus(message.message || message.status || "connected");
          break;

        case "heartbeat":
          // Heartbeat received — connection is alive, nothing to do
          break;

        case "shipment_update": {
          const shipment = message.data as OpsShipment;
          setLastShipmentUpdate(shipment);
          options.onShipmentUpdate?.(shipment);
          break;
        }

        case "rider_update": {
          const rider = message.data as OpsRider;
          setLastRiderUpdate(rider);
          options.onRiderUpdate?.(rider);
          break;
        }

        case "sla_breach": {
          const breach = message.data as SlaBreach;
          setLastSlaBreach(breach);
          options.onSlaBreach?.(breach);
          break;
        }

        case "fuel_alert": {
          const alert = message.data as FuelAlertEvent;
          setLastFuelAlert(alert);
          options.onFuelAlert?.(alert);
          break;
        }

        default:
          console.warn("Unknown ops WebSocket message type:", message.type);
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

  // Build the WebSocket URL with subscription query params
  const wsUrl = useMemo(() => {
    if (subscriptions.length === 0) {
      return OPS_WS_URL;
    }
    const params = new URLSearchParams();
    subscriptions.forEach((sub) => {
      params.append("subscribe", sub);
    });
    return `${OPS_WS_URL}?${params.toString()}`;
  }, [subscriptions]);

  // Configure the base WebSocket hook
  const wsOptions: WebSocketOptions = useMemo(
    () => ({
      autoConnect: options.autoConnect ?? true,
      initialReconnectDelay: 1000, // 1 second
      maxReconnectDelay: 30000, // 30 seconds
      maxReconnectAttempts: 0, // Infinite attempts
      backoffMultiplier: 2,
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
  } = useWebSocket(wsUrl, wsOptions);

  return {
    state,
    isConnected,
    reconnectAttempt,
    reconnectDelay,
    lastShipmentUpdate,
    lastRiderUpdate,
    lastSlaBreach,
    lastFuelAlert,
    error,
    connect,
    disconnect,
    send,
    connectionStatus,
  };
}

export default useOpsWebSocket;
