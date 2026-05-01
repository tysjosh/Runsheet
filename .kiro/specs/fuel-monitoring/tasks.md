# Implementation Plan: Fuel Monitoring

## Overview

This implementation plan builds the Fuel Monitoring module on top of the existing Runsheet platform infrastructure. Tasks are organized in dependency order: configuration and Elasticsearch indices first, then the core service layer, API endpoints, WebSocket alerts, AI tools, frontend dashboard, and finally testing.

## Tasks

- [x] 1. Configuration and package structure
  - [x] 1.1 Create fuel module package structure
    - Create `Runsheet-backend/fuel/` package with `__init__.py`
    - Create subpackages: `fuel/api/`, `fuel/services/`
    - Create `fuel/models.py` with Pydantic models (FuelStation, CreateFuelStation, ConsumptionEvent, RefillEvent, FuelAlert, FuelNetworkSummary)
    - _Requirements: 1.1-1.7, 2.1-2.7, 3.1-3.5_

  - [x] 1.2 Add fuel-specific settings to config/settings.py
    - Add `fuel_alert_default_threshold_pct` (default 20.0)
    - Add `fuel_consumption_rolling_window_days` (default 7)
    - Add `fuel_critical_days_threshold` (default 3)
    - Update `.env.development` and `.env.example` with new variables
    - _Requirements: 4.4, 5.1_

- [x] 2. Elasticsearch indices for fuel data
  - [x] 2.1 Create fuel_stations index mapping
    - Create `fuel/services/fuel_es_mappings.py` with strict mapping
    - Keyword fields: station_id, fuel_type, status, tenant_id
    - Float fields: capacity_liters, current_stock_liters, daily_consumption_rate, days_until_empty, alert_threshold_pct
    - Geo_point field: location
    - Text+keyword fields: name, location_name
    - Date fields: created_at, last_updated
    - Configure 1 primary shard, 1 replica
    - _Requirements: 8.1, 8.4_

  - [x] 2.2 Create fuel_events index mapping
    - Strict mapping with keyword fields: event_id, station_id, event_type, fuel_type, asset_id, operator_id, supplier, delivery_reference, tenant_id
    - Float fields: quantity_liters, odometer_reading
    - Date fields: event_timestamp, ingested_at
    - Configure ILM policy: warm after 30 days, cold after 90 days, delete after 365 days
    - _Requirements: 8.2, 8.3, 8.4_

  - [x] 2.3 Register fuel indices in application startup
    - Add `setup_fuel_indices()` call in main.py lifespan
    - Verify ILM policies are applied on startup
    - Log warnings for missing policies
    - _Requirements: 8.3_

- [x] 3. Core fuel service
  - [x] 3.1 Implement FuelService class
    - Create `fuel/services/fuel_service.py`
    - Constructor takes ElasticsearchService dependency
    - Implement `list_stations()` with filtering by fuel_type, status, location and pagination
    - Implement `get_station()` returning station detail with recent events
    - Implement `create_station()` with validation (capacity > 0, initial_stock <= capacity)
    - Implement `update_station()` for metadata updates (not stock)
    - _Requirements: 1.1-1.7_

  - [x] 3.2 Implement consumption recording
    - Implement `record_consumption()` in FuelService
    - Deduct quantity from current_stock_liters via ES update
    - Append consumption event to fuel_events index
    - Validate stock >= quantity before deducting (reject with 400 if insufficient)
    - Recalculate daily_consumption_rate from rolling 7-day window
    - Recalculate days_until_empty
    - Update station status based on new stock level
    - Implement `record_consumption_batch()` for bulk operations
    - _Requirements: 2.1-2.7_

  - [x] 3.3 Implement refill recording
    - Implement `record_refill()` in FuelService
    - Add quantity to current_stock_liters via ES update
    - Append refill event to fuel_events index
    - Validate stock + quantity <= capacity (reject with 400 if overflow)
    - Update station status based on new stock level
    - Clear active alerts if stock restored above threshold
    - _Requirements: 3.1-3.5_

  - [x] 3.4 Implement alert and threshold logic
    - Create `fuel/services/fuel_alert_service.py`
    - Implement `check_thresholds()` called after every stock change
    - Classify status: normal (above threshold), low (below threshold, above 10%), critical (below 10% OR days_until_empty < 3), empty (0)
    - Implement `get_alerts()` returning all stations with status != normal
    - Implement `update_threshold()` for per-station threshold configuration
    - _Requirements: 4.1-4.6_

  - [x] 3.5 Implement consumption analytics
    - Implement `get_consumption_metrics()` with ES date_histogram aggregation by bucket (hourly/daily/weekly)
    - Implement `get_efficiency_metrics()` calculating liters per km when odometer data available
    - Implement `get_network_summary()` aggregating across all stations
    - Enforce daily bucket for time ranges > 90 days
    - Support filtering by station_id, fuel_type, asset_id, date range
    - _Requirements: 5.1-5.5_

