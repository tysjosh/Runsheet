# Implementation Plan: Ops Intelligence Layer

## Overview

This implementation plan builds the Ops Intelligence Layer on top of the existing production-readiness foundation. Tasks are organized in dependency order: configuration extensions first, then Elasticsearch indices and core ingestion pipeline, followed by the API layer with tenant scoping, then AI tools, frontend dashboard, and finally infrastructure services (PII, feature flags, drift detection) and testing.

## Tasks

- [x] 1. Extend configuration and add new error codes
  - [x] 1.1 Add ops-specific settings to config/settings.py
    - Add `dinee_webhook_secret`, `dinee_idempotency_ttl_hours`, `dinee_api_base_url`, `dinee_api_key`
    - Add `ops_webhook_rate_limit` (default 500), `ops_api_rate_limit` (default 100), `ops_metrics_rate_limit` (default 20)
    - Add validators for new fields
    - Update `.env.development` and `.env.example` with new variables
    - _Requirements: 1.2, 21.1-21.3_

  - [x] 1.2 Add ops error codes to errors/codes.py
    - Add `WEBHOOK_SIGNATURE_INVALID`, `WEBHOOK_SCHEMA_UNKNOWN`, `TENANT_NOT_FOUND`, `TENANT_DISABLED`
    - Add `POISON_QUEUE_MAX_RETRIES`, `DRIFT_THRESHOLD_EXCEEDED`, `BACKFILL_IN_PROGRESS`
    - Add corresponding factory functions to errors/exceptions.py
    - Update `ERROR_CODE_STATUS_MAP` with new codes
    - _Requirements: 1.3, 4.6, 9.3_

  - [x] 1.3 Create ops module package structure
    - Create `Runsheet-backend/ops/` package with `__init__.py`
    - Create subpackages: `ops/ingestion/`, `ops/api/`, `ops/middleware/`, `ops/services/`, `ops/websocket/`, `ops/webhooks/`
    - Create `ops/models.py` with shared Pydantic models (PaginatedResponse, PaginationMeta, ShipmentDetail, RiderDetail, MetricsBucket, MetricsResponse)
    - _Requirements: 8.6_

- [x] 2. Create Elasticsearch ops indices with strict mappings
  - [x] 2.1 Create OpsElasticsearchService class
    - Create `ops/services/ops_es_service.py` with OpsElasticsearchService
    - Delegate to existing `elasticsearch_service` for connection and circuit breaker
    - Implement `setup_ops_indices()` to create all ops indices
    - _Requirements: 5.1-5.6_

  - [x] 2.2 Implement shipments_current index mapping
    - Strict mapping with keyword fields: shipment_id, status, tenant_id, rider_id, failure_reason, source_schema_version, trace_id
    - Date fields: created_at, updated_at, estimated_delivery, last_event_timestamp, ingested_at
    - Geo_point field: current_location
    - Text+keyword fields: origin, destination
    - Configure 1 primary shard, 1 replica
    - _Requirements: 5.1, 5.5_

  - [x] 2.3 Implement shipment_events index mapping
    - Strict mapping with keyword fields: event_id, shipment_id, event_type, tenant_id, source_schema_version, trace_id
    - Date fields: event_timestamp, ingested_at
    - Nested object: event_payload
    - Geo_point field: location
    - Set up index alias with time-based naming for monthly rollover
    - _Requirements: 5.2, 5.6_

  - [x] 2.4 Implement riders_current index mapping
    - Strict mapping with keyword fields: rider_id, status, tenant_id, availability, source_schema_version, trace_id
    - Date fields: last_seen, last_event_timestamp, ingested_at
    - Geo_point field: current_location
    - Integer fields: active_shipment_count, completed_today
    - Text+keyword field: rider_name
    - _Requirements: 5.3_

  - [x] 2.5 Implement ops_poison_queue index mapping
    - Keyword fields: event_id, error_type, status, tenant_id
    - Object field (enabled: false): original_payload
    - Text+keyword field: error_reason
    - Date field: created_at
    - Integer fields: retry_count, max_retries
    - _Requirements: 4.2_

  - [x] 2.6 Implement scripted upsert for current-state indices
    - Create painless script that compares incoming event_timestamp vs existing last_event_timestamp
    - Discard upsert (ctx.op = 'noop') if incoming is older
    - Partial update: only update fields present in incoming event
    - Log discarded stale events at INFO level with event_id, entity_id, timestamps
    - _Requirements: 6.1, 6.2, 6.4, 6.7, 6.8_

  - [x] 2.7 Implement append logic for shipment_events
    - Always append to shipment_events regardless of ordering
    - Use event_id as document ID
    - _Requirements: 6.3, 6.9_

  - [x] 2.8 Implement bulk upsert for batch ingestion
    - Use Elasticsearch bulk API for throughput optimization
    - Route failures to poison queue
    - _Requirements: 6.5, 6.6_

  - [x] 2.9 Set up ILM policies for ops indices
    - shipment_events: warm@30d, cold@90d, delete@365d
    - shipments_current/riders_current: force-merge after 7d no writes
    - Verify ILM policies on startup, log warnings for missing policies
    - _Requirements: 7.1-7.5_

  - [x] 2.10 Integrate ops index setup into application startup
    - Call `setup_ops_indices()` and `setup_ops_ilm_policies()` during lifespan startup in main.py
    - Validate ops index schemas on startup
    - _Requirements: 7.5_

