/**
 * Specialized WebSocket hook for inventory real-time updates.
 *
 * Wraps the base useWebSocket hook with inventory-specific message handling,
 * subscribing to `inventory_alert` events broadcast by the InventoryMonitorAgent
 * or stock adjustment operations. Triggers toast notifications with item name,
 * status, and location, and exposes a callback for updating local inventory state.
 *
 * Validates:
 * - Requirement 7.5: WHEN an `inventory_alert` WebSocket event is received,
 *   THE frontend SHALL display a real-time toast notification in the operations
 *   control view with the item name, status, and location.
 */

import { useCallback, useMemo, useState } from "react";
import { getAuthToken } from "../utils/auth";
import {
  useWebSocket,
  type WebSocketOptions,
  type WebSocketState,
} from "./useWebSocket";

// Derive WebSocket URL from API base URL
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
const WS_BASE = API_BASE_URL.replace(/\/api$/, "").replace("http", "ws");
const INVENTORY_WS_URL = `${WS_BASE}/ws/inventory`;

/**
 * Build WebSocket URL with JWT token for authentication
 */
async function buildInventoryWebSocketUrl(): Promise<string> {
  const token = await getAuthToken();
  return token
    ? `${INVENTORY_WS_URL}?token=${encodeURIComponent(token)}`
    : INVENTORY_WS_URL;
}

/**
 * Event types the inventory WebSocket can deliver
 */
export type InventoryEventType = "inventory_alert" | "stock_changed";

/**
 * Base message structure from the inventory WebSocket endpoint
 */
export interface InventoryWebSocketMessage {
  type: InventoryEventType | "connection" | "heartbeat";
  timestamp?: string;
  data?: unknown;
  status?: string;
  message?: string;
}

/**
 * Data payload for an inventory_alert event (broadcast by InventoryMonitorAgent)
 */
export interface InventoryAlertEvent {
  item_id: string;
  item_name: string;
  category: string;
  status: "low_stock" | "out_of_stock";
  location: string;
  current_quantity: number;
  min_threshold: number;
  compatible_assets: string[];
  severity: "high" | "critical";
}

/**
 * Data payload for a stock_changed event (broadcast on stock adjustments)
 */
export interface StockChangedEvent {
  item_id: string;
  item_name: string;
  category: string;
  status: string;
  location: string;
  previous_quantity: number;
  new_quantity: number;
  reason: string;
  reference_id?: string;
}

/**
 * Options for the inventory WebSocket hook
 */
export interface InventoryWebSocketOptions {
  /** Whether to automatically connect on mount */
  autoConnect?: boolean;
  /** Callback when an inventory_alert event is received */
  onInventoryAlert?: (alert: InventoryAlertEvent) => void;
  /** Callback when a stock_changed event is received (for updating local state) */
  onStockChanged?: (event: StockChangedEvent) => void;
  /** Callback when connection state changes */
  onConnectionStatusChange?: (state: WebSocketState) => void;
  /** Callback when reconnection starts */
  onReconnecting?: (attempt: number, delay: number) => void;
  /** Callback when max reconnection attempts reached */
  onMaxReconnectAttemptsReached?: () => void;
}

/**
 * Return type for the useInventoryWebSocket hook
 */
export interface UseInventoryWebSocketReturn {
  /** Current connection state */
  state: WebSocketState;
  /** Whether the WebSocket is currently connected */
  isConnected: boolean;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt: number;
  /** Time until next reconnection attempt (ms, 0 if not reconnecting) */
  reconnectDelay: number;
  /** Last received inventory alert event */
  lastAlert: InventoryAlertEvent | null;
  /** Last received stock changed event */
  lastStockChange: StockChangedEvent | null;
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
 * Custom hook for inventory real-time updates via WebSocket.
 *
 * Connects to `/ws/inventory` and provides typed state for inventory alert
 * and stock change events. Uses exponential backoff (1s initial, 30s max)
 * for auto-reconnection.
 *
 * The `onInventoryAlert` callback is intended for triggering toast notifications
 * with item name, status, and location. The `onStockChanged` callback enables
 * updating local inventory state when stock adjustments occur.
 *
 * @param options - Configuration options
 * @returns Inventory WebSocket state and control functions
 *
 * @example
 * ```tsx
 * const { isConnected, lastAlert } = useInventoryWebSocket({
 *   onInventoryAlert: (alert) => {
 *     showToast(`${alert.item_name} is ${alert.status} at ${alert.location}`);
 *   },
 *   onStockChanged: (event) => {
 *     updateLocalInventory(event.item_id, event.new_quantity, event.status);
 *   },
 * });
 * ```
 */
export function useInventoryWebSocket(
  options: InventoryWebSocketOptions = {},
): UseInventoryWebSocketReturn {
  const [lastAlert, setLastAlert] = useState<InventoryAlertEvent | null>(null);
  const [lastStockChange, setLastStockChange] =
    useState<StockChangedEvent | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);

  /**
   * Handle incoming WebSocket messages and dispatch by event type
   */
  const handleMessage = useCallback(
    (data: unknown) => {
      const message = data as InventoryWebSocketMessage;

      switch (message.type) {
        case "connection":
          setConnectionStatus(message.message || message.status || "connected");
          break;

        case "heartbeat":
          // Heartbeat received — connection is alive, nothing to do
          break;

        case "inventory_alert": {
          const alert = message.data as InventoryAlertEvent;
          setLastAlert(alert);
          options.onInventoryAlert?.(alert);
          break;
        }

        case "stock_changed": {
          const event = message.data as StockChangedEvent;
          setLastStockChange(event);
          options.onStockChanged?.(event);
          break;
        }

        default:
          console.warn(
            "Unknown inventory WebSocket message type:",
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

  // Configure the base WebSocket hook with exponential backoff
  const wsOptions: WebSocketOptions = useMemo(
    () => ({
      autoConnect: options.autoConnect ?? true,
      initialReconnectDelay: 1000, // 1 second
      maxReconnectDelay: 30000, // 30 seconds
      maxReconnectAttempts: 0, // Infinite attempts
      backoffMultiplier: 2,
      getUrl: buildInventoryWebSocketUrl, // Refresh token on each connection
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
    lastAlert,
    lastStockChange,
    error,
    connect,
    disconnect,
    send,
    connectionStatus,
  };
}

export default useInventoryWebSocket;
