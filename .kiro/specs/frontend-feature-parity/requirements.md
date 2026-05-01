# Requirements Document

## Introduction

This specification covers building all missing frontend UI for existing backend capabilities in the Runsheet logistics platform. The backend is significantly ahead of the frontend — several modules have full REST APIs with zero frontend coverage, while others have partial pages missing key features. This spec closes every gap so users can access all platform capabilities through the UI.

## Glossary

- **Fuel_Distribution_Page**: The frontend page for the Fuel Distribution MVP pipeline, providing plan generation, plan viewing, replanning, forecasts, and delivery priorities
- **Agent_Settings_Page**: The frontend settings page for configuring agent autonomy levels and managing agent memory
- **Ops_Monitoring_Dashboard**: The frontend dashboard displaying ingestion health, indexing health, and poison queue metrics
- **Cargo_Management_UI**: The frontend components for editing cargo manifests, changing cargo item statuses, and searching cargo across jobs
- **Fuel_Station_Form**: The frontend form component for creating and editing fuel stations and their alert thresholds
- **Failure_Analytics_Page**: The existing /ops/failures page enhanced with drill-down, type filtering, and export capabilities
- **Scheduling_Metrics_Page**: The frontend analytics page displaying job counts, completion rates, asset utilization, and delay statistics
- **API_Client**: The typed TypeScript service modules (fuelApi.ts, schedulingApi.ts, opsApi.ts, agentApi.ts) that call backend REST endpoints
- **Dead_Code**: API client functions defined in frontend service files but never imported or called from any component

## Requirements

### Requirement 1: Fuel Distribution MVP Pipeline Page

**User Story:** As an operations manager, I want to trigger fuel distribution plans, view forecasts, and see delivery priorities from the UI, so that I can manage fuel logistics without using raw API calls.

#### Acceptance Criteria

1. WHEN the user navigates to the fuel distribution page, THE Fuel_Distribution_Page SHALL display a "Generate Plan" button that triggers POST /api/fuel/mvp/plan/generate
2. WHEN a plan is generated, THE Fuel_Distribution_Page SHALL display the plan status and run_id returned from the API
3. WHEN the user selects a plan, THE Fuel_Distribution_Page SHALL display the loading plan and route plan details retrieved via GET /api/fuel/mvp/plan/{id}
4. WHEN the user triggers replanning on an existing plan, THE Fuel_Distribution_Page SHALL present a form for disruption_type, description, and entity_id and submit via POST /api/fuel/mvp/plan/{id}/replan
5. WHEN the user views the forecasts tab, THE Fuel_Distribution_Page SHALL display paginated tank forecasts retrieved via GET /api/fuel/mvp/forecasts with optional station_id and fuel_grade filters
6. WHEN the user views the priorities tab, THE Fuel_Distribution_Page SHALL display paginated delivery priority rankings retrieved via GET /api/fuel/mvp/priorities
7. IF the plan generation API returns an error, THEN THE Fuel_Distribution_Page SHALL display the error message to the user and retain the form state

### Requirement 2: Fuel Distribution API Client

**User Story:** As a developer, I want typed API client functions for the fuel distribution MVP endpoints, so that frontend components can call them with type safety.

#### Acceptance Criteria

1. THE API_Client SHALL export a `generatePlan(tenantId: string)` function that calls POST /api/fuel/mvp/plan/generate
2. THE API_Client SHALL export a `getPlan(planId: string, tenantId: string)` function that calls GET /api/fuel/mvp/plan/{plan_id}
3. THE API_Client SHALL export a `replan(planId: string, body: ReplanRequest, tenantId: string)` function that calls POST /api/fuel/mvp/plan/{plan_id}/replan
4. THE API_Client SHALL export a `getForecasts(filters: ForecastFilters)` function that calls GET /api/fuel/mvp/forecasts
5. THE API_Client SHALL export a `getPriorities(filters: PaginationFilters)` function that calls GET /api/fuel/mvp/priorities
6. THE API_Client SHALL follow the existing pattern in fuelApi.ts using fetchWithTimeout, buildQueryString, and typed response generics

### Requirement 3: Agent Autonomy Configuration Page

**User Story:** As a tenant administrator, I want to configure the agent autonomy level from the UI, so that I can control how much autonomous action the AI agents take.

#### Acceptance Criteria