- [ ] 3. Checkpoint - Verify Elasticsearch indices
  - Definition of Done (all must pass):
    - All 4 ops indices (shipments_current, shipment_events, riders_current, ops_poison_queue) created on startup with strict mappings
    - Upsert script correctly discards stale events (verified with out-of-order test: send event t=2, then t=1; current state reflects t=2)
    - ILM policies attached and verified via `GET _ilm/policy/ops_*`
    - No ERROR-level log entries during index creation
  - Ask the user if questions arise

- [x] 4. Implement ingestion pipeline core
  - [x] 4.1 Create IdempotencyService
    - Create `ops/ingestion/idempotency.py`
    - Implement Redis-backed `is_duplicate(event_id)` and `mark_processed(event_id)`
    - Use `idemp:` key prefix with configurable TTL (default 72 hours)
    - Reuse existing Redis connection pattern from RedisSessionStore
    - _Requirements: 1.4, 1.5, 1.7_

  - [x] 4.2 Create Adapter Transformer with schema version registry
    - Create `ops/ingestion/adapter.py` with AdapterTransformer class
    - Define SchemaHandler ABC and TransformResult dataclass
    - Implement handler registry with `register_handler(version, handler, deprecated)`
    - Implement `transform()` that selects handler by schema_version
    - Enrich output documents with `ingested_at`, `trace_id` (from request_id), `source_schema_version`
    - Validate output documents against target index schema
    - Log warnings for unmappable fields and deprecated versions
    - _Requirements: 2.1-2.10_

  - [x] 4.3 Implement v1.0 schema handler
    - Create `ops/ingestion/handlers/v1_0.py` with V1SchemaHandler
    - Map Dinee shipment events to shipments_current document format
    - Map Dinee rider events to riders_current document format
    - Map all events to shipment_events append format
    - _Requirements: 2.1-2.3_

  - [x] 4.4 Create PoisonQueueService
    - Create `ops/ingestion/poison_queue.py`
    - Implement `store_failed_event()` with error reason, timestamp, retry count
    - Implement `list_failed_events()` with filtering by error_type, time_range, retry_count
    - Implement `retry_event()` that resubmits through standard pipeline
    - Implement `purge_event()` for permanently failed events
    - Enforce max retry count of 5, emit ERROR log when exceeded
    - _Requirements: 4.1-4.7_

  - [x] 4.5 Create Webhook Receiver endpoint
    - Create `ops/webhooks/receiver.py` with FastAPI router
    - Canonical webhook auth policy: HMAC-SHA256 only (no separate API key for webhook verification; `dinee_api_key` is used exclusively for outbound Dinee REST API calls in the Replay Service)
    - Implement HMAC-SHA256 signature verification against `dinee_webhook_secret`
    - Reject invalid signatures with 401 and log rejection with request_id and source IP
    - Validate `schema_version` field conforms to semver format
    - Route unknown schema versions to poison queue, return 200
    - Check event_id idempotency via IdempotencyService
    - Return 200 for duplicates without reprocessing
    - Pass valid payloads to AdapterTransformer
    - Store processed event_ids with configurable TTL
    - Return 200 with event_id on success
    - _Requirements: 1.1-1.11_

  - [x] 4.6 Wire webhook receiver into main.py
    - Import and include webhook router in FastAPI app
    - Apply rate limiting (500 req/min per IP) to webhook endpoint
    - Ensure RequestIDMiddleware generates trace_id for webhook requests
    - _Requirements: 1.1, 21.1, 21.4, 20.1_

- [ ] 5. Checkpoint - Verify ingestion pipeline
  - Definition of Done (all must pass):
    - Webhook with valid HMAC returns 200 and document appears in ES within 2s
    - Webhook with invalid HMAC returns 401 (zero false accepts in 50 test requests)
    - Duplicate event_id returns 200 without creating a second ES document
    - Unknown schema_version routes to poison queue and returns 200
    - Poison queue retry resubmits through full pipeline successfully
    - Adapter produces documents matching strict index mappings (no mapping exceptions)
  - Ask the user if questions arise

