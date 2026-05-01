# Requirements Document

## Introduction

This document specifies the requirements for the Ops Intelligence Layer, a comprehensive operational visibility and analytics system for the Runsheet logistics platform. The layer ingests real-time shipment and rider event data from the Dinee external platform via webhooks, materializes it into Elasticsearch read-model indices, and exposes normalized APIs, dashboards, and AI-powered analytics to operations teams. The system spans ingestion (webhook receiver, adapter, replay, poison queue), Elasticsearch modeling (shipments, riders, events indices with strict mappings and lifecycle policies), a tenant-scoped API layer with filtered and aggregated endpoints, a frontend operations dashboard suite, AI assistant tooling for operational queries and reports, and reliability/security infrastructure including tracing, rate limiting, PII masking, and monitoring. Validation includes contract tests, drift detection, load testing, and staged tenant rollout with feature flags.

## Glossary

- **Webhook_Receiver**: The FastAPI endpoint that receives signed webhook payloads from the Dinee platform and verifies their authenticity
- **Dinee_Platform**: The external third-party system that sends shipment and rider event data via webhooks and exposes backfill APIs
- **Adapter_Transformer**: The component that converts Dinee webhook payloads into normalized Elasticsearch documents conforming to Runsheet index mappings
- **Replay_Service**: The background job component that pulls historical data from Dinee APIs to rebuild or backfill Elasticsearch state
- **Poison_Queue**: A persistent queue that stores failed webhook events for inspection and retry processing
- **Shipments_Current_Index**: The Elasticsearch index holding the latest state of each shipment, keyed by shipment_id
- **Shipment_Events_Index**: The Elasticsearch append-only index storing the full event history for shipments, keyed by event_id
- **Riders_Current_Index**: The Elasticsearch index holding the latest state of each rider, keyed by rider_id
- **Ops_API**: The set of FastAPI read endpoints that expose normalized shipment, rider, and event data to the frontend and AI assistant
- **Tenant_Guard**: The middleware/query filter component that enforces tenant-scoped data isolation on every Ops_API request
- **Ops_Dashboard**: The frontend Next.js page suite providing live shipment status board, rider utilization, failure analytics, and shipment tracking views
- **AI_Ops_Tools**: The set of AI agent tool functions that query shipment, rider, and event indices for the Runsheet AI assistant
- **Report_Template**: A predefined AI report format for SLA violations, failure root causes, or rider productivity analysis
- **Feature_Flag_Service**: The component that controls per-tenant rollout of the Ops Intelligence Layer with rollback capability
- **PII_Masker**: The component that redacts personally identifiable information from public-facing API responses and AI outputs
- **Drift_Detector**: The validation component that compares Dinee source state against Runsheet read-model state to detect divergence
- **Schema_Version**: A semantic version string (e.g., `1.0`, `1.1`) embedded in webhook payloads and transformed documents that identifies the payload contract version
- **Event_Sequence**: A monotonically increasing sequence number or timestamp used to determine the canonical ordering of events for a given entity

## Requirements

### Requirement 1: Dinee Webhook Receiver with Signed Verification, Idempotency, and At-Least-Once Semantics

**User Story:** As an operations engineer, I want a secure webhook endpoint that receives Dinee shipment and rider events with signature verification and idempotent processing, so that ingested data is authentic and duplicate deliveries do not corrupt state.

#### Acceptance Criteria

1. THE Webhook_Receiver SHALL expose a POST endpoint at `/webhooks/dinee` that accepts JSON payloads from the Dinee_Platform
2. WHEN a webhook request is received, THE Webhook_Receiver SHALL verify the HMAC-SHA256 signature in the request header against the shared secret before processing the payload
3. IF the signature verification fails, THEN THE Webhook_Receiver SHALL reject the request with a 401 status and log the rejection with the request_id and source IP
4. WHEN a webhook payload is received, THE Webhook_Receiver SHALL extract the event_id and check it against a Redis-backed idempotency store before processing
5. IF the event_id already exists in the idempotency store, THEN THE Webhook_Receiver SHALL return a 200 status without reprocessing the event
6. WHEN a webhook payload passes verification and idempotency checks, THE Webhook_Receiver SHALL pass the payload to the Adapter_Transformer for normalization
7. THE Webhook_Receiver SHALL store processed event_ids in the idempotency store with a configurable TTL defaulting to 72 hours
8. WHEN the Webhook_Receiver successfully processes an event, THE Webhook_Receiver SHALL return a 200 status with the event_id in the response body
9. WHEN a webhook payload is received, THE Webhook_Receiver SHALL validate that the payload contains a `schema_version` field conforming to semantic versioning format
10. IF the `schema_version` in a webhook payload is not recognized by the current Adapter_Transformer, THEN THE Webhook_Receiver SHALL accept the payload, log a warning with the unknown version and event_id, and route the event to the Poison_Queue for manual review
11. THE Webhook_Receiver SHALL operate under at-least-once delivery semantics, where every event is guaranteed to be delivered to the processing pipeline at least once, and idempotent upsert logic in downstream components ensures duplicate deliveries produce no side effects

