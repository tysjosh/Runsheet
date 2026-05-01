# Requirements Document

## Introduction

This document specifies the requirements for the Fuel Monitoring module, a fuel inventory tracking and consumption analytics system for the Runsheet logistics platform. The module tracks fuel stock levels across stations and depots, records consumption and refill events, generates low-stock alerts, and provides trend analytics for fuel usage. It extends the existing Elasticsearch-backed inventory infrastructure with fuel-specific data models, dedicated API endpoints, a frontend fuel dashboard, and AI assistant tools for fuel queries and reports. The system supports multiple fuel types (AGO/diesel, PMS/petrol, ATK/aviation fuel, LPG) across geographically distributed stations, with real-time stock updates and configurable alert thresholds.

## Glossary

- **Fuel_Station**: A physical location that stores and dispenses fuel, identified by a unique station_id. Includes bulk fuel dumps, industrial fuel stations, depot tanks, and mobile refueling units.
- **Fuel_Type**: The classification of fuel stored at a station. Supported types: AGO (Automotive Gas Oil / diesel), PMS (Premium Motor Spirit / petrol), ATK (Aviation Turbine Kerosene), LPG (Liquefied Petroleum Gas).
- **Fuel_Stock**: The current quantity of a specific fuel type at a specific station, measured in liters.
- **Consumption_Event**: A recorded instance of fuel being dispensed from a station, capturing quantity, recipient asset, timestamp, and operator.
- **Refill_Event**: A recorded instance of fuel being delivered to a station, capturing quantity, supplier, timestamp, and delivery reference.
- **Alert_Threshold**: A configurable percentage of station capacity below which a low-stock alert is triggered. Default: 20%.
- **Fuel_API**: The set of FastAPI endpoints that expose fuel station data, stock levels, consumption history, and alerts.
- **Fuel_Dashboard**: The frontend Next.js page providing fuel stock overview, station details, consumption trends, and alert management.
- **AI_Fuel_Tools**: The set of AI agent tool functions that query fuel data for the Runsheet AI assistant.
- **Fuel_Stations_Index**: The Elasticsearch index holding the current state of each fuel station including stock levels, keyed by a composite of station_id and fuel_type.
- **Fuel_Events_Index**: The Elasticsearch append-only index storing consumption and refill event history, keyed by event_id.
- **Daily_Consumption_Rate**: The calculated average daily fuel usage for a station and fuel type, derived from consumption events over a rolling window.
- **Days_Until_Empty**: The estimated number of days before a station's fuel stock reaches zero, calculated as current_stock divided by daily_consumption_rate.

## Requirements

### Requirement 1: Fuel Station Registry and Stock Tracking

**User Story:** As a fuel operations manager, I want to register fuel stations with their capacity and current stock levels, so that I have a centralized view of all fuel assets across the logistics network.

#### Acceptance Criteria

1. THE Fuel_API SHALL expose a GET `/fuel/stations` endpoint that returns all registered fuel stations with their current stock levels, capacity, fuel type, and location
2. THE Fuel_API SHALL expose a GET `/fuel/stations/{station_id}` endpoint that returns a single station with full details including stock history and recent events
3. THE Fuel_API SHALL expose a POST `/fuel/stations` endpoint that registers a new fuel station with station_id, name, location, fuel_type, capacity_liters, and initial_stock_liters
4. THE Fuel_API SHALL expose a PATCH `/fuel/stations/{station_id}` endpoint that updates station metadata (name, location, capacity, alert_threshold) without modifying stock levels
5. WHEN a fuel station is registered, THE Fuel_API SHALL validate that capacity_liters is a positive number and initial_stock_liters does not exceed capacity_liters
6. THE Fuel_API SHALL support filtering the `/fuel/stations` endpoint by fuel_type, location, and stock status (normal, low, critical, empty)
7. THE Fuel_Stations_Index SHALL store each station record with fields: station_id, name, fuel_type, capacity_liters, current_stock_liters, daily_consumption_rate, days_until_empty, alert_threshold_pct, location (geo_point), status, tenant_id, last_updated

### Requirement 2: Fuel Consumption Recording