- [x] 6. Implement Tenant Guard and Ops API endpoints
  - [x] 6.1 Create Tenant Guard dependency
    - Create `ops/middleware/tenant_guard.py`
    - Implement `get_tenant_context()` FastAPI dependency
    - Extract tenant_id exclusively from signed JWT `tenant_id` claim
    - Reject requests with missing/invalid JWT claim with 403
    - Ignore tenant_id from query params, headers (other than JWT), or unsigned payload fields
    - Extract `has_pii_access` permission from JWT claims
    - Log tenant scope enforcement at DEBUG level
    - _Requirements: 9.1-9.8_

  - [x] 6.2 Implement `inject_tenant_filter()` utility
    - Wrap any ES query with a bool filter on tenant_id
    - Apply to all read endpoints including shipments, riders, events, and metrics
    - _Requirements: 9.2, 9.4_

  - [x] 6.3 Create Ops API read endpoints
    - Create `ops/api/endpoints.py` with FastAPI router at `/ops`
    - `GET /ops/shipments` - paginated shipments with status, rider_id, date range filters and sorting
    - `GET /ops/shipments/{shipment_id}` - single shipment with full event history
    - `GET /ops/riders` - paginated riders
    - `GET /ops/riders/{rider_id}` - single rider with assigned shipment details
    - `GET /ops/events` - paginated events with shipment_id, event_type, time range filters
    - All responses use consistent JSON envelope: `{data, pagination, request_id}`
    - _Requirements: 8.1-8.6_

  - [x] 6.4 Create filtered operational query endpoints
    - `GET /ops/shipments/sla-breaches` - shipments where current time > estimated_delivery
    - `GET /ops/riders/utilization` - riders with calculated utilization metrics
    - `GET /ops/shipments/failures` - failed shipments with failure reason from latest event
    - Support combining multiple filters via query parameters
    - Return 400 for invalid filter values
    - _Requirements: 10.1-10.6_

  - [x] 6.5 Create aggregated metrics endpoints
    - `GET /ops/metrics/shipments` - shipment counts by status in hourly/daily buckets
    - `GET /ops/metrics/sla` - SLA compliance percentage and breach counts
    - `GET /ops/metrics/riders` - rider utilization and availability metrics
    - `GET /ops/metrics/failures` - failure counts by reason
    - Support `bucket` query param (hourly/daily, default hourly)
    - Enforce daily granularity for time ranges > 90 days
    - _Requirements: 11.1-11.6_

  - [x] 6.6 Create monitoring endpoints
    - `GET /ops/monitoring/ingestion` - events received/processed/failed, avg latency
    - `GET /ops/monitoring/indexing` - documents indexed, errors, bulk success rate, avg latency
    - `GET /ops/monitoring/poison-queue` - queue depth, oldest event age, retry stats
    - _Requirements: 23.1-23.3_

  - [x] 6.7 Wire Ops API router into main.py
    - Include ops router in FastAPI app
    - Apply rate limiting: 100 req/min per user for ops endpoints, 20 req/min for metrics
    - Require valid authentication token (JWT or session) on all ops endpoints
    - _Requirements: 21.2, 21.3, 21.5_

- [ ] 7. Checkpoint - Verify Ops API layer
  - Definition of Done (all must pass):
    - All endpoints return correct JSON envelope `{data, pagination, request_id}`
    - Tenant isolation: query with tenant_id=A returns zero documents belonging to tenant_id=B (verified with 2-tenant test dataset)
    - Filter combinations (status + date range + rider_id) return correct subsets
    - Pagination: `total_pages` matches `ceil(total / size)` for all responses
    - Rate limiting: 101st request within 1 minute returns 429 with Retry-After header
    - Invalid filter values return 400 with structured error
  - Ask the user if questions arise

- [x] 8. Implement PII Masker
  - [x] 8.1 Create PIIMasker class
    - Create `ops/middleware/pii_masker.py`
    - Implement phone masking: `+XX-XXXX-XX34` retaining last 2 digits
    - Implement email masking: `***@***.com`
    - Implement name field masking for customer_name, recipient_name, sender_name
    - _Requirements: 22.1, 22.3_

  - [x] 8.2 Integrate PII masking into Ops API responses
    - Apply role-based PII masking by endpoint audience:
      - External/customer-facing endpoints: always mask PII
      - Internal ops endpoints: mask by default, unmask if JWT contains `has_pii_access: true`
      - AI tool outputs: always mask (no PII in agent responses)
    - Check `has_pii_access` from TenantContext to allow unmasked responses on internal ops endpoints
    - Log all PII access events with user_id, tenant_id, fields accessed, endpoint
    - _Requirements: 22.2, 22.4, 22.5_