### Requirement 2: Adapter Transformer for Dinee Payload Normalization with Schema Versioning

**User Story:** As a data engineer, I want Dinee webhook payloads transformed into normalized Elasticsearch documents with schema version tracking, so that downstream indices have a consistent schema regardless of upstream payload changes and version lineage is preserved.

#### Acceptance Criteria

1. WHEN the Adapter_Transformer receives a Dinee shipment event payload, THE Adapter_Transformer SHALL produce a document conforming to the Shipments_Current_Index mapping
2. WHEN the Adapter_Transformer receives a Dinee rider event payload, THE Adapter_Transformer SHALL produce a document conforming to the Riders_Current_Index mapping
3. WHEN the Adapter_Transformer receives any Dinee event payload, THE Adapter_Transformer SHALL produce an append document conforming to the Shipment_Events_Index mapping
4. IF the Adapter_Transformer encounters a field that cannot be mapped, THEN THE Adapter_Transformer SHALL log a warning with the field name and event_id and omit the field from the output document
5. THE Adapter_Transformer SHALL validate all output documents against the target index schema before returning them
6. THE Adapter_Transformer SHALL enrich each output document with an `ingested_at` timestamp and the originating `request_id` for traceability
7. FOR ALL valid Dinee payloads, transforming then serializing then deserializing then comparing against the original transform output SHALL produce an equivalent document (round-trip property)
8. THE Adapter_Transformer SHALL embed the source `schema_version` from the incoming webhook payload into every output Elasticsearch document as a `source_schema_version` field
9. THE Adapter_Transformer SHALL maintain a registry of supported schema versions and their corresponding transformation logic, allowing multiple versions to be processed concurrently during migration periods
10. WHEN the Adapter_Transformer receives a payload with a known `schema_version` that has been deprecated, THE Adapter_Transformer SHALL process the payload using the legacy transformation logic and log a WARN entry indicating the deprecated version

### Requirement 3: Replay and Backfill Job

**User Story:** As an operations engineer, I want a replay job that pulls historical data from Dinee APIs to rebuild Elasticsearch state, so that the read model can be reconstructed after data loss or initial onboarding.

#### Acceptance Criteria

1. THE Replay_Service SHALL expose an API endpoint to trigger a backfill job for a specified tenant and time range
2. WHEN a backfill job is triggered, THE Replay_Service SHALL pull paginated data from the Dinee_Platform REST APIs for the specified time range
3. WHILE a backfill job is running, THE Replay_Service SHALL process records through the Adapter_Transformer using the same normalization logic as live webhook processing
4. WHEN processing backfill records, THE Replay_Service SHALL use the same idempotency checks as the Webhook_Receiver to avoid duplicating records already ingested via webhooks
5. THE Replay_Service SHALL report progress including total records, processed count, failed count, and estimated time remaining
6. IF a backfill job encounters a transient error from the Dinee_Platform API, THEN THE Replay_Service SHALL retry with exponential backoff up to 5 attempts before marking the batch as failed
7. WHEN a backfill job completes, THE Replay_Service SHALL log a summary with total processed, failed, and skipped (duplicate) counts

### Requirement 4: Poison Queue and Failed Event Retry

**User Story:** As an operations engineer, I want failed webhook events stored in a poison queue with retry capability, so that transient failures do not result in permanent data loss.

#### Acceptance Criteria

