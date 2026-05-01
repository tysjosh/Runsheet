# Implementation Plan: Frontend Feature Parity

## Overview

This plan closes every gap between the Runsheet backend API surface and the frontend UI. Tasks are ordered: API client extensions first, then new page components, then navigation wiring, then enhancements to existing pages, and finally dead code cleanup. All new pages are lazy-loaded components rendered inside the existing `page.tsx` main layout. All API client functions extend existing service files (`fuelApi.ts`, `agentApi.ts`, `opsApi.ts`, `schedulingApi.ts`).

## Tasks

- [x] 1. Extend fuelApi.ts with MVP pipeline and station CRUD functions
  - [x] 1.1 Add fuel distribution MVP types and API functions to fuelApi.ts
    - Add TypeScript interfaces: `GeneratePlanResponse`, `ReplanRequest`, `ForecastFilters`, `PaginationFilters`, `PlanDetail`, `LoadingPlan`, `CompartmentAssignment`, `RoutePlan`, `RouteAssignment`, `Forecast`, `DeliveryPriority`, `ReplanResponse`
    - Add functions: `generatePlan(tenantId)` → POST `/api/fuel/mvp/plan/generate`, `getPlan(planId, tenantId)` → GET `/api/fuel/mvp/plan/{id}`, `replan(planId, body, tenantId)` → POST `/api/fuel/mvp/plan/{id}/replan`, `getForecasts(filters)` → GET `/api/fuel/mvp/forecasts`, `getPriorities(filters)` → GET `/api/fuel/mvp/priorities`
    - Follow existing `fuelRequest` / `buildQueryString` / `fetchWithTimeout` pattern
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x] 1.2 Add fuel station create/edit/threshold API functions to fuelApi.ts
    - Add TypeScript interfaces: `CreateStationPayload`, `UpdateStationPayload`
    - Add functions: `createStation(data, tenantId)` → POST `/fuel/stations`, `updateStation(stationId, data, tenantId)` → PATCH `/fuel/stations/{id}`, `updateStationThreshold(stationId, threshold, tenantId)` → PATCH `/fuel/stations/{id}/threshold`
    - _Requirements: 8.2, 8.4, 8.5_

  - [ ]* 1.3 Write unit tests for fuelApi.ts MVP and station CRUD functions
    - Test each new function calls the correct endpoint with correct method and parameters
    - Test error handling for API failures
    - _Requirements: 2.1–2.6, 8.2, 8.4, 8.5_

- [x] 2. Extend agentApi.ts with autonomy and memory management functions
  - [x] 2.1 Add autonomy and memory types and API functions to agentApi.ts
    - Add TypeScript types/interfaces: `AutonomyLevel`, `AutonomyUpdateResponse`, `MemoryEntry`, `MemoryFilters`, `PaginatedMemories`
    - Add functions: `getAutonomyLevel(tenantId)` → GET `/api/agent/config/autonomy`, `updateAutonomyLevel(level, tenantId)` → PATCH `/api/agent/config/autonomy`, `getMemories(filters)` → GET `/api/agent/memory`, `deleteMemory(memoryId, tenantId)` → DELETE `/api/agent/memory/{id}`
    - Follow existing `agentRequest` / `buildQueryString` / `fetchWithTimeout` pattern
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [ ]* 2.2 Write unit tests for agentApi.ts autonomy and memory functions
    - Test each new function calls the correct endpoint with correct method and parameters
    - Test error handling for 403 and 404 responses
    - _Requirements: 5.1–5.4_

- [x] 3. Checkpoint — Verify all API client extensions compile
  - Ensure all TypeScript types and functions compile without errors, ask the user if questions arise.

- [x] 4. Build FuelDistributionPage component
  - [x] 4.1 Create FuelDistributionPage with tabbed layout (Plans, Forecasts, Priorities)
    - Create `src/components/ops/FuelDistributionPage.tsx`
    - Implement Plans tab: "Generate Plan" button calling `generatePlan`, display plan status and run_id, plan list
    - Implement plan detail view: display loading plan and route plan details via `getPlan`
    - Implement ReplanForm modal: fields for disruption_type, description, entity_id; submit via `replan`
    - Implement Forecasts tab: paginated table with station_id and fuel_grade filters via `getForecasts`
    - Implement Priorities tab: paginated table of delivery priority rankings via `getPriorities`
    - Handle API errors: display error message, retain form state
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [ ]* 4.2 Write unit tests for FuelDistributionPage
    - Test plan generation triggers correct API call and displays run_id
    - Test replan form submission and error handling
    - Test forecasts and priorities tabs render paginated data
    - _Requirements: 1.1–1.7_