**User Story:** As a fuel station operator, I want to record fuel dispensing events with the recipient asset and quantity, so that consumption is tracked accurately and stock levels stay current.

#### Acceptance Criteria

1. THE Fuel_API SHALL expose a POST `/fuel/consumption` endpoint that records a fuel dispensing event with station_id, fuel_type, quantity_liters, asset_id (truck/boat/vehicle receiving fuel), operator_id, and optional odometer_reading
2. WHEN a consumption event is recorded, THE Fuel_API SHALL deduct the quantity_liters from the station's current_stock_liters in the Fuel_Stations_Index
3. WHEN a consumption event is recorded, THE Fuel_API SHALL append an event document to the Fuel_Events_Index with event_type "consumption", all event fields, and a generated event_id
4. IF the requested quantity_liters exceeds the station's current_stock_liters, THEN THE Fuel_API SHALL reject the request with a 400 status and a descriptive error
5. WHEN a consumption event is recorded, THE Fuel_API SHALL recalculate the station's daily_consumption_rate based on the rolling 7-day consumption window
6. WHEN a consumption event causes the stock level to fall below the station's alert_threshold_pct of capacity, THE Fuel_API SHALL emit a low-stock alert via WebSocket to connected dashboard clients
7. THE Fuel_API SHALL support a POST `/fuel/consumption/batch` endpoint for recording multiple dispensing events in a single request

### Requirement 3: Fuel Refill Recording

**User Story:** As a fuel supply coordinator, I want to record fuel deliveries to stations, so that stock levels are updated and refill history is maintained for auditing.

#### Acceptance Criteria

1. THE Fuel_API SHALL expose a POST `/fuel/refill` endpoint that records a fuel delivery event with station_id, fuel_type, quantity_liters, supplier, delivery_reference, and operator_id
2. WHEN a refill event is recorded, THE Fuel_API SHALL add the quantity_liters to the station's current_stock_liters in the Fuel_Stations_Index
3. WHEN a refill event is recorded, THE Fuel_API SHALL append an event document to the Fuel_Events_Index with event_type "refill" and all event fields
4. IF the refill would cause current_stock_liters to exceed capacity_liters, THEN THE Fuel_API SHALL reject the request with a 400 status indicating overflow
5. WHEN a refill event restores stock above the alert threshold, THE Fuel_API SHALL clear any active low-stock alert for that station and fuel type

### Requirement 4: Fuel Alerts and Thresholds

**User Story:** As a logistics operations manager, I want configurable low-stock alerts for each fuel station, so that I am notified before stations run out of fuel and can schedule refills proactively.

#### Acceptance Criteria

1. THE Fuel_API SHALL expose a GET `/fuel/alerts` endpoint that returns all active fuel alerts across all stations
2. THE Fuel_API SHALL classify stock status as: "normal" (above threshold), "low" (below threshold but above 10%), "critical" (below 10%), "empty" (at 0)
3. WHEN a station's stock falls below its alert_threshold_pct, THE Fuel_Dashboard SHALL display a visual alert on the fuel overview panel within 5 seconds via WebSocket
4. THE Fuel_API SHALL expose a PATCH `/fuel/stations/{station_id}/threshold` endpoint that updates the alert_threshold_pct for a specific station (default: 20%)
5. THE Fuel_API SHALL include days_until_empty in alert data, calculated from current_stock_liters divided by daily_consumption_rate
6. WHEN days_until_empty falls below 3 days, THE Fuel_API SHALL escalate the alert status to "critical" regardless of the percentage threshold

### Requirement 5: Fuel Consumption Analytics

**User Story:** As an operations analyst, I want fuel consumption analytics with daily and weekly trends, so that I can identify usage patterns, detect anomalies, and optimize fuel procurement.

#### Acceptance Criteria

