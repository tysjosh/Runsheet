/**
 * Specialized WebSocket hook for notification real-time updates.
 *
 * Wraps the base useWebSocket hook with notification-specific message handling
 * and typed state for notification_created and notification_status_changed events.
 *
 * Validates:
 * - Requirement 11.1: WHEN a new Notification is created, THE Notification_WebSocket
 *   SHALL broadcast the Notification to all connected clients on the `/ws/notifications`
 *   channel within 2 seconds.
 * - Requirement 11.2: THE Notification_WebSocket SHALL follow the same connection
 *   lifecycle pattern as the existing `/ws/scheduling` WebSocket endpoint, including
 *   JWT authentication, tenant scoping, heartbeat, and reconnection with exponential backoff.
 * - Requirement 11.3: WHEN a Notification delivery_status changes, THE Notification_WebSocket
 *   SHALL broadcast a status update event to connected clients.
 * - Requirement 11.4: THE Support_Page SHALL establish a WebSocket connection to
 *   `/ws/notifications` when the Notifications tab is active and disconnect when
 *   navigating away.
 */

import { useCallback, useMemo, useState } from "react";
import type { Notification } from "../services/notificationApi";
import {
  useWebSocket,
  type WebSocketOptions,
  type WebSocketState,
} from "./useWebSocket";

// Derive WebSocket URL from API base URL
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
const WS_BASE = API_BASE_URL.replace(/\/api$/, "").replace("http", "ws");
const NOTIFICATION_WS_URL = `${WS_BASE}/ws/notifications`;

/**
 * Event types the notification WebSocket can deliver
 */
export type NotificationEventType =
  | "notification_created"
  | "notification_status_changed";

/**
 * Base message structure from the notification WebSocket endpoint
 */
export interface NotificationWebSocketMessage {
  type: NotificationEventType | "connection" | "heartbeat";
  timestamp?: string;
  data?: unknown;
  status?: string;
  message?: string;
}

/**
 * Data payload for a notification_created event
 */
export interface NotificationCreatedEvent {
  notification: Notification;
}

/**
 * Data payload for a notification_status_changed event
 */
export interface NotificationStatusChangedEvent {
  notification_id: string;
  delivery_status: string;
  updated_at: string;
  sent_at?: string | null;
  delivered_at?: string | null;
  failed_at?: string | null;
  failure_reason?: string | null;
}

/**
 * Options for the notification WebSocket hook
 */
export interface NotificationWebSocketOptions {
  /** JWT token for authentication (appended as query param) */
  token?: string;
  /** Whether to automatically connect on mount */
  autoConnect?: boolean;
  /** Callback when a notification_created event is received */
  onNotificationCreated?: (event: NotificationCreatedEvent) => void;
  /** Callback when a notification_status_changed event is received */
  onStatusChanged?: (event: NotificationStatusChangedEvent) => void;
  /** Callback when connection state changes */
  onConnectionStatusChange?: (state: WebSocketState) => void;
  /** Callback when reconnection starts */
  onReconnecting?: (attempt: number, delay: number) => void;
  /** Callback when max reconnection attempts reached */
  onMaxReconnectAttemptsReached?: () => void;
}

/**
 * Return type for the useNotificationWebSocket hook
 */
export interface UseNotificationWebSocketReturn {
  /** Current connection state */
  state: WebSocketState;
  /** Whether the WebSocket is currently connected */
  isConnected: boolean;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt: number;
  /** Time until next reconnection attempt (ms, 0 if not reconnecting) */
  reconnectDelay: number;
  /** Last received notification_created event */
  lastNotificationCreated: NotificationCreatedEvent | null;
  /** Last received notification_status_changed event */
  lastStatusChanged: NotificationStatusChangedEvent | null;
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
 * Custom hook for notification real-time updates via WebSocket.
 *
 * Connects to `/ws/notifications` with JWT token authentication and provides
 * typed state for notification creation and status change events.
 * Uses exponential backoff (1s initial, 30s max) for auto-reconnection.
 *
 * @param options - Configuration options
 * @returns Notification WebSocket state and control functions
 *
 * @example
 * ```tsx
 * const { isConnected, lastNotificationCreated, lastStatusChanged } = useNotificationWebSocket({
 *   token: 'my-jwt-token',
 *   onNotificationCreated: (event) => {
 *     console.log(`New notification: ${event.notification.notification_id}`);
 *   },
 *   onStatusChanged: (event) => {
 *     console.log(`Notification ${event.notification_id} status: ${event.delivery_status}`);
 *   },
 * });
 * ```
 */
export function useNotificationWebSocket(
  options: NotificationWebSocketOptions = {},
): UseNotificationWebSocketReturn {
  const [lastNotificationCreated, setLastNotificationCreated] =
    useState<NotificationCreatedEvent | null>(null);
  const [lastStatusChanged, setLastStatusChanged] =
    useState<NotificationStatusChangedEvent | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);

  /**
   * Handle incoming WebSocket messages and dispatch by event type
   */
  const handleMessage = useCallback(
    (data: unknown) => {
      const message = data as NotificationWebSocketMessage;

      switch (message.type) {
        case "connection":
          setConnectionStatus(message.message || message.status || "connected");
          break;

        case "heartbeat":
          // Heartbeat received — connection is alive, nothing to do
          break;

        case "notification_created": {
          const event = message.data as NotificationCreatedEvent;
          setLastNotificationCreated(event);
          options.onNotificationCreated?.(event);
          break;
        }

        case "notification_status_changed": {
          const event = message.data as NotificationStatusChangedEvent;
          setLastStatusChanged(event);
          options.onStatusChanged?.(event);
          break;
        }

        default:
          console.warn(
            "Unknown notification WebSocket message type:",
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

  // Build the WebSocket URL with JWT token query param
  const wsUrl = useMemo(() => {
    if (options.token) {
      const params = new URLSearchParams();
      params.set("token", options.token);
      return `${NOTIFICATION_WS_URL}?${params.toString()}`;
    }
    return NOTIFICATION_WS_URL;
  }, [options.token]);

  // Configure the base WebSocket hook with exponential backoff
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
    lastNotificationCreated,
    lastStatusChanged,
    error,
    connect,
    disconnect,
    send,
    connectionStatus,
  };
}

export default useNotificationWebSocket;