- [x] 5. Build AgentSettingsPage component
  - [x] 5.1 Create AgentSettingsPage with autonomy config and memory management sections
    - Create `src/components/ops/AgentSettingsPage.tsx`
    - Autonomy section: display current level via `getAutonomyLevel`, four radio options (suggest-only, auto-low, auto-medium, full-auto) with descriptions, confirm button calling `updateAutonomyLevel`, display previous and new levels on success
    - Read-only mode for non-admin users with "admin access required" message
    - Handle 403 errors: display access denied without modifying displayed level
    - Memory section: paginated list via `getMemories`, filter by memory_type and tags, delete button with confirmation dialog calling `deleteMemory`, remove entry from list on success without full reload
    - Handle 404 on delete: display "memory not found" message
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 4.4, 4.5_

  - [ ]* 5.2 Write unit tests for AgentSettingsPage
    - Test autonomy level display and update flow
    - Test read-only mode for non-admin users
    - Test memory list rendering, filtering, and deletion
    - Test error handling for 403 and 404 responses
    - _Requirements: 3.1–3.5, 4.1–4.5_

- [x] 6. Build OpsMonitoringDashboard component
  - [x] 6.1 Create OpsMonitoringDashboard with three metric cards and auto-refresh
    - Create `src/components/ops/OpsMonitoringDashboard.tsx`
    - Ingestion card: display events_received, events_processed, events_failed, avg_latency_ms via existing `getIngestionMonitoring`
    - Indexing card: display documents_indexed, indexing_errors, bulk_success_rate, avg_latency_ms via existing `getIndexingMonitoring`
    - Poison queue card: display queue_depth, oldest_event_age_seconds, pending_count, permanently_failed_count via existing `getPoisonQueueMonitoring`
    - Implement `getMetricStatus` helper: color-code values green (healthy), yellow (degraded), red (critical) based on thresholds
    - Display visual alert indicator next to metrics exceeding critical thresholds
    - Auto-refresh every 30 seconds via `setInterval` + `useEffect`; show "Last updated X seconds ago" on polling failure
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [ ]* 6.2 Write property test for metric threshold classification (Property 1)
    - **Property 1: Monitoring metric threshold classification is consistent**
    - For any non-negative metric value, `getMetricStatus` returns "healthy" below warning, "degraded" between warning and critical, "critical" above critical
    - **Validates: Requirements 6.4, 6.5**

  - [ ]* 6.3 Write unit tests for OpsMonitoringDashboard
    - Test all three metric cards render correct values
    - Test color-coding matches threshold rules
    - Test auto-refresh polling behavior
    - _Requirements: 6.1–6.6_

- [x] 7. Build SchedulingMetricsPage component
  - [x] 7.1 Create SchedulingMetricsPage with four metric sections and shared time filters
    - Create `src/components/ops/SchedulingMetricsPage.tsx`
    - Shared time range filters: bucket granularity, start_date, end_date — apply to all sections
    - Job Metrics section: display job counts by status and type in time buckets via existing `getJobMetrics`
    - Completion Rates section: display completion_rate and avg_completion_minutes per job_type via existing `getCompletionMetrics`
    - Asset Utilization section: table with total_jobs, active_jobs, idle_hours per asset via existing `getAssetUtilization`
    - Delay Statistics section: summary cards for total_delayed, avg_delay_minutes, delays_by_type via existing `getDelayMetrics`
    - Re-fetch all metrics when time range changes
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

  - [ ]* 7.2 Write unit tests for SchedulingMetricsPage
    - Test all four sections render correct data
    - Test time range filter changes trigger re-fetch
    - _Requirements: 10.1–10.6_

- [x] 8. Checkpoint — Verify all new page components compile and render
  - Ensure all new components compile without errors, ask the user if questions arise.

- [x] 9. Wire navigation and lazy-loading for new pages
  - [x] 9.1 Add new menu items to Sidebar.tsx
    - Add entries: `fuel-distribution` (Fuel Distribution, `Droplets` icon), `agent-settings` (Agent Settings, `Settings` icon), `ops-monitoring` (Ops Monitoring, `Activity` icon), `scheduling-metrics` (Scheduling Metrics, `TrendingUp` icon)
    - Import new icons from `lucide-react`
    - _Requirements: 12.1_

  - [x] 9.2 Add lazy-loaded routes in page.tsx renderMainContent switch
    - Add `lazy()` imports for `FuelDistributionPage`, `AgentSettingsPage`, `OpsMonitoringDashboard`, `SchedulingMetricsPage`
    - Add switch cases wrapping each in `ErrorBoundary` + `Suspense` + `ComponentLoadingPlaceholder`, following the existing pattern
    - _Requirements: 12.2, 12.3, 12.4_

  - [ ]* 9.3 Write unit tests for navigation wiring
    - Test sidebar renders new menu items
    - Test clicking each new item renders the correct lazy-loaded component
    - _Requirements: 12.1–12.4_

