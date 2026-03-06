/**
 * Specialized WebSocket hook for scheduling real-time updates.
 *
 * Wraps the base useWebSocket hook with scheduling-specific message handling,
 * subscription filters, and typed state for job, cargo, and delay events.
 *
 * Validates:
 * - Requirement 9.5: WHEN the WebSocket connection drops, THE Scheduling_Dashboard
 *   SHALL automatically reconnect with exponential backoff starting at 1 second
 *   with a maximum interval of 30 seconds
 */

import { useCallback, useMemo, useState } from "react";
import type { Job, SchedulingCargoItem } from "../types/api";
import {
  useWebSocket,
  type WebSocketOptions,
  type WebSocketState,
} from "./useWebSocket";

// Derive WebSocket URL from API base URL
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
const WS_BASE = API_BASE_URL.replace(/\/api$/, "").replace("http", "ws");
const SCHEDULING_WS_URL = `${WS_BASE}/ws/scheduling`;

/**
 * Event types the scheduling WebSocket can deliver
 */
export type SchedulingEventType =
  | "job_created"
  | "status_changed"
  | "delay_alert"
  | "cargo_update";

/**
 * Base message structure from the scheduling WebSocket endpoint
 */
export interface SchedulingWebSocketMessage {
  type: SchedulingEventType | "connection" | "heartbeat";
  timestamp?: string;
  data?: unknown;
  status?: string;
  message?: string;
}

/**
 * Data payload for a job_created event
 */
export interface JobCreatedEvent {
  job: Job;
}

/**
 * Data payload for a status_changed event
 */
export interface StatusChangedEvent {
  job_id: string;
  job_type: string;
  old_status: string;
  new_status: string;
  asset_assigned?: string;
  origin: string;
  destination: string;
  scheduled_time: string;
  estimated_arrival?: string;
}

/**
 * Data payload for a delay_alert event
 */
export interface DelayAlertEvent {
  job_id: string;
  job_type: string;
  asset_assigned?: string;
  origin: string;
  destination: string;
  delay_duration_minutes: number;
  estimated_arrival: string;
}

/**
 * Data payload for a cargo_update event
 */
export interface CargoUpdateEvent {
  job_id: string;
  item_id: string;
  old_status?: string;
  new_status: string;
  item?: SchedulingCargoItem;
  all_delivered?: boolean;
}

/**
 * Options for the scheduling WebSocket hook
 */
export interface SchedulingWebSocketOptions {
  /** Event types to subscribe to. Defaults to all types. */
  subscriptions?: SchedulingEventType[];
  /** Whether to automatically connect on mount */
  autoConnect?: boolean;
  /** Callback when a job_created event is received */
  onJobCreated?: (event: JobCreatedEvent) => void;
  /** Callback when a status_changed event is received */
  onStatusChanged?: (event: StatusChangedEvent) => void;
  /** Callback when a delay_alert event is received */
  onDelayAlert?: (event: DelayAlertEvent) => void;
  /** Callback when a cargo_update event is received */
  onCargoUpdate?: (event: CargoUpdateEvent) => void;
  /** Callback when connection state changes */
  onConnectionStatusChange?: (state: WebSocketState) => void;
  /** Callback when reconnection starts */
  onReconnecting?: (attempt: number, delay: number) => void;
  /** Callback when max reconnection attempts reached */
  onMaxReconnectAttemptsReached?: () => void;
}

/**
 * Return type for the useSchedulingWebSocket hook
 */
