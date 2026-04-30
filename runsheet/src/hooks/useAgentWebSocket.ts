/**
 * Specialized WebSocket hook for agent activity real-time updates.
 *
 * Wraps the base useWebSocket hook with agent-specific message handling
 * for the `/ws/agent-activity` channel. Provides typed state for
 * activity log events, approval queue events, and agent action events.
 *
 * Validates:
 * - Requirement 8.7: Real-time agent activity feed via WebSocket
 * - Requirement 9.2: Agent activity feed panel with real-time updates
 * - Requirement 9.7: Toast notifications for autonomous agent actions
 */

import { useCallback, useMemo, useState } from "react";
import type { ActivityLogEntry, ApprovalEntry } from "../services/agentApi";
import {
  useWebSocket,
  type WebSocketOptions,
  type WebSocketState,
} from "./useWebSocket";

// Derive WebSocket URL from API base URL
const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
const WS_BASE = API_BASE_URL.replace(/\/api$/, "").replace("http", "ws");
const AGENT_WS_URL = `${WS_BASE}/ws/agent-activity`;

/**
 * Event types the agent activity WebSocket can deliver
 */
export type AgentEventType =
  | "activity"
  | "approval_created"
  | "approval_approved"
  | "approval_rejected"
  | "approval_expired"
  | "agent_action_executed";

/**
 * Base message structure from the agent activity WebSocket endpoint
 */
export interface AgentWebSocketMessage {
  type: AgentEventType | "connection" | "heartbeat";
  timestamp: string;
  data?: unknown;
  status?: string;
  message?: string;
}

/**
 * Options for the useAgentWebSocket hook
 */
export interface AgentWebSocketOptions {
  /** Whether to automatically connect on mount */
  autoConnect?: boolean;
  /** Callback when an activity log event is received */
  onActivity?: (entry: ActivityLogEntry) => void;
  /** Callback when an approval event is received */
  onApprovalEvent?: (event: { type: string; approval: ApprovalEntry }) => void;
  /** Callback when an agent action is executed */
  onAgentAction?: (entry: ActivityLogEntry) => void;
  /** Callback when connection state changes */
  onConnectionStatusChange?: (state: WebSocketState) => void;
  /** Callback when reconnection starts */
  onReconnecting?: (attempt: number, delay: number) => void;
}

/**
 * Return type for the useAgentWebSocket hook
 */
export interface UseAgentWebSocketReturn {
  /** Current connection state */
  state: WebSocketState;
  /** Whether the WebSocket is currently connected */
  isConnected: boolean;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt: number;
  /** Time until next reconnection attempt (ms, 0 if not reconnecting) */
  reconnectDelay: number;
  /** Last received activity event */
  lastActivity: ActivityLogEntry | null;
  /** Last received approval event */
  lastApprovalEvent: { type: string; approval: ApprovalEntry } | null;
  /** Last received agent action event */
  lastAgentAction: ActivityLogEntry | null;
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
 * Custom hook for agent activity real-time updates via WebSocket.
 *
 * Connects to `/ws/agent-activity` and provides typed state for
 * activity log events, approval queue events, and agent action events.
 * Uses exponential backoff (1s initial, 30s max) for auto-reconnection.
 *
 * @param options - Configuration options
 * @returns Agent WebSocket state and control functions
 *
 * @example
 * ```tsx
 * const { isConnected, lastActivity, lastApprovalEvent } = useAgentWebSocket({
 *   onActivity: (entry) => {
 *     console.log(`Agent ${entry.agent_id} performed ${entry.action_type}`);
 *   },
 *   onApprovalEvent: (event) => {
 *     console.log(`Approval ${event.type}: ${event.approval.action_id}`);
 *   },
 * });
 * ```
 */
export function useAgentWebSocket(
  options: AgentWebSocketOptions = {},
): UseAgentWebSocketReturn {
  const [lastActivity, setLastActivity] = useState<ActivityLogEntry | null>(
    null,
  );
  const [lastApprovalEvent, setLastApprovalEvent] = useState<{
    type: string;
    approval: ApprovalEntry;
  } | null>(null);
  const [lastAgentAction, setLastAgentAction] =
    useState<ActivityLogEntry | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);

  /**
   * Handle incoming WebSocket messages and dispatch by event type
   */
  const handleMessage = useCallback(
    (data: unknown) => {
      const message = data as AgentWebSocketMessage;

      switch (message.type) {
        case "connection":
          setConnectionStatus(message.message || message.status || "connected");
          break;

        case "heartbeat":
          // Heartbeat received — connection is alive
          break;

        case "activity": {
          const entry = message.data as ActivityLogEntry;
          setLastActivity(entry);
          options.onActivity?.(entry);
          break;
        }

        case "approval_created":
        case "approval_approved":
        case "approval_rejected":
        case "approval_expired": {
          const approval = message.data as ApprovalEntry;
          const event = { type: message.type, approval };
          setLastApprovalEvent(event);
          options.onApprovalEvent?.(event);
          break;
        }

        case "agent_action_executed": {
          const action = message.data as ActivityLogEntry;
          setLastAgentAction(action);
          options.onAgentAction?.(action);
          break;
        }

        default:
          console.warn(
            "Unknown agent WebSocket message type:",
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

  // Configure the base WebSocket hook
  const wsOptions: WebSocketOptions = useMemo(
    () => ({
      autoConnect: options.autoConnect ?? true,
      initialReconnectDelay: 1000,
      maxReconnectDelay: 30000,
      maxReconnectAttempts: 0, // Infinite attempts
      backoffMultiplier: 2,
      onConnect: handleConnect,
      onDisconnect: handleDisconnect,
      onMessage: handleMessage,
      onReconnecting: options.onReconnecting,
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
  } = useWebSocket(AGENT_WS_URL, wsOptions);

  return {
    state,
    isConnected,
    reconnectAttempt,
    reconnectDelay,
    lastActivity,
    lastApprovalEvent,
    lastAgentAction,
    error,
    connect,
    disconnect,
    connectionStatus,
  };
}

export default useAgentWebSocket;