- [x] 9. Implement WebSocket for ops live updates
  - [x] 9.1 Create OpsWebSocketManager
    - Create `ops/websocket/ops_ws.py` with OpsWebSocketManager class
    - Support subscription filters: shipment_update, rider_update, sla_breach
    - Implement heartbeat every 30 seconds
    - Detect and disconnect stale clients
    - _Requirements: 16.1, 16.4, 16.6_

  - [x] 9.2 Add WebSocket endpoint and broadcast integration
    - Add `/ws/ops` WebSocket endpoint to main.py
    - Broadcast shipment updates on shipments_current upsert
    - Broadcast rider updates on riders_current upsert
    - Filter broadcasts by client subscriptions
    - _Requirements: 16.2, 16.3_

- [ ] 10. Implement Replay Service
  - [x] 10.1 Create ReplayService
    - Create `ops/ingestion/replay.py`
    - Implement `trigger_backfill()` for tenant + time range
    - Pull paginated data from Dinee REST APIs
    - Process through AdapterTransformer with same normalization logic
    - Use same idempotency checks as webhook receiver
    - Replay conflict rule: replay events pass through the same scripted upsert as live events, so older replay snapshots arriving after newer live updates are automatically discarded by the `last_event_timestamp` comparison (no special handling needed; the existing out-of-order reconciliation covers this)
    - Retry transient Dinee API errors with exponential backoff (up to 5 attempts)
    - Report progress: total, processed, failed, skipped, estimated remaining
    - Log summary on completion
    - _Requirements: 3.1-3.7_

  - [x] 10.2 Add replay API endpoints
    - `POST /ops/replay/trigger` - trigger backfill job
    - `GET /ops/replay/status/{job_id}` - get job progress
    - _Requirements: 3.1, 3.5_

- [ ] 11. Checkpoint - Verify ingestion, API, WebSocket, and replay
  - Definition of Done (all must pass):
    - End-to-end: webhook POST → document in ES → WebSocket broadcast received by subscribed client, all within 5s
    - Replay: backfill 100 historical records, verify all appear in ES with correct tenant scoping
    - Replay conflict: send live event at t=10, then replay older snapshot at t=5; current state still reflects t=10
    - PII masking: API response with `has_pii_access=false` contains zero raw phone/email values
    - PII bypass: API response with `has_pii_access=true` contains unmasked values
    - WebSocket heartbeat received within 30s of connection
  - Ask the user if questions arise

- [x] 12. Implement Feature Flag Service
  - [x] 12.1 Create FeatureFlagService
    - Create `ops/services/feature_flags.py`
    - Implement Redis-backed per-tenant enable/disable with `ops_ff:` prefix
    - Implement `is_enabled(tenant_id)`, `enable()`, `disable()`, `rollback()`
    - Rollback optionally purges tenant data from ops indices
    - Log all flag changes with tenant_id, action, user_id
    - _Requirements: 27.1-27.6_

  - [x] 12.2 Integrate feature flags into webhook receiver
    - Check feature flag before processing webhook events
    - Accept but skip processing for disabled tenants, return 200
    - _Requirements: 27.2_

  - [x] 12.3 Integrate feature flags into Ops API
    - Return 404 for all ops endpoints when tenant is disabled
    - _Requirements: 27.3_

  - [x] 12.4 Integrate feature flags into WebSocket
    - Reject new `/ws/ops` connections for disabled tenants with close code 4403 and reason "tenant_disabled"
    - Disconnect existing WebSocket clients within 30s when tenant is disabled (via flag change broadcast)
    - Exclude disabled tenant data from all WebSocket broadcasts
    - _Requirements: 27.3_

  - [x] 12.5 Integrate feature flags into AI tools
    - All ops AI tools check feature flag before executing queries
    - Return structured disabled response: `{"status": "disabled", "message": "Ops intelligence is not enabled for this tenant"}` 
    - Do not raise exceptions; return informational message so the agent can relay to user
    - _Requirements: 27.3_

  - [x] 12.6 Add feature flag management endpoints
    - `POST /ops/admin/feature-flags/{tenant_id}/enable`
    - `POST /ops/admin/feature-flags/{tenant_id}/disable`
    - `POST /ops/admin/feature-flags/{tenant_id}/rollback`
    - _Requirements: 27.1, 27.5_

