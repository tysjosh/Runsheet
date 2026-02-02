/**
 * Specialized WebSocket hook for fleet real-time updates.
 * 
 * This hook wraps the base useWebSocket hook with fleet-specific
 * message handling and types.
 * 
 * Validates:
 * - Requirement 9.5: WHEN the WebSocket connection drops, THE Frontend_Application
 *   SHALL automatically attempt reconnection with exponential backoff
 */

import { useState, useCallback, useMemo } from 'react';
import { useWebSocket, WebSocketState, WebSocketOptions } from './useWebSocket';
import { Truck } from '../types/api';

// API base URL for WebSocket
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api';
const WS_URL = `${API_BASE_URL.replace('http', 'ws')}/fleet/live`;

/**
 * Message types from the backend WebSocket
 */
export type FleetMessageType = 
  | 'connection'
  | 'location_update'
  | 'batch_location_update'
  | 'heartbeat';

/**
 * Base message structure from backend
 */
export interface FleetMessage {
  type: FleetMessageType;
  timestamp: string;
  data?: unknown;
  status?: string;
  message?: string;
}

/**
 * Location update message data
 */
export interface LocationUpdateData {
  truck_id: string;
  coordinates: {
    lat: number;
    lon: number;
  };
  timestamp: string;
  speed_kmh?: number;
  heading?: number;
}

/**
 * Batch location update message data
 */
export interface BatchLocationUpdateData {
  updates: LocationUpdateData[];
  count: number;
}

/**
 * Connection status message
 */
export interface ConnectionMessage extends FleetMessage {
  type: 'connection';
  status: string;
  message: string;
}

/**
 * Options for the fleet WebSocket hook
 */
export interface FleetWebSocketOptions {
  /** Whether to automatically connect on mount */
  autoConnect?: boolean;
  /** Callback when a location update is received */
  onLocationUpdate?: (update: LocationUpdateData) => void;
  /** Callback when a batch location update is received */
  onBatchLocationUpdate?: (updates: LocationUpdateData[]) => void;
  /** Callback when connection status changes */
  onConnectionStatusChange?: (state: WebSocketState) => void;
  /** Callback when reconnection starts */
  onReconnecting?: (attempt: number, delay: number) => void;
  /** Callback when max reconnection attempts reached */
  onMaxReconnectAttemptsReached?: () => void;
}

/**
 * Return type for the useFleetWebSocket hook
 */
export interface UseFleetWebSocketReturn {
  /** Current connection state */
  state: WebSocketState;
  /** Whether the WebSocket is currently connected */
  isConnected: boolean;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt: number;
  /** Time until next reconnection attempt (ms, 0 if not reconnecting) */
  reconnectDelay: number;
  /** Last received location update */
  lastLocationUpdate: LocationUpdateData | null;
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
 * Custom hook for fleet real-time updates via WebSocket.
 * 
 * Provides automatic reconnection with exponential backoff and
 * fleet-specific message handling.
 * 
 * @param options - Configuration options
 * @returns Fleet WebSocket state and control functions
 * 
 * @example
 * ```tsx
 * const { state, isConnected, lastLocationUpdate, reconnectAttempt } = useFleetWebSocket({
 *   onLocationUpdate: (update) => {
 *     console.log(`Truck ${update.truck_id} moved to ${update.coordinates.lat}, ${update.coordinates.lon}`);
 *   },
 *   onReconnecting: (attempt, delay) => {
 *     console.log(`Reconnecting in ${delay}ms (attempt ${attempt})`);
 *   },
 * });
 * ```
 */
export function useFleetWebSocket(
  options: FleetWebSocketOptions = {}
): UseFleetWebSocketReturn {
  const [lastLocationUpdate, setLastLocationUpdate] = useState<LocationUpdateData | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);

  /**
   * Handle incoming WebSocket messages
   */
  const handleMessage = useCallback((data: unknown) => {
    const message = data as FleetMessage;

    switch (message.type) {
      case 'connection':
        const connMsg = message as ConnectionMessage;
        setConnectionStatus(connMsg.message || connMsg.status);
        break;

      case 'location_update':
        const locationData = message.data as LocationUpdateData;
        setLastLocationUpdate(locationData);
        options.onLocationUpdate?.(locationData);
        break;

      case 'batch_location_update':
        const batchData = message.data as BatchLocationUpdateData;
        if (batchData.updates && batchData.updates.length > 0) {
          // Set the last update from the batch
          setLastLocationUpdate(batchData.updates[batchData.updates.length - 1]);
          options.onBatchLocationUpdate?.(batchData.updates);
        }
        break;

      case 'heartbeat':
        // Heartbeat received - connection is alive
        break;

      default:
        console.warn('Unknown fleet message type:', message.type);
    }
  }, [options]);

  /**
   * Handle connection state changes
   */
  const handleConnect = useCallback(() => {
    options.onConnectionStatusChange?.('connected');
  }, [options]);

  const handleDisconnect = useCallback(() => {
    options.onConnectionStatusChange?.('disconnected');
    setConnectionStatus(null);
  }, [options]);

  // WebSocket options
  const wsOptions: WebSocketOptions = useMemo(() => ({
    autoConnect: options.autoConnect ?? true,
    initialReconnectDelay: 1000,    // Start with 1 second
    maxReconnectDelay: 30000,       // Max 30 seconds
    maxReconnectAttempts: 0,        // Infinite attempts
    backoffMultiplier: 2,           // Double each time
    onConnect: handleConnect,
    onDisconnect: handleDisconnect,
    onMessage: handleMessage,
    onReconnecting: options.onReconnecting,
    onMaxReconnectAttemptsReached: options.onMaxReconnectAttemptsReached,
  }), [handleConnect, handleDisconnect, handleMessage, options]);

  // Use the base WebSocket hook
  const {
    state,
    isConnected,
    reconnectAttempt,
    reconnectDelay,
    error,
    connect,
    disconnect,
  } = useWebSocket(WS_URL, wsOptions);

  return {
    state,
    isConnected,
    reconnectAttempt,
    reconnectDelay,
    lastLocationUpdate,
    error,
    connect,
    disconnect,
    connectionStatus,
  };
}

export default useFleetWebSocket;
