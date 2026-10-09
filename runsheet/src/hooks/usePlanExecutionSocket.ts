/**
 * Specialized WebSocket hook for plan execution real-time updates.
 *
 * This hook wraps the base useWebSocket hook with plan-execution-specific
 * message handling and types. It connects to the plan execution WebSocket
 * endpoint and exposes execution updates as React state.
 *
 * Validates: Requirements 3.1, 3.2
 * - Real-time execution tracking via WebSocket
 * - Auto-reconnect with exponential backoff on disconnect
 */

import { useCallback, useMemo, useState } from "react";
import { getAuthToken } from "../utils/auth";
import {
  useWebSocket,
  type WebSocketOptions,
  type WebSocketState,
} from "./useWebSocket";

// API base URL for WebSocket
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080/api";
// The /api strip is ANCHORED (/\/api$/), matching every other WebSocket hook.
// It used to be an unanchored .replace("/api", ""), which was correct only while
// the backend host did not itself start with "api". Against the deployed origin
// https://api.runsheetops.com/api the unanchored form matched the "/api" inside
// "//api.runsheetops.com" and produced wss:/.runsheetops.com/api — an unparseable
// host, so plan-execution sockets failed while every other socket worked.
const WS_BASE_URL = API_BASE_URL.replace(/\/api$/, "").replace("http", "ws");
// Exported so websocketUrlDerivation.test.ts asserts against the REAL derivation
// rather than a copy of the expression, which would pass even if this line
// regressed.
export const PLAN_EXECUTION_WS_BASE_URL = `${WS_BASE_URL}/ws/plan-execution`;

/**
 * Build WebSocket URL with JWT token for authentication
 */
async function buildPlanExecutionWebSocketUrl(): Promise<string> {
  const token = await getAuthToken();
  return token
    ? `${PLAN_EXECUTION_WS_BASE_URL}?token=${encodeURIComponent(token)}`
    : PLAN_EXECUTION_WS_BASE_URL;
}

/**
 * Stop data within an execution update
 */
export interface ExecutionStopUpdate {
  station_id: string;
  sequence: number;
  status: string;
}

/**
 * Execution update data payload
 */
export interface ExecutionUpdateData {
  plan_id: string;
  route_id: string;
  stop: ExecutionStopUpdate;
  completed_stops: number;
  total_stops: number;
  updated_at: string;
}

/**
 * Message types from the plan execution WebSocket
 */
export type PlanExecutionMessageType =
  | "connection"
  | "execution_update"
  | "heartbeat";

/**
 * Base message structure from the plan execution WebSocket
 */
export interface PlanExecutionMessage {
  type: PlanExecutionMessageType;
  data?: ExecutionUpdateData;
  timestamp?: string;
  status?: string;
  message?: string;
}

/**
 * Options for the plan execution WebSocket hook
 */
export interface PlanExecutionSocketOptions {
  /** Whether to automatically connect on mount */
  autoConnect?: boolean;
  /** Callback when an execution update is received */
  onExecutionUpdate?: (update: ExecutionUpdateData) => void;
  /** Callback when connection status changes */
  onConnectionStatusChange?: (state: WebSocketState) => void;
  /** Callback when reconnection starts */
  onReconnecting?: (attempt: number, delay: number) => void;
  /** Callback when max reconnection attempts reached */
  onMaxReconnectAttemptsReached?: () => void;
}

/**
 * Return type for the usePlanExecutionSocket hook
 */
export interface UsePlanExecutionSocketReturn {
  /** Current connection state */
  state: WebSocketState;
  /** Whether the WebSocket is currently connected */
  isConnected: boolean;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt: number;
  /** Time until next reconnection attempt (ms, 0 if not reconnecting) */
  reconnectDelay: number;
  /** Last received execution update */
  lastUpdate: ExecutionUpdateData | null;
  /** Error if any occurred */
  error: Event | null;
  /** Manually connect to the WebSocket */
  connect: () => void;
  /** Manually disconnect from the WebSocket */
  disconnect: () => void;
  /** Connection status message from server */
  connectionStatus: string | null;
}

/**
 * Custom hook for plan execution real-time updates via WebSocket.
 *
 * Connects to `/ws/plan-execution?token=...` and provides automatic
 * reconnection with exponential backoff. Parses incoming execution_update
 * messages and exposes them as React state.
 *
 * @param options - Configuration options
 * @returns Plan execution WebSocket state and control functions
 *
 * @example
 * ```tsx
 * const { state, isConnected, lastUpdate } = usePlanExecutionSocket({
 *   onExecutionUpdate: (update) => {
 *     console.log(`Plan ${update.plan_id}: ${update.completed_stops}/${update.total_stops} stops done`);
 *   },
 * });
 * ```
 */
export function usePlanExecutionSocket(
  options: PlanExecutionSocketOptions = {},
): UsePlanExecutionSocketReturn {
  const [lastUpdate, setLastUpdate] = useState<ExecutionUpdateData | null>(
    null,
  );
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);

  /**
   * Handle incoming WebSocket messages
   */
  const handleMessage = useCallback(
    (data: unknown) => {
      const message = data as PlanExecutionMessage;

      switch (message.type) {
        case "connection": {
          setConnectionStatus(message.message || message.status || "connected");
          break;
        }

        case "execution_update": {
          if (message.data) {
            setLastUpdate(message.data);
            options.onExecutionUpdate?.(message.data);
          }
          break;
        }

        case "heartbeat":
          // Heartbeat received - connection is alive
          break;

        default:
          console.warn("Unknown plan execution message type:", message.type);
      }
    },
    [options],
  );

  /**
   * Handle connection state changes
   */
  const handleConnect = useCallback(() => {
    options.onConnectionStatusChange?.("connected");
  }, [options]);

  const handleDisconnect = useCallback(() => {
    options.onConnectionStatusChange?.("disconnected");
    setConnectionStatus(null);
  }, [options]);

  // WebSocket options with exponential backoff configuration
  const wsOptions: WebSocketOptions = useMemo(
    () => ({
      autoConnect: options.autoConnect ?? true,
      initialReconnectDelay: 1000, // Start with 1 second
      maxReconnectDelay: 30000, // Max 30 seconds
      maxReconnectAttempts: 0, // Infinite attempts
      backoffMultiplier: 2, // Double each time
      getUrl: buildPlanExecutionWebSocketUrl, // Refresh token on each connection
      onConnect: handleConnect,
      onDisconnect: handleDisconnect,
      onMessage: handleMessage,
      onReconnecting: options.onReconnecting,
      onMaxReconnectAttemptsReached: options.onMaxReconnectAttemptsReached,
    }),
    [handleConnect, handleDisconnect, handleMessage, options],
  );

  // Use the base WebSocket hook
  const {
    state,
    isConnected,
    reconnectAttempt,
    reconnectDelay,
    error,
    connect,
    disconnect,
  } = useWebSocket("", wsOptions);

  return {
    state,
    isConnected,
    reconnectAttempt,
    reconnectDelay,
    lastUpdate,
    error,
    connect,
    disconnect,
    connectionStatus,
  };
}

export default usePlanExecutionSocket;