- [x] 13. Implement AI Ops Tools
  - [x] 13.1 Create ops search tools
    - Create `Agents/tools/ops_search_tools.py`
    - Implement `search_shipments` tool with status, rider, time range, free-text filters
    - Implement `search_riders` tool with status, availability, utilization filters
    - Implement `get_shipment_events` tool for specific shipment event timeline
    - Implement `get_ops_metrics` tool for aggregated metrics
    - All tools enforce tenant scoping via TenantGuard
    - All tools return structured format for AI agent interpretation
    - Log all tool invocations with tool name, params, tenant_id, user_id
    - _Requirements: 17.1-17.6, 19.1-19.2, 19.5_

  - [x] 13.2 Create ops report tools
    - Create `Agents/tools/ops_report_tools.py`
    - Implement `generate_sla_report` with SLA violations, breach duration, affected tenants
    - Implement `generate_failure_report` with failures grouped by root cause, counts, trends
    - Implement `generate_rider_productivity_report` with per-rider deliveries, avg time, failure rate, utilization
    - Include report timestamp, time range, tenant scope in header
    - Format as structured markdown
    - _Requirements: 18.1-18.5_

  - [x] 13.3 Apply PII masking to AI tool outputs
    - Run PIIMasker on all AI tool results before returning to agent
    - _Requirements: 22.2_

  - [x] 13.4 Register ops tools with LogisticsAgent
    - Add ops search and report tools to the agent's tool list in mainagent.py
    - Ensure AI agent presents action suggestions without executing mutations
    - _Requirements: 19.3, 19.4_

- [x] 14. Implement Drift Detector
  - [x] 14.1 Create DriftDetector service
    - Create `ops/services/drift_detector.py`
    - Compare shipment counts/statuses between Dinee API and shipments_current for tenant + time range
    - Compare rider statuses between Dinee API and riders_current for tenant
    - Log divergent records with entity_id, expected state, actual state
    - Emit WARN alert when drift exceeds configurable threshold (default 1%)
    - _Requirements: 25.1-25.6_

  - [x] 14.2 Add drift detection endpoints
    - `POST /ops/drift/run` - trigger drift detection for tenant + time range
    - Support scheduled runs at configurable interval (default 6 hours)
    - _Requirements: 25.4, 25.5_

- [x] 15. Implement monitoring and observability
  - [x] 15.1 Add Prometheus-compatible metrics
    - Expose metrics at `/ops/metrics/prometheus` (or via prometheus_client default endpoint)
    - Exact metric names and types:
      - `ops_webhook_received_total` (Counter, labels: tenant_id, schema_version) — total webhooks received
      - `ops_webhook_processed_total` (Counter, labels: tenant_id, status=[processed|duplicate|rejected|queued]) — processing outcomes
      - `ops_ingestion_latency_seconds` (Histogram, labels: tenant_id, event_type) — webhook-to-ES-upsert latency
      - `ops_transform_errors_total` (Counter, labels: tenant_id, error_type) — adapter transform failures
      - `ops_es_indexing_latency_seconds` (Histogram, labels: index_name) — ES indexing latency
      - `ops_es_indexing_errors_total` (Counter, labels: index_name, error_type) — ES indexing failures
      - `ops_poison_queue_depth` (Gauge, labels: tenant_id) — current poison queue size
      - `ops_poison_queue_oldest_age_seconds` (Gauge) — age of oldest unresolved poison queue entry
      - `ops_api_request_duration_seconds` (Histogram, labels: endpoint, method) — API response latency
      - `ops_ws_active_connections` (Gauge, labels: tenant_id) — active WebSocket connections
      - `ops_drift_percentage` (Gauge, labels: tenant_id) — last drift detection result
      - `ops_feature_flag_changes_total` (Counter, labels: tenant_id, action=[enable|disable|rollback]) — flag changes
    - Alert rules (log-based, upgradeable to Alertmanager):
      - WARN: `ops_ingestion_latency_seconds` p95 > 5s for 5 minutes
      - ERROR: `ops_poison_queue_depth` > 100 for any tenant
      - WARN: `ops_poison_queue_oldest_age_seconds` > 3600 (1 hour)
      - ERROR: `ops_es_indexing_errors_total` rate > 10/min for 5 minutes
      - WARN: `ops_drift_percentage` > 1% for any tenant
      - WARN: `ops_ws_active_connections` = 0 for > 10 minutes (potential connectivity issue)
    - _Requirements: 23.4-23.6_

  - [x] 15.2 Ensure end-to-end request tracing
    - Webhook receiver generates request_id, propagates as trace_id in all ES documents
    - Ops API includes request_id in response headers and body
    - All log entries include request_id for correlation
    - Poison queue entries include request_id for correlation
    - _Requirements: 20.1-20.6_