1. WHEN the Webhook_Receiver or Adapter_Transformer fails to process an event, THE Poison_Queue SHALL store the original payload with the error reason, timestamp, and retry count
2. THE Poison_Queue SHALL persist failed events in a durable store (Redis list or dedicated Elasticsearch index) that survives service restarts
3. THE Poison_Queue SHALL expose an API endpoint to list failed events with filtering by error type, time range, and retry count
4. WHEN a retry is triggered for a poison queue event, THE Poison_Queue SHALL resubmit the payload through the standard Webhook_Receiver processing pipeline
5. THE Poison_Queue SHALL enforce a maximum retry count of 5 per event, after which the event is marked as permanently failed
6. WHEN an event exceeds the maximum retry count, THE Poison_Queue SHALL emit an alert log entry with severity ERROR including the event_id and failure reason
7. THE Poison_Queue SHALL expose an API endpoint to manually purge or acknowledge permanently failed events

### Requirement 5: Elasticsearch Index Creation and Strict Mappings

**User Story:** As a data engineer, I want Elasticsearch indices with strict mappings for shipments, riders, and events, so that data integrity is enforced at the storage layer and queries perform predictably.

#### Acceptance Criteria

1. THE ElasticsearchService SHALL create the `shipments_current` index with strict mapping including keyword fields for shipment_id, status, tenant_id, and rider_id; date fields for created_at, updated_at, and estimated_delivery; and geo_point field for current_location
2. THE ElasticsearchService SHALL create the `shipment_events` index with strict mapping including keyword fields for event_id, shipment_id, event_type, and tenant_id; date field for event_timestamp; and a nested object for event_payload
3. THE ElasticsearchService SHALL create the `riders_current` index with strict mapping including keyword fields for rider_id, status, tenant_id, and availability; date field for last_seen; and geo_point field for current_location
4. WHEN a document with an unmapped field is indexed, THE ElasticsearchService SHALL reject the document due to strict mapping enforcement
5. THE ElasticsearchService SHALL configure the `shipments_current` index with 1 primary shard and 1 replica for production environments
6. THE ElasticsearchService SHALL configure the `shipment_events` index with time-based index naming (monthly rollover) for efficient lifecycle management

### Requirement 6: Upsert Logic for Current-State Indices with Out-of-Order Event Reconciliation

**User Story:** As a data engineer, I want upsert logic that maintains the latest state in current-state indices with deterministic handling of out-of-order events, so that queries always reflect the most recent data without duplicates and stale events do not overwrite newer state.

#### Acceptance Criteria

1. WHEN a shipment event is processed, THE ElasticsearchService SHALL upsert the `shipments_current` index using the shipment_id as the document ID
2. WHEN a rider event is processed, THE ElasticsearchService SHALL upsert the `riders_current` index using the rider_id as the document ID
3. WHEN any event is processed, THE ElasticsearchService SHALL append a new document to the `shipment_events` index using the event_id as the document ID
4. WHEN upserting a document in `shipments_current`, THE ElasticsearchService SHALL update only the fields present in the incoming event while preserving existing fields
5. IF an upsert operation fails, THEN THE ElasticsearchService SHALL route the failed event to the Poison_Queue with the error details
6. THE ElasticsearchService SHALL process upserts using bulk API operations when handling batch ingestion for throughput optimization
7. WHEN upserting a current-state document, THE ElasticsearchService SHALL compare the incoming event's `event_timestamp` against the existing document's `last_event_timestamp` and discard the upsert if the incoming event is older (last-write-wins based on event timestamp)
8. WHEN a stale event is discarded due to out-of-order arrival, THE ElasticsearchService SHALL log the discarded event at INFO level with the event_id, entity_id, incoming timestamp, and existing timestamp
9. THE ElasticsearchService SHALL always append out-of-order events to the `shipment_events` append-only index regardless of whether the current-state upsert is discarded, preserving the complete event history

### Requirement 7: Index Lifecycle and Retention Policy

**User Story:** As a platform engineer, I want index lifecycle policies for event history data, so that storage costs are managed and old data is archived or deleted according to retention rules.

#### Acceptance Criteria

1. THE ElasticsearchService SHALL apply an ILM policy to the `shipment_events` index that transitions data to warm tier after 30 days
2. THE ElasticsearchService SHALL apply an ILM policy to the `shipment_events` index that transitions data to cold tier after 90 days
3. THE ElasticsearchService SHALL apply an ILM policy to the `shipment_events` index that deletes data after 365 days
4. THE ElasticsearchService SHALL apply an ILM policy to `shipments_current` and `riders_current` indices that maintains a single active index with force-merge after 7 days of no writes
5. WHEN the Backend_Service starts, THE ElasticsearchService SHALL verify that ILM policies are applied to all ops intelligence indices and log warnings for any missing policies

