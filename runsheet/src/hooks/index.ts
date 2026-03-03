/**
 * Custom React hooks for the Runsheet application.
 */

export { useWebSocket } from './useWebSocket';
export type {
  WebSocketState,
  WebSocketOptions,
  UseWebSocketReturn,
} from './useWebSocket';

export { useFleetWebSocket } from './useFleetWebSocket';
export type {
  FleetMessageType,
  FleetMessage,
  LocationUpdateData,
  BatchLocationUpdateData,
  ConnectionMessage,
  FleetWebSocketOptions,
  UseFleetWebSocketReturn,
} from './useFleetWebSocket';

export { useOpsWebSocket } from './useOpsWebSocket';
export type {
  OpsEventType,
  OpsWebSocketMessage,
  SlaBreach,
  OpsWebSocketOptions,
  UseOpsWebSocketReturn,
} from './useOpsWebSocket';
