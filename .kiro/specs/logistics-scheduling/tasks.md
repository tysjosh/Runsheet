# Implementation Plan: Logistics Scheduling & Dispatch

## Overview

This implementation plan builds the Logistics Scheduling & Dispatch module on top of the existing Runsheet platform infrastructure. Tasks are organized in dependency order: configuration and Elasticsearch indices first, then core services (JobService, CargoService, DelayDetectionService), API endpoints with tenant scoping, WebSocket live updates, AI tools, frontend dashboard pages, and finally testing.

## Tasks

- [x] 1. Configuration and package structure
  - [x] 1.1 Create scheduling module package structure
    - Create `Runsheet-backend/scheduling/` package with `__init__.py`
    - Create subpackages: `scheduling/api/`, `scheduling/services/`, `scheduling/websocket/`
    - Each subpackage gets an `__init__.py`
    - _Requirements: 1.1-1.6_

  - [x] 1.2 Create Pydantic models and enums
    - Create `scheduling/models.py` with all enums: JobType, JobStatus, Priority, CargoItemStatus
    - Add VALID_TRANSITIONS dict mapping current status to allowed target statuses
    - Add JOB_ASSET_COMPATIBILITY dict mapping job types to compatible asset types
    - Add all Pydantic models: GeoPoint, CargoItem, CreateJob, AssignAsset, StatusTransition, UpdateCargoManifest, UpdateCargoItemStatus, Job, JobEvent, JobSummary, SchedulingMetricsBucket, CompletionMetrics, AssetUtilizationMetric
    - Add model validators: cargo_manifest required for cargo_transport, failure_reason required for failed status
    - _Requirements: 1.3, 1.4, 2.1-2.3, 3.1, 4.1-4.3, 6.3_

  - [x] 1.3 Add scheduling-specific settings to config/settings.py
    - Add `scheduling_default_eta_hours` (default 4)
    - Add `scheduling_delay_check_interval_seconds` (default 60)
    - Add `scheduling_max_active_jobs_per_asset` (default 1)
    - Update `.env.development` and `.env.example` with new variables
    - _Requirements: 7.1, 7.3_

- [x] 2. Elasticsearch indices for scheduling data
  - [x] 2.1 Create jobs_current index mapping
    - Create `scheduling/services/scheduling_es_mappings.py`
    - Strict mapping with keyword fields: job_id, job_type, status, tenant_id, asset_assigned, created_by, priority
    - Text+keyword fields: origin, destination, failure_reason, notes
    - Geo_point fields: origin_location, destination_location
    - Date fields: scheduled_time, estimated_arrival, started_at, completed_at, created_at, updated_at
    - Boolean field: delayed
    - Integer field: delay_duration_minutes
    - Nested object: cargo_manifest (item_id, description, weight_kg, container_number, seal_number, item_status)
    - Configure 1 primary shard, 1 replica
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 6.3_

  - [x] 2.2 Create job_events index mapping
    - Strict mapping with keyword fields: event_id, job_id, event_type, tenant_id, actor_id
    - Date field: event_timestamp
    - Object field (enabled: false): event_payload
    - Configure ILM policy: warm after 30 days, cold after 90 days, delete after 365 days
    - _Requirements: 1.2, 1.6, 15.1_

  - [x] 2.3 Register scheduling indices in application startup
    - Create `setup_scheduling_indices()` function in scheduling_es_mappings.py
    - Call it during lifespan startup in main.py
    - Verify ILM policies are applied on startup, log warnings for missing policies
    - _Requirements: 1.5, 1.6_

- [x] 3. Checkpoint - Verify Elasticsearch indices
  - Definition of Done (all must pass):
    - Both `jobs_current` and `job_events` indices created on startup with strict mappings
    - Indexing a document with an unmapped field is rejected
    - ILM policy attached to `job_events`
    - No ERROR-level log entries during index creation
  - Ask the user if questions arise

