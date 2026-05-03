/**
 * Custom React hooks for the Runsheet application.
 */

export type {
  BatchLocationUpdateData,
  ConnectionMessage,
  FleetMessage,
  FleetMessageType,
  FleetWebSocketOptions,
  LocationUpdateData,
  UseFleetWebSocketReturn,
} from "./useFleetWebSocket";
export { useFleetWebSocket } from "./useFleetWebSocket";
export type {
  OpsEventType,
  OpsWebSocketMessage,
  OpsWebSocketOptions,
  SlaBreach,
  UseOpsWebSocketReturn,
} from "./useOpsWebSocket";
export { useOpsWebSocket } from "./useOpsWebSocket";
export type {
  UseWebSocketReturn,
  WebSocketOptions,
  WebSocketState,
} from "./useWebSocket";
export { useWebSocket } from "./useWebSocket";

export type {
  AgentEventType,
  AgentWebSocketMessage,
  AgentWebSocketOptions,
  UseAgentWebSocketReturn,
} from "./useAgentWebSocket";
export { useAgentWebSocket } from "./useAgentWebSocket";

export type {
  CargoUpdateEvent,
  DelayAlertEvent,
  JobCreatedEvent,
  SchedulingEventType,
  SchedulingWebSocketMessage,
  SchedulingWebSocketOptions,
  StatusChangedEvent,
  UseSchedulingWebSocketReturn,
} from "./useSchedulingWebSocket";
export { useSchedulingWebSocket } from "./useSchedulingWebSocket";

export type {
  NotificationCreatedEvent,
  NotificationEventType,
  NotificationStatusChangedEvent,
  NotificationWebSocketMessage,
  NotificationWebSocketOptions,
  UseNotificationWebSocketReturn,
} from "./useNotificationWebSocket";
export { useNotificationWebSocket } from "./useNotificationWebSocket";
