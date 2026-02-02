/**
 * WebSocket connection status indicator component.
 * 
 * Displays the current WebSocket connection state with visual feedback
 * for connected, disconnected, and reconnecting states.
 * 
 * Validates:
 * - Requirement 9.5: Handle connection drop gracefully with user feedback
 */

import React from 'react';
import { WebSocketState } from '../hooks/useWebSocket';

interface WebSocketStatusProps {
  /** Current WebSocket connection state */
  state: WebSocketState;
  /** Current reconnection attempt number (0 if not reconnecting) */
  reconnectAttempt?: number;
  /** Time until next reconnection attempt (ms) */
  reconnectDelay?: number;
  /** Whether to show detailed status text */
  showDetails?: boolean;
  /** Custom class name for the container */
  className?: string;
}

/**
 * Get status color based on connection state
 */
function getStatusColor(state: WebSocketState): string {
  switch (state) {
    case 'connected':
      return 'bg-green-500';
    case 'connecting':
      return 'bg-yellow-500 animate-pulse';
    case 'reconnecting':
      return 'bg-orange-500 animate-pulse';
    case 'disconnected':
      return 'bg-red-500';
    default:
      return 'bg-gray-500';
  }
}

/**
 * Get status text based on connection state
 */
function getStatusText(
  state: WebSocketState,
  reconnectAttempt?: number,
  reconnectDelay?: number
): string {
  switch (state) {
    case 'connected':
      return 'Live';
    case 'connecting':
      return 'Connecting...';
    case 'reconnecting':
      if (reconnectAttempt && reconnectDelay) {
        const seconds = Math.ceil(reconnectDelay / 1000);
        return `Reconnecting in ${seconds}s (attempt ${reconnectAttempt})`;
      }
      return 'Reconnecting...';
    case 'disconnected':
      return 'Disconnected';
    default:
      return 'Unknown';
  }
}

/**
 * WebSocket status indicator component.
 * 
 * Shows a colored dot and optional text indicating the current
 * WebSocket connection state.
 * 
 * @example
 * ```tsx
 * <WebSocketStatus
 *   state={wsState}
 *   reconnectAttempt={reconnectAttempt}
 *   reconnectDelay={reconnectDelay}
 *   showDetails
 * />
 * ```
 */
export default function WebSocketStatus({
  state,
  reconnectAttempt = 0,
  reconnectDelay = 0,
  showDetails = false,
  className = '',
}: WebSocketStatusProps) {
  const statusColor = getStatusColor(state);
  const statusText = getStatusText(state, reconnectAttempt, reconnectDelay);

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      {/* Status indicator dot */}
      <div
        className={`w-2 h-2 rounded-full ${statusColor}`}
        title={statusText}
        aria-label={`WebSocket status: ${statusText}`}
      />
      
      {/* Status text (optional) */}
      {showDetails && (
        <span className="text-xs text-gray-600">
          {statusText}
        </span>
      )}
    </div>
  );
}

/**
 * Compact WebSocket status badge for use in headers/toolbars.
 */
export function WebSocketStatusBadge({
  state,
  reconnectAttempt = 0,
}: Pick<WebSocketStatusProps, 'state' | 'reconnectAttempt'>) {
  const isConnected = state === 'connected';
  const isReconnecting = state === 'reconnecting';

  if (isConnected) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">
        <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
        Live
      </span>
    );
  }

  if (isReconnecting) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-orange-100 text-orange-800">
        <span className="w-1.5 h-1.5 rounded-full bg-orange-500 animate-pulse" />
        Reconnecting ({reconnectAttempt})
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800">
      <span className="w-1.5 h-1.5 rounded-full bg-red-500" />
      Offline
    </span>
  );
}