- [x] 4. Core scheduling services
  - [x] 4.1 Implement JobIdGenerator
    - Create `scheduling/services/job_id_generator.py`
    - Use Redis INCR on key `scheduling:job_id_counter` for atomic sequential IDs
    - Return `JOB_{counter}` format
    - Fallback to UUID-based ID (`JOB_{uuid[:8]}`) if Redis unavailable
    - Reuse existing Redis connection pattern from session/redis_store.py
    - _Requirements: 2.2_

  - [x] 4.2 Implement JobService - job creation
    - Create `scheduling/services/job_service.py` with JobService class
    - Constructor takes ElasticsearchService and optional redis_url
    - Implement `create_job()`:
      - Generate job_id via JobIdGenerator
      - Set initial status to "scheduled", created_at, updated_at
      - If asset_assigned provided: verify asset exists in assets/trucks index, verify asset_type compatible via JOB_ASSET_COMPATIBILITY, check no overlapping active jobs
      - Auto-generate item_id for cargo manifest items if not provided
      - Index document into jobs_current
      - Append "job_created" event to job_events
      - Set tenant_id from authenticated context (not request body)
    - _Requirements: 2.1-2.8, 8.5_

  - [x] 4.3 Implement JobService - asset assignment
    - Implement `assign_asset()`:
      - Fetch job from jobs_current, verify status is "scheduled"
      - Verify asset exists in assets index and asset_type is compatible with job_type
      - Query jobs_current for active jobs (status: assigned or in_progress) with same asset_assigned — reject with 409 if conflict found
      - Update job: status → "assigned", set asset_assigned, updated_at
      - Append "asset_assigned" event
    - Implement `reassign_asset()`:
      - Verify job status is "assigned" or "in_progress"
      - Verify new asset compatible and available
      - Update asset_assigned, append "asset_reassigned" event with old and new asset_ids
    - _Requirements: 3.1-3.6_

  - [x] 4.4 Implement JobService - status transitions
    - Implement `transition_status()`:
      - Fetch job, validate transition against VALID_TRANSITIONS map
      - Return 400 with current status and disallowed target if invalid
      - For "in_progress": verify asset is assigned, set started_at, calculate estimated_arrival (scheduled_time + scheduling_default_eta_hours)
      - For "completed": set completed_at, record delay_duration_minutes if delayed
      - For "failed": require failure_reason in request body
      - For "cancelled" or "failed": release asset (no-op for MVP, availability is query-based)
      - Append "status_changed" event with old_status, new_status, actor_id
    - _Requirements: 4.1-4.8_

  - [x] 4.5 Implement JobService - query methods
    - Implement `get_job()`: fetch from jobs_current by job_id with tenant filter, include event history from job_events
    - Implement `list_jobs()`: paginated query with filters (job_type, status, asset_assigned, origin, destination, date range), sorting, tenant scoping
    - Implement `get_active_jobs()`: query status in (scheduled, assigned, in_progress), sort by scheduled_time asc
    - Implement `get_delayed_jobs()`: query status=in_progress AND delayed=true
    - Implement `get_job_events()`: query job_events by job_id, sort by event_timestamp asc
    - Return 400 for invalid filter values
    - _Requirements: 5.1-5.7, 15.2_

  - [x] 4.6 Implement CargoService
    - Create `scheduling/services/cargo_service.py`
    - Implement `get_cargo_manifest()`: fetch job, return cargo_manifest array
    - Implement `update_cargo_manifest()`: replace cargo_manifest nested array, auto-generate item_id for new items, append "cargo_updated" event
    - Implement `update_cargo_item_status()`: update single item's item_status within nested array using painless script, append "cargo_status_changed" event, check if all items delivered → broadcast cargo_complete via WebSocket
    - Implement `search_cargo()`: nested query across all jobs by container_number, description, or item_status with pagination
    - _Requirements: 6.1-6.6_

  - [x] 4.7 Implement DelayDetectionService
    - Create `scheduling/services/delay_detection_service.py`
    - Implement `check_delays()`: query jobs_current for in_progress jobs where now > estimated_arrival AND delayed=false, mark as delayed, calculate delay_duration_minutes, broadcast delay_alert via WebSocket
    - Implement `get_eta()`: return estimated_arrival for a job
    - Implement `get_delay_metrics()`: count delayed jobs, avg delay duration, delays grouped by job_type using ES aggregations
    - _Requirements: 7.1-7.6_

  - [x] 4.8 Implement event append helper
    - Implement `_append_event()` in JobService: generate event_id (UUID), set event_timestamp, tenant_id, actor_id, index into job_events
    - Ensure every mutation (create, assign, reassign, status change, cargo update) appends exactly one event before returning
    - _Requirements: 15.1, 15.3, 15.4_