export interface UseSchedulingWebSocketReturn {
  /** Current connection state */
  state: WebSocketState;
  /** Whether the WebSocket is currently connected */
  isConnected: boolean;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt: number;
  /** Time until next reconnection attempt (ms, 0 if not reconnecting) */
  reconnectDelay: number;
  /** Last received job_created event */
  lastJobCreated: JobCreatedEvent | null;
  /** Last received status_changed event */
  lastStatusChanged: StatusChangedEvent | null;
  /** Last received delay_alert event */
  lastDelayAlert: DelayAlertEvent | null;
  /** Last received cargo_update event */
  lastCargoUpdate: CargoUpdateEvent | null;
  /** Error if any occurred */
  error: Event | null;
  /** Manually connect to the WebSocket */
  connect: () => void;
  /** Manually disconnect from the WebSocket */
  disconnect: () => void;
  /** Send a message through the WebSocket */
  send: (data: unknown) => boolean;
  /** Connection status message from server */
  connectionStatus: string | null;
}

/**
 * Custom hook for scheduling real-time updates via WebSocket.
 *
 * Connects to `/ws/scheduling` with subscription filters and provides typed
 * state for job creation, status changes, delay alerts, and cargo updates.
 * Uses exponential backoff (1s initial, 30s max) for auto-reconnection.
 *
 * @param options - Configuration options
 * @returns Scheduling WebSocket state and control functions
 *
 * @example
 * ```tsx
 * const { isConnected, lastStatusChanged, lastDelayAlert } = useSchedulingWebSocket({
 *   subscriptions: ['status_changed', 'delay_alert'],
 *   onStatusChanged: (event) => {
 *     console.log(`Job ${event.job_id} changed from ${event.old_status} to ${event.new_status}`);
 *   },
 *   onDelayAlert: (event) => {
 *     console.log(`Job ${event.job_id} delayed by ${event.delay_duration_minutes} minutes`);
 *   },
 * });
 * ```
 */
export function useSchedulingWebSocket(
  options: SchedulingWebSocketOptions = {},
): UseSchedulingWebSocketReturn {
  const [lastJobCreated, setLastJobCreated] =
    useState<JobCreatedEvent | null>(null);
  const [lastStatusChanged, setLastStatusChanged] =
    useState<StatusChangedEvent | null>(null);
  const [lastDelayAlert, setLastDelayAlert] =
    useState<DelayAlertEvent | null>(null);
  const [lastCargoUpdate, setLastCargoUpdate] =
    useState<CargoUpdateEvent | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);

  const subscriptions = options.subscriptions ?? [
    "job_created",
    "status_changed",
    "delay_alert",
    "cargo_update",
  ];

  /**
   * Handle incoming WebSocket messages and dispatch by event type
   */
  const handleMessage = useCallback(
    (data: unknown) => {
      const message = data as SchedulingWebSocketMessage;

      switch (message.type) {
        case "connection":
          setConnectionStatus(message.message || message.status || "connected");
          break;

        case "heartbeat":
          // Heartbeat received — connection is alive, nothing to do
          break;

        case "job_created": {
          const event = message.data as JobCreatedEvent;
          setLastJobCreated(event);
          options.onJobCreated?.(event);
          break;
        }

        case "status_changed": {
          const event = message.data as StatusChangedEvent;
          setLastStatusChanged(event);
          options.onStatusChanged?.(event);
          break;
        }

        case "delay_alert": {
          const event = message.data as DelayAlertEvent;
          setLastDelayAlert(event);
          options.onDelayAlert?.(event);
          break;
        }

        case "cargo_update": {
          const event = message.data as CargoUpdateEvent;
          setLastCargoUpdate(event);
          options.onCargoUpdate?.(event);
          break;
        }

        default:
          console.warn(
            "Unknown scheduling WebSocket message type:",
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

  // Build the WebSocket URL with subscription query params
  const wsUrl = useMemo(() => {
    if (subscriptions.length === 0) {
      return SCHEDULING_WS_URL;
    }
    const params = new URLSearchParams();
    subscriptions.forEach((sub) => {
      params.append("subscribe", sub);
    });
    return `${SCHEDULING_WS_URL}?${params.toString()}`;
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
    lastJobCreated,
    lastStatusChanged,
    lastDelayAlert,
    lastCargoUpdate,
    error,
    connect,
    disconnect,
    send,
    connectionStatus,
  };
}

export default useSchedulingWebSocket;
