# Requirements Document

## Introduction

This document specifies the requirements for the Logistics Scheduling and Dispatch System, the central job management capability for the Runsheet logistics platform. The system enables operations teams to create, assign, track, and complete logistics jobs across five job types: cargo transport, passenger transport, vessel movement, airport transfer, and crane booking. Each job ties a tracked asset to a logistics operation with origin/destination, scheduling, and status progression. The system also provides cargo/shipment lifecycle tracking with ETA management and a unified operations control dashboard that consolidates active jobs, delayed operations, asset assignments, fuel levels, and real-time asset locations into a single command center view. The feature integrates with the existing multi-asset tracking system (assets index), fuel monitoring module (fuel_stations index), and ops intelligence layer (shipments/riders indices), extending the Elasticsearch data store, FastAPI backend, Next.js frontend, WebSocket real-time updates, and AI agent tooling.

## Glossary

- **Job**: A scheduled logistics operation that assigns an asset to perform a task between an origin and destination within a time window. Identified by a unique job_id.
- **Job_Type**: The classification of a logistics job. Supported types: cargo_transport, passenger_transport, vessel_movement, airport_transfer, crane_booking.
- **Job_Status**: The lifecycle state of a job. Progression: scheduled → assigned → in_progress → completed | cancelled | failed.
- **Jobs_Current_Index**: The Elasticsearch index holding the current state of each job, keyed by job_id, with strict mapping enforcement.
- **Job_Events_Index**: The Elasticsearch append-only index storing the full event history for jobs (status changes, reassignments, notes), keyed by event_id.
- **Cargo_Manifest**: The set of cargo items associated with a cargo_transport job, including container references, weight, description, and seal numbers.
- **Scheduling_API**: The set of FastAPI endpoints under `/scheduling/*` that manage job lifecycle operations.
- **Scheduling_Dashboard**: The frontend Next.js page suite providing the unified operations control view, job board, and cargo tracking.
- **AI_Scheduling_Tools**: The set of AI agent tool functions that query and report on job and cargo data for the Runsheet AI assistant.
- **ETA**: Estimated Time of Arrival, calculated from scheduled_time, origin, destination, and real-time asset location data.
- **Dispatch_Assignment**: The act of linking an available asset to a scheduled job, changing the job status from scheduled to assigned.
- **Operations_Control_View**: The unified command center dashboard combining active jobs, asset locations, fuel levels, and delayed operation alerts.

## Requirements

### Requirement 1: Job Data Model and Elasticsearch Index

**User Story:** As a data engineer, I want a dedicated Elasticsearch index for logistics jobs with strict mappings and type-specific fields, so that all job types are stored consistently and queries perform predictably.

#### Acceptance Criteria

1. THE ElasticsearchService SHALL create the `jobs_current` index with strict mapping including keyword fields for job_id, job_type, status, tenant_id, asset_assigned, origin, destination, and created_by; date fields for scheduled_time, estimated_arrival, started_at, completed_at, created_at, and updated_at; and a nested object for cargo_manifest
2. THE ElasticsearchService SHALL create the `job_events` index with strict mapping including keyword fields for event_id, job_id, event_type, tenant_id, and actor_id; date field for event_timestamp; and a nested object for event_payload
3. THE Jobs_Current_Index SHALL enforce that job_type is one of: cargo_transport, passenger_transport, vessel_movement, airport_transfer, crane_booking
4. THE Jobs_Current_Index SHALL enforce that status is one of: scheduled, assigned, in_progress, completed, cancelled, failed
5. WHEN a document with an unmapped field is indexed into a jobs index, THE ElasticsearchService SHALL reject the document due to strict mapping enforcement
6. THE ElasticsearchService SHALL configure the `job_events` index with an ILM policy that transitions data to warm tier after 30 days, cold tier after 90 days, and deletes after 365 days

### Requirement 2: Job Creation and Validation

**User Story:** As a logistics coordinator, I want to create new jobs with validated fields and type-specific requirements, so that every job enters the system with complete and correct data.

#### Acceptance Criteria