- [ ] 5. Checkpoint - Verify core services
  - Definition of Done (all must pass):
    - Job creation with valid payload returns job_id in JOB_{number} format
    - Cargo_transport without cargo_manifest returns 400
    - Vessel_movement with vehicle asset returns 400 (incompatible type)
    - Assigning an already-busy asset returns 409
    - Invalid status transition (e.g., scheduled → completed) returns 400
    - Every mutation produces exactly one event in job_events
    - Tenant filter is injected in all queries
  - Ask the user if questions arise

- [x] 6. Scheduling API endpoints
  - [x] 6.1 Create scheduling API router
    - Create `scheduling/api/endpoints.py` with FastAPI router (prefix="/scheduling")
    - Wire JobService, CargoService, DelayDetectionService dependencies
    - Apply rate limiting using existing limiter middleware
    - Apply tenant scoping using existing Tenant_Guard (get_tenant_context dependency)
    - All responses use consistent JSON envelope: {data, pagination, request_id}
    - _Requirements: 5.1, 8.1-8.5_

  - [x] 6.2 Implement job CRUD endpoints
    - POST `/scheduling/jobs` — create job (201 response)
    - GET `/scheduling/jobs` — list with filters, pagination, sorting
    - GET `/scheduling/jobs/active` — active jobs only
    - GET `/scheduling/jobs/delayed` — delayed jobs only
    - GET `/scheduling/jobs/{job_id}` — single job with event history
    - GET `/scheduling/jobs/{job_id}/events` — event timeline
    - _Requirements: 2.1, 5.1-5.7, 15.2_

  - [x] 6.3 Implement assignment endpoints
    - PATCH `/scheduling/jobs/{job_id}/assign` — assign asset
    - PATCH `/scheduling/jobs/{job_id}/reassign` — reassign asset
    - _Requirements: 3.1-3.6_

  - [x] 6.4 Implement status transition endpoint
    - PATCH `/scheduling/jobs/{job_id}/status` — transition status
    - _Requirements: 4.1-4.8_

  - [x] 6.5 Implement cargo endpoints
    - GET `/scheduling/jobs/{job_id}/cargo` — get cargo manifest
    - PATCH `/scheduling/jobs/{job_id}/cargo` — update cargo manifest
    - PATCH `/scheduling/jobs/{job_id}/cargo/{item_id}/status` — update cargo item status
    - GET `/scheduling/cargo/search` — search cargo across jobs
    - _Requirements: 6.1-6.6_

  - [x] 6.6 Implement ETA and metrics endpoints
    - GET `/scheduling/jobs/{job_id}/eta` — get ETA
    - GET `/scheduling/metrics/jobs` — job counts by status/type in time buckets
    - GET `/scheduling/metrics/completion` — completion rate and avg time by job_type
    - GET `/scheduling/metrics/assets` — asset utilization (jobs per asset, idle time)
    - GET `/scheduling/metrics/delays` — delay statistics
    - Support `bucket` query param (hourly/daily, default hourly)
    - Enforce daily granularity for time ranges > 90 days
    - _Requirements: 7.2, 7.5, 13.1-13.5_

  - [x] 6.7 Register scheduling router in main.py
    - Import and include scheduling router in FastAPI app
    - Initialize JobService, CargoService, DelayDetectionService with existing elasticsearch_service and redis
    - Apply rate limiting: 100 req/min per user for scheduling endpoints
    - _Requirements: 8.1_