### Requirement 8: Normalized Read Endpoints for Shipment Board and Analytics

**User Story:** As a frontend developer, I want normalized REST endpoints for shipment and rider data, so that the operations dashboard can display live status boards and analytics without coupling to Elasticsearch query syntax.

#### Acceptance Criteria

1. THE Ops_API SHALL expose a GET `/ops/shipments` endpoint that returns paginated shipment records from the Shipments_Current_Index
2. THE Ops_API SHALL expose a GET `/ops/shipments/{shipment_id}` endpoint that returns a single shipment with its full event history from the Shipment_Events_Index
3. THE Ops_API SHALL expose a GET `/ops/riders` endpoint that returns paginated rider records from the Riders_Current_Index
4. THE Ops_API SHALL expose a GET `/ops/riders/{rider_id}` endpoint that returns a single rider with assigned shipment details
5. THE Ops_API SHALL expose a GET `/ops/events` endpoint that returns paginated event records from the Shipment_Events_Index with filtering by shipment_id, event_type, and time range
6. WHEN any Ops_API endpoint is called, THE Ops_API SHALL return responses in a consistent JSON envelope containing `data`, `pagination`, and `request_id` fields

### Requirement 9: Tenant-Scoped Query Guards with Verified Tenant Identity

**User Story:** As a security engineer, I want every ops API query automatically scoped to the requesting tenant with tenant identity derived from a cryptographically verified source, so that tenants cannot access other tenants' data and tenant assignment cannot be spoofed.

#### Acceptance Criteria

1. WHEN any Ops_API endpoint is called, THE Tenant_Guard SHALL extract the tenant_id from the authenticated request context
2. THE Tenant_Guard SHALL inject a tenant_id filter into every Elasticsearch query executed by the Ops_API before the query is sent to Elasticsearch
3. IF a request does not contain a valid tenant_id, THEN THE Tenant_Guard SHALL reject the request with a 403 status
4. THE Tenant_Guard SHALL apply tenant scoping to all read endpoints including shipments, riders, events, and aggregated metrics
5. THE Tenant_Guard SHALL log all tenant scope enforcement actions at DEBUG level including the tenant_id and endpoint path for audit purposes
6. FOR Ops_API requests, THE Tenant_Guard SHALL derive the tenant_id exclusively from the signed JWT `tenant_id` claim in the authentication token, and SHALL reject any request where the JWT claim is missing or invalid
7. FOR webhook ingestion requests, THE Webhook_Receiver SHALL derive the tenant_id from the HMAC-verified payload body's `tenant_id` field, and SHALL reject any request where the tenant_id in the payload does not match the tenant associated with the webhook signing secret
8. THE Tenant_Guard SHALL ignore any tenant_id provided in query parameters, request headers (other than the JWT), or unsigned payload fields to prevent tenant spoofing

### Requirement 10: Filtered APIs for Operational Queries

**User Story:** As an operations manager, I want filtered API endpoints for status, SLA breach, rider utilization, and failure queries, so that I can quickly find shipments and riders matching specific operational criteria.

#### Acceptance Criteria

1. THE Ops_API SHALL support filtering the `/ops/shipments` endpoint by status (pending, in_transit, delivered, failed, returned) via query parameter
2. THE Ops_API SHALL expose a GET `/ops/shipments/sla-breaches` endpoint that returns shipments where the current time exceeds the estimated_delivery time
3. THE Ops_API SHALL expose a GET `/ops/riders/utilization` endpoint that returns rider records with calculated utilization metrics (active shipments count, completed today, idle time)
4. THE Ops_API SHALL expose a GET `/ops/shipments/failures` endpoint that returns failed shipments with the failure reason extracted from the latest event
5. THE Ops_API SHALL support combining multiple filters on the `/ops/shipments` endpoint (status AND time range AND rider_id) via query parameters
6. WHEN a filter parameter contains an invalid value, THE Ops_API SHALL return a 400 status with a descriptive validation error

### Requirement 11: Aggregated Metrics Endpoints

**User Story:** As an operations analyst, I want aggregated metrics endpoints with hourly and daily buckets, so that I can analyze operational trends and performance over time.

