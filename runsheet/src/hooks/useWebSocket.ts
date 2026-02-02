/**
 * WebSocket hook with automatic reconnection and exponential backoff.
 * 
 * Validates:
 * - Requirement 9.5: WHEN the WebSocket connection drops, THE Frontend_Application
 *   SHALL automatically attempt reconnection with exponential backoff
 */

import { useEffect, useRef, useState, useCallback } from 'react';

/**
 * WebSocket connection states
 */
export type WebSocketState = 'connecting' | 'connected' | 'disconnected' | 'reconnecting';

/**
 * Configuration options for the WebSocket hook
 */
export interface WebSocketOptions {
  /** Initial delay before first reconnection attempt (ms) */
  initialReconnectDelay?: number;
  /** Maximum delay between reconnection attempts (ms) */
  maxReconnectDelay?: number;
  /** Maximum number of reconnection attempts (0 = infinite) */
  maxReconnectAttempts?: number;
  /** Backoff multiplier for exponential backoff */
  backoffMultiplier?: number;
  /** Whether to automatically connect on mount */
  autoConnect?: boolean;
  /** Callback when connection is established */
  onConnect?: () => void;
  /** Callback when connection is closed */
  onDisconnect?: (event: CloseEvent) => void;
  /** Callback when an error occurs */
  onError?: (error: Event) => void;
  /** Callback when a message is received */
  onMessage?: (data: unknown) => void;
  /** Callback when reconnection starts */
  onReconnecting?: (attempt: number, delay: number) => void;
  /** Callback when max reconnection attempts reached */
  onMaxReconnectAttemptsReached?: () => void;
}

/**
 * Return type for the useWebSocket hook
 */
export interface UseWebSocketReturn {
  /** Current connection state */
  state: WebSocketState;
  /** Whether the WebSocket is currently connected */
  isConnected: boolean;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt: number;
  /** Time until next reconnection attempt (ms, 0 if not reconnecting) */
  reconnectDelay: number;
  /** Last received message data */
  lastMessage: unknown | null;
  /** Error if any occurred */
  error: Event | null;
  /** Manually connect to the WebSocket */
  connect: () => void;
  /** Manually disconnect from the WebSocket */
  disconnect: () => void;
  /** Send a message through the WebSocket */
  send: (data: unknown) => boolean;
}

// Default configuration values
const DEFAULT_OPTIONS: Required<Omit<WebSocketOptions, 'onConnect' | 'onDisconnect' | 'onError' | 'onMessage' | 'onReconnecting' | 'onMaxReconnectAttemptsReached'>> = {
  initialReconnectDelay: 1000,    // 1 second
  maxReconnectDelay: 30000,       // 30 seconds
  maxReconnectAttempts: 0,        // Infinite attempts
  backoffMultiplier: 2,           // Double the delay each time
  autoConnect: true,
};

/**
 * Calculate the next reconnection delay using exponential backoff with jitter.
 * 
 * @param attempt - Current reconnection attempt number
 * @param initialDelay - Initial delay in milliseconds
 * @param maxDelay - Maximum delay in milliseconds
 * @param multiplier - Backoff multiplier
 * @returns Delay in milliseconds with jitter applied
 */
function calculateBackoffDelay(
  attempt: number,
  initialDelay: number,
  maxDelay: number,
  multiplier: number
): number {
  // Calculate exponential delay: initialDelay * multiplier^(attempt-1)
  const exponentialDelay = initialDelay * Math.pow(multiplier, attempt - 1);
  
  // Cap at maximum delay
  const cappedDelay = Math.min(exponentialDelay, maxDelay);
  
  // Add jitter (±25%) to prevent thundering herd
  const jitter = cappedDelay * 0.25 * (Math.random() * 2 - 1);
  
  return Math.floor(cappedDelay + jitter);
}

/**
 * Custom hook for WebSocket connections with automatic reconnection
 * and exponential backoff.
 * 
 * @param url - WebSocket URL to connect to
 * @param options - Configuration options
 * @returns WebSocket state and control functions
 * 
 * @example
 * ```tsx
 * const { state, isConnected, lastMessage, connect, disconnect } = useWebSocket(
 *   'ws://localhost:8000/api/fleet/live',
 *   {
 *     onMessage: (data) => console.log('Received:', data),
 *     onReconnecting: (attempt, delay) => console.log(`Reconnecting in ${delay}ms (attempt ${attempt})`),
 *   }
 * );
 * ```
 */