1. THE Scheduling_API SHALL expose a POST `/scheduling/jobs` endpoint that creates a new job with required fields: job_type, origin, destination, scheduled_time, and optional fields: asset_assigned, cargo_manifest, priority, notes, and created_by
2. WHEN a job is created, THE Scheduling_API SHALL generate a unique job_id with the format `JOB_{sequential_number}` and set the initial status to "scheduled"
3. WHEN a cargo_transport job is created, THE Scheduling_API SHALL validate that the cargo_manifest contains at least one item with description and weight_kg fields
4. WHEN a vessel_movement job is created, THE Scheduling_API SHALL validate that the asset_assigned (if provided) references an asset with asset_type "vessel" in the Assets_Index
5. WHEN a crane_booking job is created, THE Scheduling_API SHALL validate that the asset_assigned (if provided) references an asset with asset_type "equipment" and asset_subtype "crane" in the Assets_Index
6. WHEN a job is created with an asset_assigned, THE Scheduling_API SHALL verify that the asset exists in the Assets_Index and is not already assigned to an overlapping active job
7. WHEN a job is successfully created, THE Scheduling_API SHALL append a "job_created" event to the Job_Events_Index with the full job payload and creator information
8. IF any validation fails during job creation, THEN THE Scheduling_API SHALL return a 400 status with a descriptive error identifying the invalid field and constraint

### Requirement 3: Job Assignment and Dispatch

**User Story:** As a dispatch operator, I want to assign available assets to scheduled jobs, so that jobs transition from planned to actionable with a confirmed asset.

#### Acceptance Criteria

1. THE Scheduling_API SHALL expose a PATCH `/scheduling/jobs/{job_id}/assign` endpoint that assigns an asset to a job by accepting an asset_id
2. WHEN an asset is assigned to a job, THE Scheduling_API SHALL update the job status from "scheduled" to "assigned" and set the asset_assigned field
3. WHEN an asset assignment is requested, THE Scheduling_API SHALL verify that the asset exists in the Assets_Index and has a compatible asset_type for the job_type (vehicles for cargo_transport and passenger_transport, vessels for vessel_movement, equipment for crane_booking)
4. IF the specified asset is already assigned to another active job (status in_progress or assigned) with an overlapping time window, THEN THE Scheduling_API SHALL reject the assignment with a 409 status indicating the scheduling conflict
5. WHEN an asset is successfully assigned, THE Scheduling_API SHALL append an "asset_assigned" event to the Job_Events_Index with the asset_id, job_id, and operator information
6. THE Scheduling_API SHALL expose a PATCH `/scheduling/jobs/{job_id}/reassign` endpoint that changes the assigned asset, appending a "asset_reassigned" event with both old and new asset_ids

### Requirement 4: Job Status Progression and Lifecycle

**User Story:** As an operations manager, I want jobs to follow a defined status progression with validation at each transition, so that job lifecycle is tracked accurately and invalid transitions are prevented.

#### Acceptance Criteria

1. THE Scheduling_API SHALL expose a PATCH `/scheduling/jobs/{job_id}/status` endpoint that transitions a job to a new status
2. THE Scheduling_API SHALL enforce the following valid status transitions: scheduled → assigned, scheduled → cancelled, assigned → in_progress, assigned → cancelled, in_progress → completed, in_progress → failed, in_progress → cancelled
3. IF an invalid status transition is requested, THEN THE Scheduling_API SHALL return a 400 status with a message identifying the current status and the disallowed target status
4. WHEN a job transitions to "in_progress", THE Scheduling_API SHALL record the started_at timestamp and verify that an asset is assigned
5. WHEN a job transitions to "completed", THE Scheduling_API SHALL record the completed_at timestamp
6. WHEN a job transitions to "failed", THE Scheduling_API SHALL require a failure_reason field in the request body
7. WHEN any status transition occurs, THE Scheduling_API SHALL append a "status_changed" event to the Job_Events_Index with the old status, new status, timestamp, and actor_id
8. WHEN a job transitions to "cancelled" or "failed", THE Scheduling_API SHALL release the assigned asset so it becomes available for other jobs

### Requirement 5: Job Query and Filtering Endpoints

**User Story:** As a frontend developer, I want normalized REST endpoints for querying jobs with filtering and pagination, so that the scheduling dashboard can display job boards and search results without coupling to Elasticsearch query syntax.

#### Acceptance Criteria