- [x] 4. Fuel API endpoints
  - [x] 4.1 Create fuel API router
    - Create `fuel/api/endpoints.py` with FastAPI router (prefix="/fuel")
    - Wire FuelService dependency
    - Apply rate limiting using existing limiter middleware
    - Apply tenant scoping using existing Tenant_Guard
    - _Requirements: 1.1-1.6, 2.1, 3.1_

  - [x] 4.2 Implement station management endpoints
    - GET `/fuel/stations` — list with filters (fuel_type, status, location)
    - GET `/fuel/stations/{station_id}` — station detail with events
    - POST `/fuel/stations` — register new station
    - PATCH `/fuel/stations/{station_id}` — update metadata
    - PATCH `/fuel/stations/{station_id}/threshold` — update alert threshold
    - _Requirements: 1.1-1.7, 4.4_

  - [x] 4.3 Implement fuel event endpoints
    - POST `/fuel/consumption` — record consumption event
    - POST `/fuel/consumption/batch` — batch consumption recording
    - POST `/fuel/refill` — record refill event
    - _Requirements: 2.1-2.7, 3.1-3.5_

  - [x] 4.4 Implement alert and metrics endpoints
    - GET `/fuel/alerts` — list active alerts
    - GET `/fuel/metrics/consumption` — consumption by time bucket
    - GET `/fuel/metrics/efficiency` — fuel efficiency per asset
    - GET `/fuel/metrics/summary` — network-wide summary
    - _Requirements: 4.1, 5.1-5.5_

  - [x] 4.5 Register fuel router in main.py
    - Import and include fuel router in FastAPI app
    - Initialize FuelService with existing elasticsearch_service
    - Call `configure_fuel_api()` during startup
    - _Requirements: 1.1_

- [x] 5. WebSocket fuel alerts
  - [x] 5.1 Extend OpsWebSocketManager for fuel alerts
    - Add `fuel_alert` subscription type to existing `/ws/ops` endpoint
    - Implement broadcast method for fuel stock status changes
    - Broadcast alert when stock falls below threshold
    - Broadcast alert clearance when stock restored above threshold
    - _Requirements: 2.6, 4.3_

- [x] 6. AI agent fuel tools
  - [x] 6.1 Create fuel AI tools
    - Create `Agents/tools/fuel_tools.py`
    - Implement `search_fuel_stations(query, fuel_type, status)` — search stations by name, type, location, status
    - Implement `get_fuel_summary()` — network-wide fuel summary
    - Implement `get_fuel_consumption_history(station_id, asset_id, days)` — consumption events over date range
    - Implement `generate_fuel_report(days)` — markdown report with stock levels, trends, alerts, recommendations
    - All tools read-only, tenant-scoped
    - Add telemetry logging for tool invocations
    - _Requirements: 7.1-7.6_

  - [x] 6.2 Register fuel tools with AI agent
    - Import fuel tools in `Agents/tools/__init__.py`
    - Add to ALL_TOOLS list
    - Update agent system prompt with fuel tool descriptions
    - _Requirements: 7.1_

- [x] 7. Frontend fuel dashboard
  - [x] 7.1 Create fuel API client
    - Create `runsheet/src/services/fuelApi.ts`
    - Implement typed functions: getStations, getStation, getAlerts, getConsumptionMetrics, getEfficiencyMetrics, getNetworkSummary
    - Reuse existing API_TIMEOUTS and error handling from api.ts
    - Define TypeScript interfaces: FuelStation, FuelAlert, FuelNetworkSummary, ConsumptionMetric
    - _Requirements: 6.1-6.7_

  - [x] 7.2 Create fuel dashboard components
    - Create `FuelSummaryBar.tsx` — network summary (total capacity, stock, alerts, avg days until empty)
    - Create `FuelStationList.tsx` — station list with stock percentage bars, status color-coding (green/yellow/red/gray), fuel type, location
    - Create `FuelConsumptionChart.tsx` — daily consumption trend chart with lines per fuel type
    - Create `FuelStationDetail.tsx` — station detail panel with stock history, recent events, daily rate trend
    - _Requirements: 6.1-6.6_

  - [x] 7.3 Create fuel dashboard page
    - Create `runsheet/src/app/ops/fuel/page.tsx`
    - Compose FuelSummaryBar, FuelStationList, FuelConsumptionChart, FuelStationDetail
    - Add filters for fuel_type, status, location
    - Subscribe to `fuel_alert` WebSocket events for real-time updates
    - Add route to sidebar navigation
    - _Requirements: 6.1-6.7_

- [x] 8. Testing
  - [x] 8.1 Write unit tests for FuelService
    - Test station CRUD operations with mocked Elasticsearch
    - Test consumption recording: stock deduction, rate recalculation, status update
    - Test refill recording: stock addition, overflow rejection, alert clearance
    - Test alert threshold logic: normal, low, critical, empty classification
    - Test days_until_empty calculation and critical escalation at < 3 days
    - Test consumption metrics aggregation
    - Test network summary calculation
    - _Requirements: 1.5, 2.4, 3.4, 4.2, 4.5, 4.6_

  - [x] 8.2 Write unit tests for fuel API endpoints
    - Test all endpoint response formats and status codes
    - Test input validation (negative quantities, overflow, missing fields)
    - Test tenant scoping (requests without tenant_id rejected)
    - Test rate limiting
    - _Requirements: 1.1-1.7, 2.1-2.7, 3.1-3.5_

  - [x] 8.3 Write unit tests for AI fuel tools
    - Test each tool returns structured results
    - Test tenant scoping enforcement
    - Test read-only constraint (no mutations)
    - _Requirements: 7.1-7.6_

  - [x] 8.4 Write integration tests
    - Test full consumption flow: create station → record consumption → verify stock update → verify alert
    - Test full refill flow: low stock station → record refill → verify alert cleared
    - Test metrics endpoints with seeded consumption data
    - Test WebSocket alert broadcasting
    - _Requirements: 2.6, 3.5, 4.3, 5.1-5.5_