- [x] 7. Checkpoint - Verify API layer
  - Definition of Done (all must pass):
    - All endpoints return correct JSON envelope {data, pagination, request_id}
    - Tenant isolation: query with tenant_id=A returns zero documents belonging to tenant_id=B
    - Filter combinations (job_type + status + date range) return correct subsets
    - Pagination: total_pages matches ceil(total / size)
    - Rate limiting: 101st request within 1 minute returns 429
    - Invalid filter values return 400 with structured error
    - POST /scheduling/jobs returns 201 with job_id
    - PATCH /scheduling/jobs/{id}/assign with busy asset returns 409
  - Ask the user if questions arise

- [x] 8. WebSocket live updates for scheduling
  - [x] 8.1 Create SchedulingWebSocketManager
    - Create `scheduling/websocket/scheduling_ws.py`
    - Support subscription filters: job_created, status_changed, delay_alert, cargo_update
    - Implement heartbeat every 30 seconds
    - Detect and disconnect stale clients
    - _Requirements: 9.1, 9.3, 9.6_

  - [x] 8.2 Add WebSocket endpoint and broadcast integration
    - Add `/ws/scheduling` WebSocket endpoint to main.py
    - Broadcast job_created on job creation
    - Broadcast status_changed on status transitions and assignments
    - Broadcast delay_alert when jobs become delayed
    - Broadcast cargo_update on cargo item status changes
    - Broadcast cargo_complete when all manifest items reach "delivered"
    - Filter broadcasts by client subscriptions
    - _Requirements: 9.1-9.4, 6.6_

  - [x] 8.3 Integrate WebSocket broadcasts into services
    - Wire SchedulingWebSocketManager into JobService and CargoService
    - Call broadcast methods after each mutation (create, assign, status change, cargo update)
    - Include full job data in broadcast payloads
    - _Requirements: 9.2, 9.4_

- [x] 9. Implement delay detection periodic check
  - [x] 9.1 Add periodic delay check to application lifecycle
    - Start a background task during app lifespan that calls `DelayDetectionService.check_delays()` at configurable interval (default 60s)
    - The check queries all in_progress jobs where now > estimated_arrival AND delayed=false
    - Mark matching jobs as delayed, update delay_duration_minutes
    - Broadcast delay_alert for each newly delayed job
    - _Requirements: 7.3, 7.4_

- [x] 10. Checkpoint - Verify WebSocket and delay detection
  - Definition of Done (all must pass):
    - WebSocket client at /ws/scheduling receives job_created event within 5s of POST /scheduling/jobs
    - WebSocket client receives status_changed event within 5s of PATCH /scheduling/jobs/{id}/status
    - WebSocket client receives delay_alert when a job's estimated_arrival passes
    - WebSocket heartbeat received within 30s of connection
    - Delay detection marks overdue jobs as delayed within one check interval
  - Ask the user if questions arise

- [x] 11. AI scheduling tools
  - [x] 11.1 Create scheduling AI tools
    - Create `Agents/tools/scheduling_tools.py`
    - Implement `search_jobs` tool: query jobs_current with job_type, status, asset, origin, destination, time range filters; tenant-scoped; return structured results
    - Implement `get_job_details` tool: return single job with event history and cargo manifest
    - Implement `find_available_assets` tool: query assets index for all assets of given type, query jobs_current for active jobs in time window, return assets not in active job set
    - Implement `get_scheduling_summary` tool: return active jobs count, delayed count, available assets, upcoming scheduled jobs
    - Implement `generate_dispatch_report` tool: markdown report with completion rates, delay analysis, asset utilization, recommendations
    - All tools read-only, tenant-scoped, log invocations
    - _Requirements: 14.1-14.7_

  - [x] 11.2 Register scheduling tools with AI agent
    - Import scheduling tools in `Agents/tools/__init__.py`
    - Add to ALL_TOOLS list
    - Update agent system prompt in mainagent.py with scheduling tool descriptions and example queries (e.g., "show me all delayed cargo jobs", "find available trucks for tomorrow", "what's the status of JOB_2332")
    - _Requirements: 14.1_