1. THE Scheduling_API SHALL expose a GET `/scheduling/jobs` endpoint that returns paginated job records from the Jobs_Current_Index with a consistent JSON envelope containing `data`, `pagination`, and `request_id` fields
2. THE Scheduling_API SHALL support filtering the `/scheduling/jobs` endpoint by job_type, status, asset_assigned, origin, destination, and date range (scheduled_time)
3. THE Scheduling_API SHALL expose a GET `/scheduling/jobs/{job_id}` endpoint that returns a single job with its full event history from the Job_Events_Index
4. THE Scheduling_API SHALL expose a GET `/scheduling/jobs/active` endpoint that returns all jobs with status in (scheduled, assigned, in_progress) sorted by scheduled_time ascending
5. THE Scheduling_API SHALL expose a GET `/scheduling/jobs/delayed` endpoint that returns jobs where the current time exceeds the estimated_arrival and status is "in_progress"
6. THE Scheduling_API SHALL support combining multiple filters via query parameters (job_type AND status AND date range)
7. WHEN a filter parameter contains an invalid value, THE Scheduling_API SHALL return a 400 status with a descriptive validation error

### Requirement 6: Cargo Manifest and Shipment Tracking

**User Story:** As a cargo operations coordinator, I want to track cargo manifests attached to transport jobs with container references and weight, so that I can monitor what is being moved and verify delivery completeness.

#### Acceptance Criteria

1. THE Scheduling_API SHALL expose a GET `/scheduling/jobs/{job_id}/cargo` endpoint that returns the cargo manifest for a cargo_transport job
2. THE Scheduling_API SHALL expose a PATCH `/scheduling/jobs/{job_id}/cargo` endpoint that updates the cargo manifest items (add, remove, or modify items)
3. THE Jobs_Current_Index cargo_manifest nested object SHALL include fields: item_id, description, weight_kg, container_number (optional), seal_number (optional), and item_status (pending, loaded, in_transit, delivered, damaged)
4. WHEN a cargo item status is updated, THE Scheduling_API SHALL append a "cargo_status_changed" event to the Job_Events_Index with the item_id, old status, and new status
5. THE Scheduling_API SHALL expose a GET `/scheduling/cargo/search` endpoint that searches cargo items across all jobs by container_number, description, or item_status
6. WHEN all cargo items in a manifest reach "delivered" status, THE Scheduling_API SHALL emit a WebSocket notification indicating the cargo delivery is complete

### Requirement 7: ETA Calculation and Delay Detection

**User Story:** As an operations manager, I want estimated arrival times calculated for active jobs and automatic delay detection, so that I can proactively manage late operations and inform stakeholders.

#### Acceptance Criteria

1. WHEN a job transitions to "in_progress", THE Scheduling_API SHALL calculate an initial estimated_arrival based on the scheduled_time and store it in the Jobs_Current_Index
2. THE Scheduling_API SHALL expose a GET `/scheduling/jobs/{job_id}/eta` endpoint that returns the current estimated_arrival for a job
3. WHEN the current time exceeds a job's estimated_arrival and the job status is "in_progress", THE Scheduling_API SHALL mark the job as delayed by setting a `delayed` boolean field to true in the Jobs_Current_Index
4. WHEN a job becomes delayed, THE Scheduling_API SHALL emit a WebSocket notification with the job_id, job_type, asset_assigned, and delay duration
5. THE Scheduling_API SHALL expose a GET `/scheduling/metrics/delays` endpoint that returns delay statistics: count of delayed jobs, average delay duration, and delayed jobs grouped by job_type
6. WHEN a delayed job transitions to "completed", THE Scheduling_API SHALL record the actual delay duration in the completion event

### Requirement 8: Tenant-Scoped Job Access

**User Story:** As a security engineer, I want every scheduling API query automatically scoped to the requesting tenant, so that tenants cannot access other tenants' job data.

#### Acceptance Criteria

1. WHEN any Scheduling_API endpoint is called, THE Tenant_Guard SHALL extract the tenant_id from the authenticated JWT token and inject a tenant_id filter into every Elasticsearch query
2. IF a request does not contain a valid tenant_id in the JWT claims, THEN THE Tenant_Guard SHALL reject the request with a 403 status
3. THE Tenant_Guard SHALL apply tenant scoping to all job read endpoints, job mutation endpoints, cargo queries, and metrics endpoints
4. THE Tenant_Guard SHALL ignore any tenant_id provided in query parameters or unsigned request body fields to prevent tenant spoofing
5. WHEN a job is created, THE Scheduling_API SHALL set the tenant_id from the authenticated request context, not from the request body