#### Acceptance Criteria

1. THE Ops_API SHALL expose a GET `/ops/metrics/shipments` endpoint that returns shipment counts aggregated by status in configurable time buckets (hourly or daily)
2. THE Ops_API SHALL expose a GET `/ops/metrics/sla` endpoint that returns SLA compliance percentage and breach counts in configurable time buckets
3. THE Ops_API SHALL expose a GET `/ops/metrics/riders` endpoint that returns rider utilization and availability metrics aggregated by time bucket
4. THE Ops_API SHALL expose a GET `/ops/metrics/failures` endpoint that returns failure counts grouped by failure reason in configurable time buckets
5. WHEN a metrics endpoint is called with a time range exceeding 90 days, THE Ops_API SHALL enforce daily bucket granularity to limit response size
6. THE Ops_API SHALL support a `bucket` query parameter accepting values `hourly` or `daily` with a default of `hourly`

### Requirement 12: Shipment Operations Dashboard

**User Story:** As an operations manager, I want a live shipment status board in the frontend, so that I can monitor all active shipments in real time and take action on exceptions.

#### Acceptance Criteria

1. THE Ops_Dashboard SHALL display a shipment status board showing all shipments with columns for shipment_id, status, rider, origin, destination, estimated delivery, and last update time
2. THE Ops_Dashboard SHALL color-code shipment rows by status (green for delivered, yellow for in_transit, red for failed, orange for SLA breach)
3. THE Ops_Dashboard SHALL support filtering the shipment board by status, rider, and date range using filter controls
4. THE Ops_Dashboard SHALL support sorting the shipment board by any column in ascending or descending order
5. THE Ops_Dashboard SHALL display a summary bar showing counts of shipments by status (total, in_transit, delivered, failed, SLA breached)
6. WHEN a shipment status changes, THE Ops_Dashboard SHALL update the affected row within 5 seconds via WebSocket push without requiring a page refresh

### Requirement 13: Rider Utilization and Availability View

**User Story:** As a fleet coordinator, I want a rider utilization and availability view, so that I can see which riders are active, idle, or overloaded and make assignment decisions.

#### Acceptance Criteria

1. THE Ops_Dashboard SHALL display a rider list view showing rider_id, name, status (active, idle, offline), current shipment count, completed today count, and last seen time
2. THE Ops_Dashboard SHALL display a utilization bar for each rider showing the ratio of active shipments to a configurable capacity threshold
3. THE Ops_Dashboard SHALL highlight riders who exceed the capacity threshold in red and riders who are idle for more than 30 minutes in yellow
4. THE Ops_Dashboard SHALL support filtering riders by status (active, idle, offline) and sorting by utilization percentage
5. WHEN a rider's status or shipment assignment changes, THE Ops_Dashboard SHALL update the rider row within 5 seconds via WebSocket push

### Requirement 14: Failure Reason Analytics Page

**User Story:** As an operations analyst, I want a failure analytics page showing failure reasons and trends, so that I can identify systemic issues and take corrective action.

#### Acceptance Criteria

1. THE Ops_Dashboard SHALL display a failure analytics page with a bar chart showing failure counts grouped by failure reason for a selected time range
2. THE Ops_Dashboard SHALL display a trend line chart showing failure counts over time in hourly or daily buckets
3. THE Ops_Dashboard SHALL display a table of recent failed shipments with shipment_id, failure reason, rider, and failure timestamp
4. THE Ops_Dashboard SHALL support selecting a time range (today, last 7 days, last 30 days, custom range) for all failure analytics views
5. WHEN the user clicks a failure reason in the bar chart, THE Ops_Dashboard SHALL filter the failed shipments table to show only shipments with that failure reason

### Requirement 15: Shipment Tracking Monitor

**User Story:** As an operations team member, I want an internal shipment tracking monitor showing the event timeline for a specific shipment, so that I can trace the full lifecycle of a shipment for troubleshooting.

#### Acceptance Criteria

1. THE Ops_Dashboard SHALL display a shipment tracking page accessible by shipment_id that shows the full event timeline from the Shipment_Events_Index
2. THE Ops_Dashboard SHALL render each event in the timeline with event_type, timestamp, location (if available), and event details
3. THE Ops_Dashboard SHALL display the current shipment status, assigned rider, origin, destination, and estimated delivery at the top of the tracking page
4. THE Ops_Dashboard SHALL display a map view showing the shipment route with event locations plotted as markers when geo_point data is available
5. WHEN a new event is received for the displayed shipment, THE Ops_Dashboard SHALL append the event to the timeline within 5 seconds via WebSocket push