- [ ] 12. Frontend scheduling dashboard
  - [x] 12.1 Add scheduling TypeScript types
    - Add to `runsheet/src/types/api.ts`: JobType, JobStatus, CargoItemStatus, Priority union types
    - Add interfaces: CargoItem, Job, JobEvent, JobSummary, OperationsControlSummary
    - _Requirements: 11.1-11.7, 12.1-12.5_

  - [x] 12.2 Create scheduling API client
    - Create `runsheet/src/services/schedulingApi.ts`
    - Implement typed functions for all scheduling endpoints: getJobs, getJob, getActiveJobs, getDelayedJobs, createJob, assignAsset, reassignAsset, transitionStatus, getCargo, updateCargo, updateCargoItemStatus, searchCargo, getEta, getJobMetrics, getCompletionMetrics, getAssetUtilization, getDelayMetrics, getJobEvents
    - Reuse existing API_TIMEOUTS and error handling from api.ts
    - _Requirements: 5.1-5.7, 6.1-6.5, 7.2, 7.5, 13.1-13.5_

  - [x] 12.3 Create useSchedulingWebSocket hook
    - Create `runsheet/src/hooks/useSchedulingWebSocket.ts`
    - Connect to `/ws/scheduling` with subscription filters
    - Auto-reconnect with exponential backoff (1s initial, 30s max)
    - Parse job_created, status_changed, delay_alert, cargo_update events
    - _Requirements: 9.5_

  - [x] 12.4 Create Job Board page
    - Create `runsheet/src/app/ops/scheduling/page.tsx`
    - Create `runsheet/src/components/ops/JobBoard.tsx`: sortable columns (job_id, job_type, status, origin, destination, asset_assigned, scheduled_time, estimated_arrival), color-coded rows by status
    - Create `runsheet/src/components/ops/JobSummaryBar.tsx`: counts by status (total, scheduled, assigned, in_progress, completed, failed, delayed)
    - Create `runsheet/src/components/ops/JobFilters.tsx`: filter controls for job_type, status, date range, asset_assigned
    - Create `runsheet/src/components/ops/JobActionButtons.tsx`: status transition buttons per row based on current status and valid transitions
    - Update rows within 5 seconds via WebSocket
    - _Requirements: 11.1-11.7_

  - [x] 12.5 Create Cargo Tracking page
    - Create `runsheet/src/app/ops/scheduling/[id]/cargo/page.tsx`
    - Create `runsheet/src/components/ops/CargoManifestView.tsx`: cargo item list with item_id, description, weight_kg, container_number, seal_number, item_status with status color-coding
    - Create `runsheet/src/components/ops/CargoItemActions.tsx`: action buttons to update item status (loaded, in_transit, delivered, damaged)
    - Display job header with job_id, origin, destination, asset_assigned, overall status
    - Update item rows within 5 seconds via WebSocket
    - _Requirements: 12.1-12.5_

  - [x] 12.6 Create Operations Control Dashboard page
    - Create `runsheet/src/app/ops/control/page.tsx`
    - Create `runsheet/src/components/ops/OperationsControlView.tsx`: command center layout composing all panels
    - Create `runsheet/src/components/ops/OperationsSummaryBar.tsx`: active jobs, delayed count, available assets, fuel alerts count
    - Create `runsheet/src/components/ops/OperationsMap.tsx`: map overlay showing asset locations with job assignment indicators (color-coded by job status), reuse existing MapView patterns
    - Create `runsheet/src/components/ops/JobQueuePanel.tsx`: upcoming scheduled/assigned jobs sorted by scheduled_time
    - Create `runsheet/src/components/ops/DelayedOperationsPanel.tsx`: delayed jobs with delay duration
    - Create `runsheet/src/components/ops/FuelStatusSidebar.tsx`: stations with low/critical fuel levels from fuel API
    - Subscribe to scheduling + ops + fuel WebSocket events for real-time updates across all panels
    - _Requirements: 10.1-10.7_

  - [x] 12.7 Add scheduling navigation to sidebar
    - Add "Scheduling" and "Operations Control" links to existing Sidebar component under the ops section
    - Link to `/ops/scheduling` and `/ops/control`
    - _Requirements: 11.1, 10.1_