### Requirement 9: WebSocket Live Updates for Job Changes

**User Story:** As a frontend developer, I want WebSocket channels for job state changes and delay alerts, so that the scheduling dashboard updates in real time without polling.

#### Acceptance Criteria

1. THE Backend_Service SHALL expose a WebSocket endpoint at `/ws/scheduling` that streams job state change events to connected clients
2. WHEN a job document is created or updated in the Jobs_Current_Index, THE Backend_Service SHALL broadcast the updated job data to all connected WebSocket clients subscribed to job updates
3. THE WebSocket endpoint SHALL support subscription filters allowing clients to subscribe to specific event types (job_created, status_changed, delay_alert, cargo_update)
4. WHEN a job becomes delayed, THE Backend_Service SHALL broadcast a delay_alert event with job details and delay duration to all subscribed clients
5. WHEN the WebSocket connection drops, THE Scheduling_Dashboard SHALL automatically reconnect with exponential backoff starting at 1 second with a maximum interval of 30 seconds
6. THE WebSocket endpoint SHALL send heartbeat messages every 30 seconds to keep connections alive

### Requirement 10: Unified Operations Control Dashboard

**User Story:** As an operations commander, I want a single command center view that shows active jobs, asset locations, fuel levels, and delayed operations together, so that I can make informed dispatch decisions from one screen.

#### Acceptance Criteria

1. THE Scheduling_Dashboard SHALL display an operations control page at route `/ops/control` that combines data from the Jobs_Current_Index, Assets_Index, and Fuel_Stations_Index
2. THE Operations_Control_View SHALL display a summary bar showing: total active jobs, delayed jobs count, available assets count, and fuel alerts count
3. THE Operations_Control_View SHALL display a map overlay showing asset locations (from the Assets_Index) with job assignment indicators (color-coded by job status)
4. THE Operations_Control_View SHALL display a job queue panel showing upcoming scheduled and assigned jobs sorted by scheduled_time
5. THE Operations_Control_View SHALL display a delayed operations panel highlighting jobs that have exceeded their estimated_arrival with delay duration
6. THE Operations_Control_View SHALL display a fuel status sidebar showing stations with low or critical fuel levels from the Fuel_Stations_Index
7. WHEN any underlying data changes (job status, asset location, fuel level), THE Operations_Control_View SHALL update the affected panel within 5 seconds via WebSocket without requiring a page refresh

### Requirement 11: Job Board Dashboard

**User Story:** As a dispatch operator, I want a job board showing all jobs with filtering, sorting, and status color-coding, so that I can manage the daily job schedule efficiently.

#### Acceptance Criteria

1. THE Scheduling_Dashboard SHALL display a job board page at route `/ops/scheduling` showing all jobs with columns for job_id, job_type, status, origin, destination, asset_assigned, scheduled_time, and estimated_arrival
2. THE Scheduling_Dashboard SHALL color-code job rows by status: blue for scheduled, orange for assigned, green for in_progress, gray for completed, red for failed, yellow for delayed
3. THE Scheduling_Dashboard SHALL support filtering the job board by job_type, status, date range, and asset_assigned using filter controls
4. THE Scheduling_Dashboard SHALL support sorting the job board by any column in ascending or descending order
5. THE Scheduling_Dashboard SHALL display a summary bar showing counts of jobs by status (total, scheduled, assigned, in_progress, completed, failed, delayed)
6. WHEN a job status changes, THE Scheduling_Dashboard SHALL update the affected row within 5 seconds via WebSocket push without requiring a page refresh
7. THE Scheduling_Dashboard SHALL provide action buttons on each job row for status transitions (start, complete, cancel, fail) based on the current status and valid transitions

### Requirement 12: Cargo Tracking View

**User Story:** As a cargo operations coordinator, I want a cargo tracking page showing the manifest and item-level status for transport jobs, so that I can verify loading, transit, and delivery of individual cargo items.