### Requirement 16: WebSocket Live Updates for Shipment and Rider Changes

**User Story:** As a frontend developer, I want WebSocket channels for shipment and rider state changes, so that the operations dashboard updates in real time without polling.

#### Acceptance Criteria

1. THE Backend_Service SHALL expose a WebSocket endpoint at `/ws/ops` that streams shipment and rider state change events to connected clients
2. WHEN a shipment document is upserted in the Shipments_Current_Index, THE Backend_Service SHALL broadcast the updated shipment data to all connected WebSocket clients subscribed to shipment updates
3. WHEN a rider document is upserted in the Riders_Current_Index, THE Backend_Service SHALL broadcast the updated rider data to all connected WebSocket clients subscribed to rider updates
4. THE WebSocket endpoint SHALL support subscription filters allowing clients to subscribe to specific event types (shipment_update, rider_update, sla_breach)
5. WHEN the WebSocket connection drops, THE Ops_Dashboard SHALL automatically reconnect with exponential backoff starting at 1 second with a maximum interval of 30 seconds
6. THE WebSocket endpoint SHALL send heartbeat messages every 30 seconds to keep connections alive and detect stale clients

### Requirement 17: AI Tools for Querying Shipment, Rider, and Event Indices

**User Story:** As an operations manager, I want the AI assistant to query shipment, rider, and event data on my behalf, so that I can ask natural language questions about operational status and get accurate answers.

#### Acceptance Criteria

1. THE AI_Ops_Tools SHALL include a `search_shipments` tool that queries the Shipments_Current_Index with support for status, rider, time range, and free-text filters
2. THE AI_Ops_Tools SHALL include a `search_riders` tool that queries the Riders_Current_Index with support for status, availability, and utilization filters
3. THE AI_Ops_Tools SHALL include a `get_shipment_events` tool that queries the Shipment_Events_Index for a specific shipment_id and returns the event timeline
4. THE AI_Ops_Tools SHALL include a `get_ops_metrics` tool that queries aggregated metrics endpoints and returns summary statistics for a specified time range
5. WHEN an AI tool is invoked, THE AI_Ops_Tools SHALL enforce the same Tenant_Guard scoping as the Ops_API to prevent cross-tenant data access
6. THE AI_Ops_Tools SHALL return results in a structured format that the AI_Agent can interpret and present in natural language

### Requirement 18: AI Report Templates

**User Story:** As an operations manager, I want the AI assistant to generate predefined operational reports, so that I can get standardized SLA violation, failure root cause, and rider productivity analyses on demand.

#### Acceptance Criteria

1. THE AI_Ops_Tools SHALL include a `generate_sla_report` tool that produces a report listing SLA violations with shipment details, breach duration, and affected tenants for a specified time range
2. THE AI_Ops_Tools SHALL include a `generate_failure_report` tool that produces a report grouping failures by root cause with counts, affected shipments, and trend indicators
3. THE AI_Ops_Tools SHALL include a `generate_rider_productivity_report` tool that produces a report showing per-rider metrics including deliveries completed, average delivery time, failure rate, and utilization percentage
4. WHEN a report is generated, THE AI_Ops_Tools SHALL include the report generation timestamp, time range covered, and tenant scope in the report header
5. THE AI_Ops_Tools SHALL format report outputs in a structured markdown format that the AI_Agent can present directly to the user

### Requirement 19: AI Guardrails for Read-Only Access

**User Story:** As a security engineer, I want the AI assistant restricted to read-only operations by default with explicit action endpoints required for mutations, so that the AI cannot accidentally modify operational data.

#### Acceptance Criteria

1. THE AI_Ops_Tools SHALL operate in read-only mode by default, with all tool functions limited to querying and reporting
2. THE AI_Ops_Tools SHALL not expose any tool that directly modifies shipment, rider, or event data in Elasticsearch
3. WHEN the AI_Agent identifies an action suggestion (e.g., reassign rider, mark shipment as failed), THE AI_Agent SHALL present the suggestion to the user without executing it
4. IF an explicit action endpoint is called by the user through the UI, THEN THE Backend_Service SHALL execute the action through a separate authenticated mutation API, not through the AI tool pipeline
5. THE AI_Ops_Tools SHALL log all tool invocations including the tool name, parameters, tenant_id, and user_id for audit purposes