1. WHEN the user navigates to agent settings, THE Agent_Settings_Page SHALL display the current autonomy level for the tenant
2. WHEN the user selects a new autonomy level, THE Agent_Settings_Page SHALL present four options: suggest-only, auto-low, auto-medium, and full-auto with descriptions of each level
3. WHEN the user confirms the autonomy level change, THE Agent_Settings_Page SHALL submit via PATCH /api/agent/config/autonomy and display the previous and new levels
4. IF the user does not have admin role, THEN THE Agent_Settings_Page SHALL display the current level as read-only with a message indicating admin access is required
5. IF the API returns a 403 error, THEN THE Agent_Settings_Page SHALL display an access denied message without modifying the displayed level

### Requirement 4: Agent Memory Management Page

**User Story:** As a tenant administrator, I want to view and delete agent memories from the UI, so that I can manage what the AI agents have learned.

#### Acceptance Criteria

1. WHEN the user navigates to agent memory management, THE Agent_Settings_Page SHALL display a paginated list of memories retrieved via GET /api/agent/memory
2. THE Agent_Settings_Page SHALL support filtering memories by memory_type (pattern or preference) and by tags
3. WHEN the user clicks delete on a memory entry, THE Agent_Settings_Page SHALL confirm the action and then call DELETE /api/agent/memory/{id}
4. WHEN a memory is successfully deleted, THE Agent_Settings_Page SHALL remove the entry from the displayed list without a full page reload
5. IF the delete API returns 404, THEN THE Agent_Settings_Page SHALL display a message that the memory was not found

### Requirement 5: Agent API Client Extensions

**User Story:** As a developer, I want typed API client functions for autonomy configuration and memory management, so that the new settings components can call them with type safety.

#### Acceptance Criteria

1. THE API_Client SHALL export an `updateAutonomyLevel(level: string, tenantId: string)` function that calls PATCH /api/agent/config/autonomy
2. THE API_Client SHALL export a `getMemories(filters: MemoryFilters)` function that calls GET /api/agent/memory
3. THE API_Client SHALL export a `deleteMemory(memoryId: string, tenantId: string)` function that calls DELETE /api/agent/memory/{id}
4. THE API_Client SHALL define TypeScript interfaces for AutonomyLevel, MemoryEntry, and MemoryFilters matching the backend Pydantic models

### Requirement 6: Ops Monitoring Dashboard

**User Story:** As a platform operator, I want a monitoring dashboard showing ingestion health, indexing health, and poison queue metrics, so that I can detect and respond to data pipeline issues.

#### Acceptance Criteria

1. WHEN the user navigates to the ops monitoring page, THE Ops_Monitoring_Dashboard SHALL display ingestion metrics (events_received, events_processed, events_failed, avg_latency_ms) retrieved via GET /ops/monitoring/ingestion
2. THE Ops_Monitoring_Dashboard SHALL display indexing metrics (documents_indexed, indexing_errors, bulk_success_rate, avg_latency_ms) retrieved via GET /ops/monitoring/indexing
3. THE Ops_Monitoring_Dashboard SHALL display poison queue metrics (queue_depth, oldest_event_age_seconds, pending_count, permanently_failed_count) retrieved via GET /ops/monitoring/poison-queue
4. THE Ops_Monitoring_Dashboard SHALL color-code metric values using green for healthy, yellow for degraded, and red for critical thresholds
5. WHEN any metric exceeds a critical threshold, THE Ops_Monitoring_Dashboard SHALL display a visual alert indicator next to the affected metric
6. THE Ops_Monitoring_Dashboard SHALL auto-refresh metrics every 30 seconds using a polling interval

### Requirement 7: Cargo Management Edit UI

**User Story:** As a logistics coordinator, I want to edit cargo manifests, change item statuses, and search cargo from the UI, so that I can manage cargo without API calls.

#### Acceptance Criteria

1. WHEN the user views a cargo manifest, THE Cargo_Management_UI SHALL display an "Edit" button that opens an editable form for the cargo items
2. WHEN the user submits cargo manifest changes, THE Cargo_Management_UI SHALL call the updateCargo API function and display the updated manifest
3. WHEN the user clicks a status change button on a cargo item, THE Cargo_Management_UI SHALL present valid status options and call updateCargoItemStatus on selection
4. WHEN the user navigates to cargo search, THE Cargo_Management_UI SHALL display a search form with fields for container_number, description, and item_status
5. WHEN the user submits a cargo search, THE Cargo_Management_UI SHALL call the searchCargo API function and display paginated results with the associated job_id
6. IF the cargo update API returns an error, THEN THE Cargo_Management_UI SHALL display the error message and revert the form to the previous state

### Requirement 8: Fuel Station Create/Edit Forms

**User Story:** As a fuel operations manager, I want to create new fuel stations and edit existing ones from the UI, so that I can manage the station network without API calls.

#### Acceptance Criteria