- [x] 13. Checkpoint - Verify frontend dashboard
  - Definition of Done (all must pass):
    - Job Board page renders with mock data, color-coding matches spec (blue/orange/green/gray/red/yellow)
    - Cargo Tracking page renders manifest items with status colors
    - Operations Control page renders all panels (summary, map, job queue, delays, fuel)
    - WebSocket update reflected in UI within 5s of broadcast
    - Filters narrow displayed data correctly
    - Pagination controls navigate correctly
    - Scheduling nav links visible in sidebar
  - Ask the user if questions arise

- [x] 14. Unit tests
  - [x] 14.1 Test JobService - creation and validation
    - Test job creation with valid payload returns JOB_{number} id
    - Test cargo_transport without cargo_manifest returns 400
    - Test vessel_movement with vehicle asset returns 400
    - Test crane_booking with non-crane equipment returns 400
    - Test creation with asset_assigned verifies asset exists
    - Test creation appends job_created event
    - _Requirements: 2.1-2.8_

  - [x] 14.2 Test JobService - assignment and conflicts
    - Test assign_asset updates status to assigned
    - Test assign with incompatible asset_type returns 400
    - Test assign with busy asset (overlapping active job) returns 409
    - Test reassign_asset logs old and new asset_ids
    - Test assign appends asset_assigned event
    - _Requirements: 3.1-3.6_

  - [x] 14.3 Test JobService - status transitions
    - Test all valid transitions succeed (scheduled→assigned, assigned→in_progress, etc.)
    - Test all invalid transitions return 400 (scheduled→completed, completed→in_progress, etc.)
    - Test in_progress sets started_at and estimated_arrival
    - Test completed sets completed_at and records delay duration if delayed
    - Test failed requires failure_reason
    - Test each transition appends status_changed event
    - _Requirements: 4.1-4.8_

  - [x] 14.4 Test CargoService
    - Test get_cargo_manifest returns manifest items
    - Test update_cargo_manifest replaces items and auto-generates item_ids
    - Test update_cargo_item_status updates single item
    - Test cargo_status_changed event appended on item status update
    - Test all-delivered detection triggers cargo_complete
    - Test search_cargo by container_number, description, item_status
    - _Requirements: 6.1-6.6_

  - [x] 14.5 Test DelayDetectionService
    - Test check_delays marks overdue jobs as delayed
    - Test check_delays calculates delay_duration_minutes correctly
    - Test get_eta returns estimated_arrival
    - Test get_delay_metrics returns correct counts and averages
    - _Requirements: 7.1-7.6_

  - [x] 14.6 Test tenant scoping
    - Test all query methods include tenant_id filter
    - Test requests without valid tenant_id return 403
    - Test tenant_id from query params is ignored
    - Test job creation sets tenant_id from JWT context
    - _Requirements: 8.1-8.5_

  - [x] 14.7 Test API endpoints
    - Test all endpoint response formats and status codes
    - Test input validation (missing required fields, invalid enums)
    - Test pagination (total_pages = ceil(total / size))
    - Test filter combinations
    - Test rate limiting
    - _Requirements: 5.1-5.7, 13.1-13.5_

- [x] 15. Property-based tests
  - [x] 15.1 Property test: Status Transition Validity
    - Generate random (current_status, target_status) pairs
    - Verify acceptance iff pair exists in VALID_TRANSITIONS
    - Minimum 100 iterations
    - **Property 1, Validates: Requirements 4.2, 4.3**

  - [x] 15.2 Property test: Asset-Type Compatibility
    - Generate random (job_type, asset_type) pairs
    - Verify acceptance iff asset_type in JOB_ASSET_COMPATIBILITY[job_type]
    - Minimum 100 iterations
    - **Property 2, Validates: Requirements 2.4, 2.5, 3.3**

  - [x] 15.3 Property test: Asset Scheduling Conflict Detection
    - Generate job sequences with overlapping time windows for same asset
    - Verify second assignment is rejected with 409
    - Minimum 100 iterations
    - **Property 3, Validates: Requirements 2.6, 3.4**

  - [x] 15.4 Property test: Event Append Completeness
    - Generate sequences of mutations (create, assign, status change, cargo update)
    - Verify event count in job_events equals mutation count
    - Minimum 100 iterations
    - **Property 4, Validates: Requirements 2.7, 3.5, 4.7, 6.4, 15.3**

  - [x] 15.5 Property test: Tenant Isolation
    - Generate multi-tenant queries
    - Verify every ES query includes tenant_id filter and zero cross-tenant results
    - Minimum 100 iterations
    - **Property 5, Validates: Requirements 8.1-8.5**

  - [x] 15.6 Property test: Job ID Uniqueness
    - Generate concurrent job creation sequences
    - Verify all job_ids are unique
    - Minimum 100 iterations
    - **Property 6, Validates: Requirement 2.2**

  - [x] 15.7 Property test: Event Payload Round-Trip
    - Generate random event payloads, serialize to JSON, deserialize, compare
    - Verify equivalence
    - Minimum 100 iterations
    - **Property 7, Validates: Requirement 15.5**