- [x] 10. Enhance Failure Analytics page with drill-down, type filter, and CSV export
  - [x] 10.1 Add drill-down panel to failures page
    - When user clicks a failed shipment row, open a side panel showing full shipment detail and event timeline fetched via `getShipmentById`
    - _Requirements: 9.1_

  - [x] 10.2 Add failure type dropdown filter
    - Add a dropdown filter for `failure_reason` values that filters both charts and table
    - Re-query API with selected filter and update all displayed data
    - _Requirements: 9.2, 9.3_

  - [x] 10.3 Add CSV export functionality
    - Implement `generateFailureCSV` helper function that produces CSV from filtered failure data
    - CSV columns: shipment_id, failure_reason, rider_id, origin, destination, timestamp
    - "Export" button triggers browser download of the generated CSV file
    - _Requirements: 9.4, 9.5_

  - [ ]* 10.4 Write property test for CSV export round-trip (Property 3)
    - **Property 3: CSV export round-trip preserves failure data**
    - For any array of failure objects, `generateFailureCSV` produces CSV with correct headers and data rows
    - **Validates: Requirements 9.4, 9.5**

  - [ ]* 10.5 Write unit tests for failure analytics enhancements
    - Test drill-down panel opens and displays shipment detail
    - Test failure type filter updates charts and table
    - Test CSV export generates correct file content
    - _Requirements: 9.1–9.5_

- [x] 11. Add FuelStationForm component to existing fuel page
  - [x] 11.1 Create FuelStationForm modal component
    - Create `src/components/ops/FuelStationForm.tsx`
    - Fields: name, fuel_type (select), capacity_liters (number), location_name (text), alert_threshold_pct (number 0-100)
    - Client-side validation: capacity_liters > 0, alert_threshold_pct between 0 and 100
    - Create mode: calls `createStation`, adds new station to list
    - Edit mode: pre-populates with current values, calls `updateStation`
    - Threshold-only edit: calls `updateStationThreshold`
    - On API error: display error message, retain form values
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [x] 11.2 Integrate FuelStationForm into existing fuel dashboard page
    - Add "Add Station" button to fuel stations page that opens FuelStationForm in create mode
    - Add "Edit" button per station row that opens FuelStationForm in edit mode
    - _Requirements: 8.1, 8.3_

  - [ ]* 11.3 Write property test for fuel station form validation (Property 2)
    - **Property 2: Fuel station form validation accepts valid inputs and rejects invalid inputs**
    - For any positive capacity and threshold in [0,100], validation accepts; for invalid values, validation rejects
    - **Validates: Requirements 8.6**

  - [ ]* 11.4 Write unit tests for FuelStationForm
    - Test create and edit form rendering
    - Test validation rejects invalid inputs
    - Test API calls on submit and error handling
    - _Requirements: 8.1–8.7_

- [x] 12. Add cargo management edit UI and search to scheduling area
  - [x] 12.1 Create CargoManifestEditor component
    - Create `src/components/ops/CargoManifestEditor.tsx`
    - Wraps existing `CargoManifestView` with an edit mode toggle
    - In edit mode, cargo item fields become editable inputs
    - Submit calls existing `updateCargo` from schedulingApi.ts and displays updated manifest
    - Status change buttons per item call existing `updateCargoItemStatus`
    - On API error: display error message, revert form to previous state
    - _Requirements: 7.1, 7.2, 7.3, 7.6_

  - [x] 12.2 Create CargoSearchSection component
    - Create `src/components/ops/CargoSearchSection.tsx`
    - Search form with fields: container_number, description, item_status
    - Submit calls existing `searchCargo` from schedulingApi.ts
    - Display paginated results with associated job_id
    - _Requirements: 7.4, 7.5_

  - [x] 12.3 Integrate CargoManifestEditor and CargoSearchSection into scheduling page
    - Replace `CargoManifestView` usage with `CargoManifestEditor` in job detail views
    - Add cargo search as an accessible section from the scheduling area
    - _Requirements: 7.1, 7.4_

  - [ ]* 12.4 Write unit tests for cargo management components
    - Test edit mode toggle and form submission
    - Test status change buttons call correct API
    - Test cargo search form and results rendering
    - _Requirements: 7.1–7.6_

- [x] 13. Checkpoint — Verify all features compile and integrate correctly
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Dead code cleanup across API service files
  - [x] 14.1 Audit and clean up unused exports in all four service files
    - Verify every exported function in `schedulingApi.ts` is imported by at least one component
    - Verify every exported function in `opsApi.ts` is imported by at least one component
    - Verify every exported function in `agentApi.ts` is imported by at least one component
    - Verify every exported function in `fuelApi.ts` is imported by at least one component
    - Remove any functions and associated types that remain unused after all UI features are built
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

- [x] 15. Final checkpoint — Ensure all code compiles and integrates
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped — the user has requested no tests be added yet
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation at key integration points
- All new API functions extend existing service files using the established `fetchWithTimeout` / `buildQueryString` / typed-response pattern
- All new page components are lazy-loaded inside `page.tsx` with `ErrorBoundary` + `Suspense` wrappers
- No backend changes are required — all endpoints already exist
