/**
 * Specialized WebSocket hook for order real-time updates.
 *
 * Wraps the base useWebSocket hook with order-specific message handling,
 * subscription filters, and typed state for order and driver events.
 *
 * Validates: Requirements 4.1, 8.1.5
 */

import { useCallback, useMemo, useState } from "react";
import { getAuthToken } from "../utils/auth";
import type { FuelOrder } from "../services/ordersApi";
import {
  useWebSocket,
  type WebSocketOptions,
  type WebSocketState,
} from "./useWebSocket";

// Derive WebSocket URL from API base URL
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
const WS_BASE = API_BASE_URL.replace(/\/api$/, "").replace("http", "ws");
const ORDERS_WS_BASE_URL = `${WS_BASE}/ws/orders`;

/**
 * Build WebSocket URL with JWT token for authentication
 */
async function buildOrdersWebSocketUrl(subscriptions: OrdersEventType[]): Promise<string> {
  const token = await getAuthToken();
  if (!token) return ORDERS_WS_BASE_URL;
  
  const params = new URLSearchParams();
  params.set("token", token);
  subscriptions.forEach((sub) => {
    params.append("subscribe", sub);
  });
  return `${ORDERS_WS_BASE_URL}?${params.toString()}`;
}

/**
 * Event types the orders WebSocket can deliver
 */
export type OrdersEventType =
  | "order_placed"
  | "order_status_changed"
  | "order_assigned"
  | "driver_update"
  | "sla_breach";

/**
 * Base message structure from the orders WebSocket endpoint
 */
export interface OrdersWebSocketMessage {
  type: OrdersEventType | "connection" | "heartbeat";
  timestamp: string;
  data?: unknown;
  status?: string;
  message?: string;
}

/**
 * Options for the orders WebSocket hook
 */
export interface OrdersWebSocketOptions {
  /** Event types to subscribe to. Defaults to all types. */
  subscriptions?: OrdersEventType[];
  /** Whether to automatically connect on mount */
  autoConnect?: boolean;
  /** Callback when an order is placed */
  onOrderPlaced?: (order: FuelOrder) => void;
  /** Callback when an order status changes */
  onOrderStatusChanged?: (order: FuelOrder) => void;
  /** Callback when an order is assigned */
  onOrderAssigned?: (order: FuelOrder) => void;
  /** Callback when connection state changes */
  onConnectionStatusChange?: (state: WebSocketState) => void;
}

/**
 * Return type for the useOrdersWebSocket hook
 */
export interface UseOrdersWebSocketReturn {
  /** Current connection state */
  state: WebSocketState;
  /** Whether the WebSocket is currently connected */
  isConnected: boolean;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt: number;
  /** Last received order update */
  lastOrderUpdate: FuelOrder | null;
  /** Error if any occurred */
  error: Event | null;
  /** Manually connect to the WebSocket */
  connect: () => void;
  /** Manually disconnect from the WebSocket */
  disconnect: () => void;
  /** Send a message through the WebSocket */
  send: (data: unknown) => boolean;
}

/**
 * Custom hook for order real-time updates via WebSocket.
 *
 * Connects to `/ws/orders` with subscription filters and provides typed state
 * for order events. Uses exponential backoff (1s initial, 30s max) for
 * auto-reconnection.
 *
 * @param options - Configuration options
 * @returns Orders WebSocket state and control functions
 */
export function useOrdersWebSocket(
  options: OrdersWebSocketOptions = {},
): UseOrdersWebSocketReturn {
  const [lastOrderUpdate, setLastOrderUpdate] = useState<FuelOrder | null>(
    null,
  );

  const subscriptions = options.subscriptions ?? [
    "order_placed",
    "order_status_changed",
    "order_assigned",
    "driver_update",
    "sla_breach",
  ];

  const handleMessage = useCallback(
    (data: unknown) => {
      const message = data as OrdersWebSocketMessage;

      switch (message.type) {
        case "connection":
        case "heartbeat":
          break;

        case "order_placed": {
          const order = message.data as FuelOrder;
          setLastOrderUpdate(order);
          options.onOrderPlaced?.(order);
          break;
        }

        case "order_status_changed": {
          const order = message.data as FuelOrder;
          setLastOrderUpdate(order);
          options.onOrderStatusChanged?.(order);
          break;
        }

        case "order_assigned": {
          const order = message.data as FuelOrder;
          setLastOrderUpdate(order);
          options.onOrderAssigned?.(order);
          break;
        }

        default:
          break;
      }
    },
    [options],
  );

  const handleConnect = useCallback(() => {
    options.onConnectionStatusChange?.("connected");
  }, [options]);

  const handleDisconnect = useCallback(() => {
    options.onConnectionStatusChange?.("disconnected");
  }, [options]);

  const wsOptions: WebSocketOptions = useMemo(
    () => ({
      autoConnect: options.autoConnect ?? true,
      initialReconnectDelay: 1000,
      maxReconnectDelay: 30000,
      maxReconnectAttempts: 0,
      backoffMultiplier: 2,
      getUrl: () => buildOrdersWebSocketUrl(subscriptions), // Refresh token on each connection
      onConnect: handleConnect,
      onDisconnect: handleDisconnect,
      onMessage: handleMessage,
    }),
    [subscriptions, handleConnect, handleDisconnect, handleMessage, options],
  );

  const {
    state,
    isConnected,
    reconnectAttempt,
    error,
    connect,
    disconnect,
    send,
  } = useWebSocket("", wsOptions);

  return {
    state,
    isConnected,
    reconnectAttempt,
    lastOrderUpdate,
    error,
    connect,
    disconnect,
    send,
  };
}

export default useOrdersWebSocket;