1. WHEN the user clicks "Add Station" on the fuel stations page, THE Fuel_Station_Form SHALL display a creation form with fields for name, fuel_type, capacity_liters, location, and alert_threshold_pct
2. WHEN the user submits the creation form, THE Fuel_Station_Form SHALL call POST /fuel/stations and add the new station to the displayed list
3. WHEN the user clicks "Edit" on a station, THE Fuel_Station_Form SHALL display a pre-populated edit form with the station's current values
4. WHEN the user submits the edit form, THE Fuel_Station_Form SHALL call PATCH /fuel/stations/{id} and update the displayed station data
5. WHEN the user edits the alert threshold, THE Fuel_Station_Form SHALL call PATCH /fuel/stations/{id}/threshold with the new alert_threshold_pct value
6. THE Fuel_Station_Form SHALL validate that capacity_liters is a positive number and alert_threshold_pct is between 0 and 100 before submission
7. IF the creation or update API returns an error, THEN THE Fuel_Station_Form SHALL display the error message and retain the form values

### Requirement 9: Failure Analytics Enhancements

**User Story:** As an operations analyst, I want to drill down into failure reasons, filter by failure type, and export failure data, so that I can perform root cause analysis.

#### Acceptance Criteria

1. WHEN the user clicks on a failed shipment row, THE Failure_Analytics_Page SHALL display a drill-down panel showing the full shipment detail and event timeline
2. THE Failure_Analytics_Page SHALL provide a failure type dropdown filter that filters both the charts and the table by specific failure_reason values
3. WHEN the user selects a failure type filter, THE Failure_Analytics_Page SHALL re-query the API with the selected filter and update all displayed data
4. WHEN the user clicks "Export", THE Failure_Analytics_Page SHALL generate a CSV file containing the currently filtered failure data and trigger a browser download
5. THE Failure_Analytics_Page SHALL include columns for shipment_id, failure_reason, rider_id, origin, destination, and timestamp in the export

### Requirement 10: Scheduling Metrics Analytics Page

**User Story:** As an operations manager, I want a scheduling analytics page showing job counts, completion rates, asset utilization, and delay statistics, so that I can monitor scheduling performance.

#### Acceptance Criteria

1. WHEN the user navigates to the scheduling metrics page, THE Scheduling_Metrics_Page SHALL display job count metrics by status and type in time buckets retrieved via the getJobMetrics API function
2. THE Scheduling_Metrics_Page SHALL display completion rate metrics (completion_rate, avg_completion_minutes per job_type) retrieved via the getCompletionMetrics API function
3. THE Scheduling_Metrics_Page SHALL display asset utilization metrics (total_jobs, active_jobs, idle_hours per asset) retrieved via the getAssetUtilization API function
4. THE Scheduling_Metrics_Page SHALL display delay statistics (total_delayed, avg_delay_minutes, delays_by_type) retrieved via the getDelayMetrics API function
5. THE Scheduling_Metrics_Page SHALL provide time range filters (bucket granularity, start_date, end_date) that apply to all four metrics sections
6. WHEN the user changes the time range, THE Scheduling_Metrics_Page SHALL re-fetch all metrics with the updated filters

### Requirement 11: Dead Code Cleanup

**User Story:** As a developer, I want unused API client functions either wired to UI components or removed, so that the codebase stays maintainable and free of dead code.

#### Acceptance Criteria

1. WHEN all new UI components are built, THE Dead_Code audit SHALL verify that every exported function in schedulingApi.ts is imported by at least one component
2. WHEN all new UI components are built, THE Dead_Code audit SHALL verify that every exported function in opsApi.ts is imported by at least one component
3. WHEN all new UI components are built, THE Dead_Code audit SHALL verify that every exported function in agentApi.ts is imported by at least one component
4. WHEN all new UI components are built, THE Dead_Code audit SHALL verify that every exported function in fuelApi.ts is imported by at least one component
5. IF an API function remains unused after all UI features are built, THEN THE Dead_Code cleanup SHALL remove the function and its associated types from the service file

### Requirement 12: Navigation and Routing

**User Story:** As a user, I want to access all new pages from the application sidebar and routing structure, so that I can discover and navigate to all platform features.

#### Acceptance Criteria

1. THE Sidebar SHALL include navigation entries for Fuel Distribution, Agent Settings, Ops Monitoring, and Scheduling Metrics
2. WHEN the user clicks a navigation entry, THE application SHALL route to the corresponding page without a full page reload
3. THE application SHALL use Next.js app router pages for new routes following the existing /ops/* pattern
4. THE application SHALL wrap each new page in ErrorBoundary and Suspense components following the existing lazy-loading pattern