export function useWebSocket(
  url: string,
  options: WebSocketOptions = {}
): UseWebSocketReturn {
  // Merge options with defaults
  const config = {
    ...DEFAULT_OPTIONS,
    ...options,
  };

  // State
  const [state, setState] = useState<WebSocketState>('disconnected');
  const [reconnectAttempt, setReconnectAttempt] = useState(0);
  const [reconnectDelay, setReconnectDelay] = useState(0);
  const [lastMessage, setLastMessage] = useState<unknown | null>(null);
  const [error, setError] = useState<Event | null>(null);

  // Refs for mutable values that shouldn't trigger re-renders
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const shouldReconnectRef = useRef(true);
  const mountedRef = useRef(true);

  /**
   * Clear any pending reconnection timeout
   */
  const clearReconnectTimeout = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
  }, []);

  /**
   * Schedule a reconnection attempt with exponential backoff
   */
  const scheduleReconnect = useCallback((attempt: number) => {
    if (!mountedRef.current || !shouldReconnectRef.current) {
      return;
    }

    // Check if max attempts reached
    if (config.maxReconnectAttempts > 0 && attempt > config.maxReconnectAttempts) {
      setState('disconnected');
      setReconnectAttempt(0);
      setReconnectDelay(0);
      options.onMaxReconnectAttemptsReached?.();
      return;
    }

    const delay = calculateBackoffDelay(
      attempt,
      config.initialReconnectDelay,
      config.maxReconnectDelay,
      config.backoffMultiplier
    );

    setState('reconnecting');
    setReconnectAttempt(attempt);
    setReconnectDelay(delay);

    options.onReconnecting?.(attempt, delay);

    reconnectTimeoutRef.current = setTimeout(() => {
      if (mountedRef.current && shouldReconnectRef.current) {
        connectInternal(attempt);
      }
    }, delay);
  }, [config.initialReconnectDelay, config.maxReconnectDelay, config.backoffMultiplier, config.maxReconnectAttempts, options]);

  /**
   * Internal connect function that handles the WebSocket connection
   */
  const connectInternal = useCallback((currentAttempt: number = 0) => {
    if (!mountedRef.current) {
      return;
    }

    // Close existing connection if any
    if (wsRef.current) {
      wsRef.current.onclose = null; // Prevent triggering reconnect
      wsRef.current.close();
      wsRef.current = null;
    }

    clearReconnectTimeout();
    setState('connecting');
    setError(null);

    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!mountedRef.current) return;
        
        setState('connected');
        setReconnectAttempt(0);
        setReconnectDelay(0);
        setError(null);
        options.onConnect?.();
      };

      ws.onclose = (event) => {
        if (!mountedRef.current) return;

        setState('disconnected');
        options.onDisconnect?.(event);

        // Only reconnect if it wasn't a clean close and we should reconnect
        if (shouldReconnectRef.current && !event.wasClean) {
          scheduleReconnect(currentAttempt + 1);
        }
      };

      ws.onerror = (event) => {
        if (!mountedRef.current) return;

        setError(event);
        options.onError?.(event);
        
        // The error event is usually followed by a close event,
        // so we don't need to trigger reconnect here
      };

      ws.onmessage = (event) => {
        if (!mountedRef.current) return;

        try {
          const data = JSON.parse(event.data);
          setLastMessage(data);
          options.onMessage?.(data);
        } catch {
          // If not JSON, pass raw data
          setLastMessage(event.data);
          options.onMessage?.(event.data);
        }
      };
    } catch (err) {
      if (!mountedRef.current) return;

      setState('disconnected');
      
      // Schedule reconnect on connection error
      if (shouldReconnectRef.current) {
        scheduleReconnect(currentAttempt + 1);
      }
    }
  }, [url, clearReconnectTimeout, scheduleReconnect, options]);

  /**
   * Public connect function
   */
  const connect = useCallback(() => {
    shouldReconnectRef.current = true;
    connectInternal(0);
  }, [connectInternal]);

  /**
   * Public disconnect function
   */
  const disconnect = useCallback(() => {
    shouldReconnectRef.current = false;
    clearReconnectTimeout();
    setReconnectAttempt(0);
    setReconnectDelay(0);

    if (wsRef.current) {
      wsRef.current.onclose = null; // Prevent triggering reconnect
      wsRef.current.close(1000, 'Client disconnected');
      wsRef.current = null;
    }

    setState('disconnected');
  }, [clearReconnectTimeout]);

  /**
   * Send a message through the WebSocket
   */
  const send = useCallback((data: unknown): boolean => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      console.warn('WebSocket is not connected. Cannot send message.');
      return false;
    }

    try {
      const message = typeof data === 'string' ? data : JSON.stringify(data);
      wsRef.current.send(message);
      return true;
    } catch (err) {
      console.error('Failed to send WebSocket message:', err);
      return false;
    }
  }, []);

  // Auto-connect on mount if enabled
  useEffect(() => {
    mountedRef.current = true;

    if (config.autoConnect) {
      connect();
    }

    // Cleanup on unmount
    return () => {
      mountedRef.current = false;
      shouldReconnectRef.current = false;
      clearReconnectTimeout();

      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close(1000, 'Component unmounted');
        wsRef.current = null;
      }
    };
  }, [url]); // Only re-run if URL changes

  return {
    state,
    isConnected: state === 'connected',
    reconnectAttempt,
    reconnectDelay,
    lastMessage,
    error,
    connect,
    disconnect,
    send,
  };
}

export default useWebSocket;