### Requirement 20: End-to-End Request Tracing

**User Story:** As an SRE, I want a request_id propagated from webhook ingestion through Elasticsearch indexing to API responses and UI, so that I can trace any data point back to its origin for debugging.

#### Acceptance Criteria

1. WHEN the Webhook_Receiver receives an event, THE Webhook_Receiver SHALL generate a unique request_id and attach it to all downstream processing for that event
2. THE Adapter_Transformer SHALL include the request_id in every Elasticsearch document it produces as a `trace_id` field
3. WHEN the Ops_API serves a request, THE Ops_API SHALL generate a request_id (or use the incoming X-Request-ID header) and include it in the response headers and response body
4. THE Backend_Service SHALL include the request_id in all log entries related to a specific request or event processing chain
5. THE Ops_Dashboard SHALL display the trace_id for each shipment event in the tracking monitor timeline view
6. WHEN an error occurs during event processing, THE Backend_Service SHALL include the request_id in the error log and in any Poison_Queue entry for correlation

### Requirement 21: Rate Limiting and Auth Hardening for Ingestion and APIs

**User Story:** As a security engineer, I want rate limiting and authentication hardening on ingestion and API endpoints, so that the system is protected against abuse and unauthorized access.

#### Acceptance Criteria

1. THE Backend_Service SHALL enforce rate limiting of 500 requests per minute on the `/webhooks/dinee` endpoint per source IP
2. THE Backend_Service SHALL enforce rate limiting of 100 requests per minute per authenticated user on all Ops_API endpoints
3. THE Backend_Service SHALL enforce rate limiting of 20 requests per minute per authenticated user on aggregated metrics endpoints to protect against expensive query abuse
4. THE Webhook_Receiver SHALL require a valid API key or HMAC signature on every request in addition to the webhook signature verification
5. THE Ops_API SHALL require a valid authentication token (JWT or session token) on every request
6. IF a client exceeds the rate limit, THEN THE Backend_Service SHALL return a 429 status with a Retry-After header indicating when the client can retry

### Requirement 22: PII Masking for Public-Facing Outputs

**User Story:** As a compliance officer, I want personally identifiable information masked in API responses and AI outputs, so that customer data is protected in operational views.

#### Acceptance Criteria

1. THE PII_Masker SHALL redact customer phone numbers, email addresses, and full names from all Ops_API responses by default
2. THE PII_Masker SHALL redact PII fields from AI_Ops_Tools output before the AI_Agent presents results to the user
3. THE PII_Masker SHALL replace redacted values with masked placeholders (e.g., `***@***.com` for emails, `+XX-XXXX-XX34` for phone numbers retaining last 2 digits)
4. WHERE a user has an elevated `pii_access` permission, THE PII_Masker SHALL allow unmasked PII in API responses for that user
5. THE PII_Masker SHALL log all PII access events including the user_id, tenant_id, and fields accessed for compliance audit

### Requirement 23: Monitoring Dashboards for Ingestion and Indexing Health

**User Story:** As an SRE, I want monitoring dashboards showing ingestion lag, failed transforms, and Elasticsearch indexing errors, so that I can detect and respond to data pipeline issues before they impact operations.

#### Acceptance Criteria

1. THE Backend_Service SHALL expose a GET `/ops/monitoring/ingestion` endpoint that returns metrics including events received, events processed, events failed, and average processing latency over a configurable time window
2. THE Backend_Service SHALL expose a GET `/ops/monitoring/indexing` endpoint that returns metrics including documents indexed, indexing errors, bulk operation success rate, and average indexing latency
3. THE Backend_Service SHALL expose a GET `/ops/monitoring/poison-queue` endpoint that returns the current poison queue depth, oldest event age, and retry statistics
4. THE Backend_Service SHALL emit structured log entries at WARN level when ingestion processing latency exceeds 5 seconds for a single event
5. THE Backend_Service SHALL emit structured log entries at ERROR level when the poison queue depth exceeds 100 events
6. THE Backend_Service SHALL record Prometheus-compatible metrics for ingestion throughput, transform errors, and indexing latency that can be scraped by external monitoring systems