- [ ] 16. Checkpoint - Verify backend feature completeness
  - Definition of Done (all must pass):
    - Feature flag disable: webhook returns 200 but no ES document created; API returns 404; WebSocket rejects connection with 4403; AI tools return disabled response
    - Feature flag enable: all surfaces resume normal operation within 5s
    - AI tools return tenant-scoped, PII-masked results (verify with 2-tenant dataset)
    - AI tools execute zero write operations (verify via ES audit log or mock)
    - Drift detector: inject 2% divergence, verify WARN alert emitted and `ops_drift_percentage` gauge updated
    - All Prometheus metrics listed in task 15.1 are emitting values (verify via `/ops/metrics/prometheus`)
  - Ask the user if questions arise

- [x] 17. Implement frontend ops dashboard
  - [x] 17.1 Create ops API client service
    - Create `runsheet/src/services/opsApi.ts`
    - Implement functions for all ops endpoints (shipments, riders, events, metrics, failures, SLA breaches, utilization)
    - Handle pagination, filtering, and error responses
    - _Requirements: 8.1-8.6, 10.1-10.6, 11.1-11.6_

  - [x] 17.2 Create useOpsWebSocket hook
    - Create `runsheet/src/hooks/useOpsWebSocket.ts`
    - Connect to `/ws/ops` with subscription filters
    - Auto-reconnect with exponential backoff (1s initial, 30s max)
    - Parse shipment_update, rider_update, sla_breach events
    - _Requirements: 16.5_

  - [x] 17.3 Create Shipment Status Board page
    - Create `runsheet/src/app/ops/page.tsx`
    - Create `runsheet/src/components/ops/ShipmentBoard.tsx` with sortable columns: shipment_id, status, rider, origin, destination, estimated delivery, last update
    - Color-code rows: green=delivered, yellow=in_transit, red=failed, orange=SLA breach
    - Create `runsheet/src/components/ops/ShipmentSummaryBar.tsx` with status counts
    - Create `runsheet/src/components/ops/OpsFilters.tsx` for status, rider, date range filters
    - Update rows within 5 seconds via WebSocket
    - _Requirements: 12.1-12.6_

  - [x] 17.4 Create Rider Utilization View page
    - Create `runsheet/src/app/ops/riders/page.tsx`
    - Create `runsheet/src/components/ops/RiderUtilizationList.tsx`
    - Show rider_id, name, status, shipment count, completed today, last seen
    - Display utilization bar (active/capacity ratio)
    - Highlight overloaded riders in red, idle >30min in yellow
    - Filter by status, sort by utilization
    - Update via WebSocket within 5 seconds
    - _Requirements: 13.1-13.5_

  - [x] 17.5 Create Failure Analytics page
    - Create `runsheet/src/app/ops/failures/page.tsx`
    - Create `runsheet/src/components/ops/FailureBarChart.tsx` - failure counts by reason
    - Create `runsheet/src/components/ops/FailureTrendChart.tsx` - failure trend over time
    - Add table of recent failed shipments with shipment_id, reason, rider, timestamp
    - Support time range selection (today, 7d, 30d, custom)
    - Click failure reason in bar chart to filter table
    - _Requirements: 14.1-14.5_

  - [x] 17.6 Create Shipment Tracking Monitor page
    - Create `runsheet/src/app/ops/tracking/[id]/page.tsx`
    - Create `runsheet/src/components/ops/ShipmentTimeline.tsx` - event timeline with type, timestamp, location, details
    - Create `runsheet/src/components/ops/ShipmentMap.tsx` - map with event location markers
    - Display current status, rider, origin, destination, estimated delivery at top
    - Display trace_id for each event
    - Append new events via WebSocket within 5 seconds
    - _Requirements: 15.1-15.5, 20.5_

  - [x] 17.7 Add ops navigation to sidebar
    - Add ops section to existing Sidebar component
    - Conditionally show/hide based on feature flag status for tenant
    - _Requirements: 27.4_

- [ ] 18. Checkpoint - Verify frontend dashboard
  - Definition of Done (all must pass):
    - All 4 ops pages render without console errors with mock data
    - ShipmentBoard color-coding matches spec (green/yellow/red/orange)
    - WebSocket update reflected in UI within 5s of broadcast
    - Filters narrow displayed data correctly (verify with known dataset)
    - Pagination controls navigate correctly and total_pages is accurate
    - Ops sidebar nav hidden when feature flag is disabled for tenant
  - Ask the user if questions arise