- [x] 16. Integration tests
  - [ ]* 16.1 Test full job lifecycle
    - Create job → assign asset → start (in_progress) → complete
    - Verify job document in ES at each stage
    - Verify event history contains all transitions
    - Verify WebSocket broadcasts received
    - _Requirements: 2.1-2.8, 3.1-3.5, 4.1-4.8_

  - [ ]* 16.2 Test cargo tracking flow
    - Create cargo_transport job with manifest → update item statuses → verify all-delivered notification
    - _Requirements: 6.1-6.6_

  - [ ]* 16.3 Test delay detection flow
    - Create job → start → wait past ETA → verify delay detection marks job as delayed → verify WebSocket delay_alert
    - _Requirements: 7.3, 7.4_

  - [ ]* 16.4 Test API with tenant isolation
    - Create jobs for tenant A and tenant B
    - Verify tenant A queries return zero tenant B jobs
    - _Requirements: 8.1-8.5_

- [x] 17. Frontend tests
  - [ ]* 17.1 Jest component tests
    - Test JobBoard rendering and color-coding
    - Test JobActionButtons shows correct buttons per status
    - Test CargoManifestView renders items with status colors
    - Test OperationsSummaryBar displays correct counts
    - _Requirements: 11.1-11.7, 12.1-12.5, 10.1-10.7_

  - [ ]* 17.2 Playwright E2E tests
    - Navigate scheduling pages, verify data display
    - Test filter interactions on job board
    - Test cargo item status update flow
    - _Requirements: 11.6, 12.4, 12.5_

- [x] 18. Final checkpoint - Complete logistics scheduling verification
  - Definition of Done (all must pass):
    - All mandatory unit and property tests pass (zero failures)
    - Status transition property test: 100+ iterations, zero invalid transitions accepted
    - Asset compatibility property test: 100+ iterations, zero incompatible assignments accepted
    - Tenant isolation property test: 100+ iterations, zero cross-tenant leaks
    - End-to-end flow verified: create job → assign → start → complete, with events and WebSocket broadcasts
    - Cargo tracking flow verified: create manifest → update items → all-delivered notification
    - Delay detection verified: overdue job marked as delayed within check interval
    - All 3 frontend pages render without console errors
    - API response latency < 500ms for job queries (measured over 50 requests)
  - Ask the user if questions arise

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The following test tasks are MANDATORY (not optional): 14.1-14.7 (unit tests), 15.1 (status transitions), 15.2 (asset compatibility), 15.4 (event completeness), 15.5 (tenant isolation)
- Remaining test tasks (15.3, 15.6, 15.7, 16.1-16.4, 17.1-17.2) are optional (`*`) and can be deferred for MVP
- Asset availability is determined by querying active jobs, not by maintaining a separate lock on assets — this avoids dual-write consistency issues
- The scheduling module (`Runsheet-backend/scheduling/`) is self-contained to minimize coupling with existing code
- The delay detection periodic check runs as a background task during app lifespan, not as a separate service
- Each task references specific requirements for traceability
- Checkpoints have strict Definition of Done gates with measurable pass/fail criteria
- The implementation order ensures dependencies are satisfied: config → indices → services → API → WebSocket → AI tools → frontend → tests
- Property tests require minimum 100 iterations per test