### Requirement 24: End-to-End Contract Tests Against Dinee Webhook Samples

**User Story:** As a QA engineer, I want contract tests that validate the full ingestion pipeline against real Dinee webhook payload samples, so that I can verify compatibility whenever the Dinee payload format changes.

#### Acceptance Criteria

1. THE test suite SHALL include contract tests that send sample Dinee webhook payloads through the Webhook_Receiver and verify the resulting Elasticsearch documents match expected schemas
2. THE contract tests SHALL cover all known Dinee event types including shipment_created, shipment_updated, shipment_delivered, shipment_failed, rider_assigned, and rider_status_changed
3. THE contract tests SHALL verify that the Adapter_Transformer produces valid documents for each event type that pass strict mapping validation
4. THE contract tests SHALL verify that signature verification accepts valid signatures and rejects invalid signatures
5. THE contract tests SHALL verify that idempotency handling correctly deduplicates repeated event deliveries
6. WHEN a contract test fails, THE test framework SHALL report the specific field or validation that failed with the expected and actual values

### Requirement 25: Drift Detection Between Dinee State and Runsheet Read Model

**User Story:** As a data engineer, I want drift detection tests that compare Dinee source state against the Runsheet read model, so that I can detect and remediate data divergence before it impacts operations.

#### Acceptance Criteria

1. THE Drift_Detector SHALL compare shipment counts and statuses between the Dinee_Platform API and the Shipments_Current_Index for a specified tenant and time range
2. THE Drift_Detector SHALL compare rider statuses between the Dinee_Platform API and the Riders_Current_Index for a specified tenant
3. WHEN the Drift_Detector finds a discrepancy, THE Drift_Detector SHALL log the divergent records with shipment_id or rider_id, expected state, and actual state
4. THE Drift_Detector SHALL expose an API endpoint to trigger a drift detection run and return the results
5. THE Drift_Detector SHALL support scheduled drift detection runs at a configurable interval defaulting to every 6 hours
6. WHEN drift is detected exceeding a configurable threshold (default 1% of records), THE Drift_Detector SHALL emit an alert log entry with severity WARN

### Requirement 26: Load Testing for Ingestion Throughput and Dashboard Query Latency

**User Story:** As a performance engineer, I want load tests for the ingestion pipeline and dashboard query endpoints, so that I can verify the system handles production-scale traffic without degradation.

#### Acceptance Criteria

1. THE load test suite SHALL simulate sustained ingestion of 100 webhook events per second for 10 minutes and measure end-to-end processing latency
2. THE load test suite SHALL measure p50, p95, and p99 latencies for the `/ops/shipments` endpoint under 50 concurrent users
3. THE load test suite SHALL measure p50, p95, and p99 latencies for aggregated metrics endpoints under 20 concurrent users
4. THE load test suite SHALL verify that ingestion processing latency remains under 2 seconds at p95 during sustained load
5. THE load test suite SHALL verify that dashboard query latency remains under 500 milliseconds at p95 during sustained load
6. WHEN load tests complete, THE test framework SHALL generate a report with latency distributions, throughput metrics, error rates, and resource utilization

### Requirement 27: Staged Tenant Rollout with Feature Flag and Rollback

**User Story:** As a product manager, I want the Ops Intelligence Layer rolled out per-tenant with feature flags and rollback capability, so that I can control the blast radius and revert quickly if issues arise.

#### Acceptance Criteria

1. THE Feature_Flag_Service SHALL support enabling or disabling the Ops Intelligence Layer per tenant_id via a configuration endpoint
2. WHEN the Ops Intelligence Layer is disabled for a tenant, THE Webhook_Receiver SHALL accept but skip processing events for that tenant and return a 200 status
3. WHEN the Ops Intelligence Layer is disabled for a tenant, THE Ops_API SHALL return a 404 status for all ops endpoints for that tenant
4. WHEN the Ops Intelligence Layer is disabled for a tenant, THE Ops_Dashboard SHALL hide the ops navigation items for users of that tenant
5. THE Feature_Flag_Service SHALL support a rollback operation that disables the Ops Intelligence Layer for a tenant and optionally purges that tenant's data from the ops indices
6. THE Feature_Flag_Service SHALL log all feature flag changes including the tenant_id, action (enable/disable/rollback), and the user who performed the change