- [x] 19. Implement webhook tenant verification
  - [x] 19.1 Verify tenant_id in webhook payload matches signing secret
    - Derive tenant_id from HMAC-verified payload body's tenant_id field
    - Reject requests where payload tenant_id doesn't match tenant associated with webhook signing secret
    - _Requirements: 9.7_

- [ ] 20. Write unit tests
  - [x] 20.1 Test webhook receiver
    - Test HMAC signature verification (valid, invalid, missing)
    - Test idempotency (duplicate event_id returns 200 without reprocessing)
    - Test schema_version validation (known, unknown, deprecated)
    - Test feature flag gating (disabled tenant skips processing)
    - _Requirements: 1.1-1.11, 24.4, 24.5_

  - [ ]* 20.2 Test adapter transformer
    - Test transformation for each event type (shipment_created, shipment_updated, shipment_delivered, shipment_failed, rider_assigned, rider_status_changed)
    - Test enrichment with ingested_at, trace_id, source_schema_version
    - Test unmappable field warning
    - Test deprecated version handling
    - _Requirements: 2.1-2.10, 24.2, 24.3_

  - [x] 20.3 Test tenant guard
    - Test valid JWT with tenant_id claim
    - Test missing JWT claim returns 403
    - Test spoofed query param tenant_id is ignored
    - Test pii_access permission extraction
    - _Requirements: 9.1-9.8_

  - [ ]* 20.4 Test PII masker
    - Test phone masking retains last 2 digits
    - Test email masking produces `***@***.com`
    - Test name field masking
    - Test elevated pii_access bypasses masking
    - _Requirements: 22.1-22.5_

  - [ ]* 20.5 Test poison queue
    - Test store, list, retry, purge operations
    - Test max retry count enforcement
    - Test ERROR log on max retries exceeded
    - _Requirements: 4.1-4.7_

  - [ ]* 20.6 Test feature flag service
    - Test enable, disable, rollback operations
    - Test is_enabled returns correct state
    - Test rollback with purge_data option
    - Test change logging
    - _Requirements: 27.1-27.6_

  - [ ]* 20.7 Test out-of-order upsert logic
    - Test newer event updates current state
    - Test stale event is discarded (noop)
    - Test stale event still appended to shipment_events
    - Test INFO log for discarded events
    - _Requirements: 6.7-6.9_

- [ ] 21. Write property-based tests
  - [x] 21.1 Property test: HMAC Signature Verification
    - Generate random payloads and secrets, verify accept iff HMAC matches
    - **Property 1, Validates: Requirements 1.2, 1.3**

  - [x] 21.2 Property test: Idempotent Processing
    - Generate event_ids, deliver N times, verify ES state equals single delivery
    - **Property 2, Validates: Requirements 1.4, 1.5, 1.11**

  - [ ]* 21.3 Property test: Transform Round-Trip
    - Generate valid payloads, transform → serialize → deserialize → compare
    - **Property 3, Validates: Requirement 2.7**

  - [ ]* 21.4 Property test: Out-of-Order Reconciliation
    - Generate event sequences with random ordering, verify current state reflects latest timestamp
    - **Property 5, Validates: Requirements 6.7, 6.8, 6.9**

  - [x] 21.5 Property test: Tenant Isolation
    - Generate multi-tenant queries, verify every ES query includes tenant_id filter
    - **Property 6, Validates: Requirements 9.1-9.8**

  - [ ]* 21.6 Property test: Poison Queue Retry Bound
    - Generate retry sequences, verify retry_count never exceeds 5
    - **Property 7, Validates: Requirements 4.5, 4.6**

  - [ ]* 21.7 Property test: PII Masking Completeness
    - Generate responses with phone/email/name patterns, verify all masked unless pii_access
    - **Property 8, Validates: Requirements 22.1-22.4**

  - [ ]* 21.8 Property test: AI Read-Only Invariant
    - Invoke all AI tools, verify no write operations executed
    - **Property 12, Validates: Requirements 19.1-19.2**

- [x] 22. Write contract tests
  - [x] 22.1 Create sample Dinee webhook payloads
    - Create JSON fixtures for: shipment_created, shipment_updated, shipment_delivered, shipment_failed, rider_assigned, rider_status_changed
    - _Requirements: 24.1_

  - [x] 22.2 Write end-to-end contract tests
    - Send sample payloads through webhook receiver
    - Verify resulting ES documents match expected schemas
    - Verify signature verification accepts valid / rejects invalid
    - Verify idempotency deduplicates repeated deliveries
    - _Requirements: 24.1-24.6_