#### Acceptance Criteria

1. THE Scheduling_Dashboard SHALL display a cargo tracking page at route `/ops/scheduling/{job_id}/cargo` showing the cargo manifest for a cargo_transport job
2. THE Scheduling_Dashboard SHALL render each cargo item with item_id, description, weight_kg, container_number, seal_number, and item_status with status color-coding
3. THE Scheduling_Dashboard SHALL display the job header with job_id, origin, destination, asset_assigned, and overall job status
4. THE Scheduling_Dashboard SHALL provide action buttons to update individual cargo item statuses (mark as loaded, in_transit, delivered, damaged)
5. WHEN a cargo item status changes, THE Scheduling_Dashboard SHALL update the item row within 5 seconds via WebSocket push

### Requirement 13: Scheduling Metrics and Analytics

**User Story:** As an operations analyst, I want scheduling metrics with job completion rates, delay trends, and asset utilization, so that I can identify bottlenecks and optimize dispatch operations.

#### Acceptance Criteria

1. THE Scheduling_API SHALL expose a GET `/scheduling/metrics/jobs` endpoint that returns job counts aggregated by status and job_type in configurable time buckets (hourly or daily)
2. THE Scheduling_API SHALL expose a GET `/scheduling/metrics/completion` endpoint that returns job completion rate (completed / total) and average completion time grouped by job_type
3. THE Scheduling_API SHALL expose a GET `/scheduling/metrics/assets` endpoint that returns asset utilization metrics: jobs per asset, idle time, and active assignment hours grouped by asset_type
4. WHEN a metrics endpoint is called with a time range exceeding 90 days, THE Scheduling_API SHALL enforce daily bucket granularity to limit response size
5. THE Scheduling_API SHALL support a `bucket` query parameter accepting values `hourly` or `daily` with a default of `hourly`

### Requirement 14: AI Tools for Scheduling Queries and Reports

**User Story:** As an operations manager, I want the AI assistant to query job schedules, find available assets, and generate dispatch reports, so that I can get scheduling insights through natural language.

#### Acceptance Criteria

1. THE AI_Scheduling_Tools SHALL include a `search_jobs` tool that queries the Jobs_Current_Index with support for job_type, status, asset, origin, destination, and time range filters
2. THE AI_Scheduling_Tools SHALL include a `get_job_details` tool that returns a single job with its full event history and cargo manifest
3. THE AI_Scheduling_Tools SHALL include a `find_available_assets` tool that queries the Assets_Index for assets not assigned to active jobs within a specified time window, filtered by asset_type
4. THE AI_Scheduling_Tools SHALL include a `get_scheduling_summary` tool that returns a summary of active jobs, delayed jobs, available assets, and upcoming scheduled jobs
5. THE AI_Scheduling_Tools SHALL include a `generate_dispatch_report` tool that produces a markdown report covering job completion rates, delay analysis, asset utilization, and recommendations for a specified time range
6. WHEN an AI scheduling tool is invoked, THE AI_Scheduling_Tools SHALL enforce the same tenant scoping as the Scheduling_API to prevent cross-tenant data access
7. THE AI_Scheduling_Tools SHALL operate in read-only mode and SHALL NOT modify job data or assignments

### Requirement 15: Job Event History and Audit Trail

**User Story:** As an operations auditor, I want a complete event history for every job showing all status changes, assignments, and cargo updates with timestamps and actor information, so that I can trace the full lifecycle of any job for compliance and dispute resolution.

#### Acceptance Criteria

1. THE Job_Events_Index SHALL store every job lifecycle event with fields: event_id, job_id, event_type, actor_id, event_timestamp, tenant_id, and event_payload
2. THE Scheduling_API SHALL expose a GET `/scheduling/jobs/{job_id}/events` endpoint that returns the complete event timeline for a job sorted by event_timestamp ascending
3. WHEN any mutation occurs on a job (creation, assignment, status change, cargo update, cancellation), THE Scheduling_API SHALL append an event to the Job_Events_Index before returning the response
4. THE event_payload SHALL contain the full before and after state for status changes, and the specific fields modified for other mutations
5. FOR ALL valid job event sequences, serializing then deserializing the event_payload SHALL produce an equivalent object (round-trip property for event serialization)