1. THE Fuel_API SHALL expose a GET `/fuel/metrics/consumption` endpoint that returns fuel consumption aggregated by station, fuel_type, and time bucket (hourly, daily, weekly)
2. THE Fuel_API SHALL expose a GET `/fuel/metrics/efficiency` endpoint that returns fuel efficiency metrics per asset (liters per km or liters per trip) when odometer data is available
3. THE Fuel_API SHALL support filtering consumption metrics by station_id, fuel_type, asset_id, and date range
4. THE Fuel_API SHALL expose a GET `/fuel/metrics/summary` endpoint that returns a network-wide fuel summary: total capacity, total current stock, total daily consumption, average days_until_empty, and station count by status
5. WHEN a metrics endpoint is called with a time range exceeding 90 days, THE Fuel_API SHALL enforce daily bucket granularity to limit response size

### Requirement 6: Fuel Operations Dashboard

**User Story:** As a logistics operator, I want a fuel monitoring dashboard showing stock levels, alerts, and consumption trends, so that I can manage fuel operations from a single view.

#### Acceptance Criteria

1. THE Fuel_Dashboard SHALL display a fuel overview panel showing all stations with current stock level as a percentage bar, fuel type, location, and status color-coding (green for normal, yellow for low, red for critical, gray for empty)
2. THE Fuel_Dashboard SHALL display a network summary bar showing total fuel capacity, total current stock, number of active alerts, and average days_until_empty
3. THE Fuel_Dashboard SHALL display a consumption trend chart showing daily fuel consumption over the selected time range with separate lines per fuel type
4. THE Fuel_Dashboard SHALL support filtering stations by fuel_type, status, and location
5. WHEN a station's stock status changes, THE Fuel_Dashboard SHALL update the station row within 5 seconds via WebSocket without requiring a page refresh
6. THE Fuel_Dashboard SHALL display a station detail view when a station is selected, showing stock history, recent consumption events, recent refill events, and the daily consumption rate trend
7. THE Fuel_Dashboard SHALL be accessible at the route `/ops/fuel` in the frontend application

### Requirement 7: AI Tools for Fuel Queries and Reports

**User Story:** As an operations manager, I want the AI assistant to answer questions about fuel stock levels and generate fuel reports, so that I can get fuel insights through natural language without navigating dashboards.

#### Acceptance Criteria

1. THE AI_Fuel_Tools SHALL include a `search_fuel_stations` tool that queries fuel stations by name, fuel_type, location, and stock status
2. THE AI_Fuel_Tools SHALL include a `get_fuel_summary` tool that returns the network-wide fuel summary including total stock, alerts, and days_until_empty per station
3. THE AI_Fuel_Tools SHALL include a `get_fuel_consumption_history` tool that returns consumption events for a specific station or asset over a date range
4. THE AI_Fuel_Tools SHALL include a `generate_fuel_report` tool that produces a markdown report covering stock levels, consumption trends, alert history, and refill recommendations for a specified time range
5. WHEN an AI fuel tool is invoked, THE AI_Fuel_Tools SHALL enforce the same tenant scoping as the Fuel_API to prevent cross-tenant data access
6. THE AI_Fuel_Tools SHALL operate in read-only mode and SHALL NOT modify fuel stock levels or station configuration

### Requirement 8: Elasticsearch Indices for Fuel Data

**User Story:** As a data engineer, I want dedicated Elasticsearch indices for fuel stations and fuel events with strict mappings, so that fuel data integrity is enforced and queries perform predictably.

#### Acceptance Criteria

1. THE ElasticsearchService SHALL create the `fuel_stations` index with strict mapping including keyword fields for station_id, fuel_type, status, and tenant_id; float fields for capacity_liters, current_stock_liters, daily_consumption_rate, and days_until_empty; geo_point field for location; date fields for last_updated and created_at; and float field for alert_threshold_pct
2. THE ElasticsearchService SHALL create the `fuel_events` index with strict mapping including keyword fields for event_id, station_id, event_type (consumption/refill), fuel_type, asset_id, operator_id, and tenant_id; float field for quantity_liters; date field for event_timestamp; and optional float field for odometer_reading
3. THE ElasticsearchService SHALL configure the `fuel_events` index with an ILM policy that transitions data to warm tier after 30 days, cold tier after 90 days, and deletes after 365 days
4. WHEN a document with an unmapped field is indexed into a fuel index, THE ElasticsearchService SHALL reject the document due to strict mapping enforcement