- [x] 23. Write integration tests
  - [x]* 23.1 Test ingestion pipeline integration
    - Webhook → Adapter → ES upsert with test ES instance
    - Verify documents in all three indices
    - _Requirements: 24.1-24.3_

  - [x]* 23.2 Test Ops API integration
    - Test all endpoints with tenant scoping
    - Verify cross-tenant isolation
    - Test filter combinations and pagination
    - _Requirements: 8.1-8.6, 9.1-9.8_

  - [x]* 23.3 Test WebSocket integration
    - Test connection lifecycle
    - Test subscription filtering
    - Test broadcast on upsert
    - _Requirements: 16.1-16.6_

  - [x]* 23.4 Test feature flag integration
    - Test webhook gating for disabled tenant
    - Test API 404 for disabled tenant
    - Test WebSocket rejection (close code 4403) for disabled tenant
    - Test AI tools return disabled response for disabled tenant
    - Test re-enable restores all surfaces
    - _Requirements: 27.1-27.4_

- [ ] 24. Write frontend tests
  - [ ]* 24.1 Jest component tests
    - Test ShipmentBoard rendering and color-coding
    - Test RiderUtilizationList with utilization bars
    - Test FailureBarChart click-to-filter
    - Test ShipmentTimeline event rendering
    - _Requirements: 12.1-12.5, 13.1-13.4, 14.1-14.5, 15.1-15.3_

  - [ ]* 24.2 Playwright E2E tests
    - Navigate ops pages, verify data display
    - Test filter interactions
    - Test WebSocket live updates
    - _Requirements: 12.6, 13.5, 15.5_

- [ ] 25. Set up load tests
  - [ ]* 25.1 Create webhook load test
    - Simulate 100 webhooks/second sustained for 10 minutes
    - Measure end-to-end processing latency (target: p95 < 2s)
    - _Requirements: 26.1, 26.4_

  - [ ]* 25.2 Create dashboard query load test
    - `/ops/shipments` under 50 concurrent users (target: p95 < 500ms)
    - Metrics endpoints under 20 concurrent users (target: p95 < 500ms)
    - Generate report with latency distributions, throughput, error rates
    - _Requirements: 26.2, 26.3, 26.5, 26.6_

- [ ] 26. Final checkpoint - Complete ops intelligence layer verification
  - Definition of Done (all must pass, checkpoint fails otherwise):
    - All mandatory unit, property, contract, and integration tests pass (zero failures)
    - HMAC property test: 100+ iterations, zero false accepts
    - Idempotency property test: 100+ iterations, zero duplicate documents
    - Tenant isolation property test: 100+ iterations, zero cross-tenant leaks
    - End-to-end flow verified: Dinee webhook → ES indices → API → Dashboard → AI tools
    - Webhook ingestion p95 latency < 2s (measured over 1000 events)
    - API query p95 latency < 500ms (measured over 100 requests)
    - Zero high-severity drift alerts (drift < 1% across all tenants)
    - Poison queue depth < 10 (no unresolved backlog)
    - All Prometheus metrics emitting and alert rules configured
    - Feature flag gating verified on all 4 surfaces (webhook, API, WebSocket, AI tools)
  - Ask the user if questions arise

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The following test tasks are MANDATORY (not optional): 20.1 (webhook receiver), 20.3 (tenant guard), 21.1 (HMAC property), 21.2 (idempotency property), 21.5 (tenant isolation property), 22.1 (sample payloads), 22.2 (contract tests)
- Remaining test tasks (20.2, 20.4-20.7, 21.3-21.4, 21.6-21.8, 23.1-23.4, 24.1-24.2, 25.1-25.2) are optional (`*`) and can be deferred for MVP
- Canonical webhook auth policy: HMAC-SHA256 only. The `dinee_api_key` setting is used exclusively for outbound Dinee REST API calls (Replay Service), not for webhook verification
- PII masking is role-based by endpoint audience, not blanket default on all responses
- Feature flag disabled behavior is defined for all 4 surfaces: webhook (200 skip), API (404), WebSocket (reject/disconnect with 4403), AI tools (disabled response)
- Replay service uses the same scripted upsert as live events; older replay snapshots are naturally discarded by the out-of-order reconciliation logic
- Each task references specific requirements for traceability
- Checkpoints have strict Definition of Done gates with measurable pass/fail criteria
- The implementation order ensures dependencies are satisfied: config → indices → ingestion → API → AI tools → frontend → infrastructure → tests
- Property tests require minimum 100 iterations per test
- The ops module (`Runsheet-backend/ops/`) is self-contained to minimize coupling with existing code
