# ADR 004: WebSocket Architecture

## Status

Accepted

## Context

The Runsheet platform requires real-time push communication for four distinct operational domains:

1. **Fleet tracking** (`/api/fleet/live`): Live vehicle location updates, batch fleet status changes, and heartbeat signals for connected dashboards.
2. **Ops monitoring** (`/ws/ops`): Shipment status updates, rider assignment changes, and SLA breach alerts with tenant-scoped filtering.
3. **Scheduling** (`/ws/scheduling`): Job creation notifications, status transitions, delay alerts, and cargo updates with subscription-based filtering and periodic heartbeats.
4. **Agent activity** (`/ws/agent-activity`): AI agent action logs, approval queue events, and orchestration decisions for the agent monitoring dashboard.

Each domain has different message types, subscription models, and client interaction patterns. The platform needed to decide between two architectural approaches:

1. **Unified pub/sub system**: A single WebSocket manager with a topic-based subscription model. Clients subscribe to topics (e.g., `fleet.location`, `ops.shipment.update`) and receive messages matching their subscriptions. One connection endpoint serves all domains.

2. **Separate per-domain WebSocket managers**: Each domain has its own WebSocket manager class with domain-specific broadcast methods, connection handling, and subscription logic. Each domain exposes its own WebSocket endpoint.

## Decision

We chose separate per-domain WebSocket managers, each extending a shared `BaseWSManager` base class.

The architecture consists of:

- **`BaseWSManager`** (`websocket/base_ws_manager.py`): Abstract base class providing common lifecycle management, connection registry with metadata, Prometheus-compatible metrics, configurable backpressure, stale client detection, and dead client cleanup.
- **`ConnectionManager`** (fleet): Handles live vehicle tracking with `broadcast_location_update`, `broadcast_batch_update`, and `send_heartbeat`.
- **`OpsWebSocketManager`** (ops): Handles shipment/rider updates with tenant-scoped connections, subscription filtering, and `disconnect_tenant` for feature flag enforcement.
- **`SchedulingWebSocketManager`** (scheduling): Handles job lifecycle events with subscription filtering, periodic heartbeat loops, and delay alert broadcasting.
- **`AgentActivityWSManager`** (agent activity): Handles agent action logs and approval events with `broadcast_activity`, `broadcast_approval_event`, and `broadcast_event`.

All four managers share the `BaseWSManager` contract:

- Standard connection lifecycle: `connect` (with handshake confirmation), `disconnect`, `shutdown`
- Backpressure enforcement: Messages are dropped for clients whose pending send queue exceeds a configurable threshold (default: 100 messages)
- Metrics emission: `connections_total`, `disconnections_total`, `messages_sent_total`, `send_failures_total`, `messages_dropped_total`, plus an `active_connections` gauge, all labeled by manager name and tenant ID
- Dead client cleanup: Failed sends during broadcast trigger automatic client removal within 5 seconds
- Stale client detection: Tracking of `last_send` timestamp per client

Key design choices:

- **Per-domain endpoints over unified endpoint**: Each domain has its own WebSocket URL (`/ws/ops`, `/ws/scheduling`, `/ws/agent-activity`, `/api/fleet/live`). This allows domain-specific authentication, rate limiting, and monitoring.
- **Shared base class for consistency**: The `BaseWSManager` eliminates the code duplication that existed when each manager independently implemented connection tracking, error handling, and cleanup.
- **Backpressure at the manager level**: Rather than relying on OS-level TCP backpressure or application-level message queues, each manager enforces a per-client pending message limit. This prevents a single slow client from degrading broadcast performance for all clients.

## Consequences

### Positive

- **Domain isolation**: A bug or performance issue in the ops WebSocket manager does not affect fleet tracking or scheduling. Each manager can be debugged and optimized independently.
- **Tailored subscription models**: Each domain implements the subscription model that fits its use case. Ops uses tenant-scoped filtering, scheduling uses topic-based subscriptions, and fleet uses broadcast-all. A unified pub/sub system would need to accommodate all these patterns.
- **Independent scaling**: In a future multi-process deployment, each WebSocket endpoint could be scaled independently based on its connection count and message volume.
- **Clear metrics**: Metrics are labeled by manager name, making it straightforward to monitor each domain's WebSocket health separately in dashboards and alerts.
- **Consistent lifecycle**: The `BaseWSManager` ensures all managers send the same handshake confirmation message, track the same metrics, and enforce the same backpressure policy. This consistency was previously missing when each manager was implemented independently.

### Negative

- **Multiple connections per client**: A dashboard that needs data from multiple domains must maintain multiple WebSocket connections. This increases client-side complexity and connection count.
- **No cross-domain subscriptions**: A client cannot subscribe to events from multiple domains through a single connection. If a future feature requires cross-domain real-time updates (e.g., "show me fuel alerts alongside scheduling delays"), it would require either a new aggregating endpoint or client-side merging.
- **Four managers to maintain**: Despite the shared base class, each manager has domain-specific methods that must be maintained. Changes to the broadcast contract require updates across all four managers.
- **Connection overhead**: Four separate WebSocket endpoints mean four separate connection pools, four sets of heartbeat timers, and four sets of metrics. A unified system would have lower per-connection overhead.
