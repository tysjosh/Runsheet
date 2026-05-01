# Requirements Document

## Introduction

This document specifies the requirements for extending the Runsheet asset tracking system from truck-only to multi-asset support. The current platform tracks trucks exclusively via the `trucks` Elasticsearch index and fleet API endpoints. This extension generalizes the data model and APIs to support additional logistics asset types — boats, cranes, fuel trucks, cargo containers, and personnel vehicles — while maintaining full backward compatibility with existing truck tracking functionality. The extension touches the Elasticsearch index mapping, backend API endpoints, GPS ingestion service, frontend components, and AI agent tools.

## Glossary

- **Asset**: Any trackable logistics entity. Types: vehicle (truck, fuel_truck, personnel_vehicle), vessel (boat, barge), equipment (crane, forklift), container (cargo_container, ISO_tank).
- **Asset_Type**: The top-level classification of an asset: vehicle, vessel, equipment, container.
- **Asset_Subtype**: The specific kind within a type, e.g. "truck", "boat", "crane", "cargo_container".
- **Assets_Index**: The Elasticsearch index holding all trackable assets (renamed/aliased from the existing `trucks` index).
- **Fleet_API**: The existing set of FastAPI endpoints under `/api/fleet/*` that are extended to support all asset types.
- **Fleet_Dashboard**: The existing frontend components (FleetTracking, MapView) extended with asset type filtering and type-specific icons.

## Requirements

### Requirement 1: Extend Elasticsearch Data Model for Multi-Asset Support

**User Story:** As a data engineer, I want the asset index to support multiple asset types with type-specific fields, so that all logistics assets are stored in a single searchable index with consistent structure.

#### Acceptance Criteria

1. THE Assets_Index SHALL include a `asset_type` keyword field that classifies each asset into one of: vehicle, vessel, equipment, container
2. THE Assets_Index SHALL include a `asset_subtype` keyword field that specifies the exact kind: truck, fuel_truck, personnel_vehicle, boat, barge, crane, forklift, cargo_container, ISO_tank
3. THE Assets_Index SHALL retain all existing truck fields (truck_id as asset_id, plate_number, driver_id, driver_name, current_location, destination, route, status, estimated_arrival, last_update, cargo) for backward compatibility
4. THE Assets_Index SHALL add vessel-specific optional fields: vessel_name, IMO_number, port_of_registry, draft_meters, vessel_capacity_tonnes
5. THE Assets_Index SHALL add equipment-specific optional fields: equipment_model, lifting_capacity_tonnes, operational_radius_meters
6. THE Assets_Index SHALL add container-specific optional fields: container_number, container_size (20ft, 40ft), seal_number, contents_description, weight_tonnes
7. THE Assets_Index mapping SHALL use dynamic: "false" (not strict) to allow type-specific fields without rejecting documents that omit them
8. WHEN the application starts, THE ElasticsearchService SHALL create an index alias `assets` pointing to the existing `trucks` index so that both index names work during migration

### Requirement 2: Extend Fleet API Endpoints for Multi-Asset Queries

**User Story:** As a frontend developer, I want the fleet API to support filtering by asset type and subtype, so that the dashboard can show all assets or a specific category.

#### Acceptance Criteria

1. THE Fleet_API GET `/api/fleet/trucks` endpoint SHALL continue to work unchanged, returning only assets where asset_type is "vehicle" and asset_subtype is "truck" (backward compatibility)
2. THE Fleet_API SHALL expose a GET `/api/fleet/assets` endpoint that returns all asset types with optional filtering by asset_type, asset_subtype, and status
3. THE Fleet_API SHALL expose a GET `/api/fleet/assets/{asset_id}` endpoint that returns a single asset regardless of type
4. THE Fleet_API GET `/api/fleet/summary` endpoint SHALL be extended to return counts broken down by asset_type in addition to the existing truck-only summary
5. WHEN the `/api/fleet/assets` endpoint is called with `asset_type=vessel`, THE Fleet_API SHALL return only vessel assets with vessel-specific fields included
6. THE Fleet_API SHALL return a consistent response envelope for all asset types with a `asset_type` and `asset_subtype` field in each asset object

### Requirement 3: Extend GPS Ingestion for Multi-Asset Updates

**User Story:** As an operations engineer, I want the GPS ingestion service to accept location updates for any asset type, so that boats, cranes, and containers can be tracked in real time alongside trucks.

#### Acceptance Criteria

1. THE LocationUpdate model SHALL accept an `asset_id` field in addition to the existing `truck_id` field, with `truck_id` treated as an alias for backward compatibility
2. THE LocationUpdate model SHALL accept an optional `asset_type` field to classify the update source
3. WHEN a location update is received with `asset_id`, THE DataIngestionService SHALL update the corresponding document in the assets index regardless of asset type
4. WHEN a location update is received with `truck_id` (legacy format), THE DataIngestionService SHALL treat it as `asset_id` with asset_type "vehicle"
5. THE WebSocket broadcast for location updates SHALL include the `asset_type` and `asset_subtype` fields so the frontend can render type-appropriate markers

### Requirement 4: Extend Frontend for Multi-Asset Display

**User Story:** As a logistics operator, I want the fleet tracking dashboard and map to show all asset types with distinct icons, so that I can visually distinguish trucks from boats from cranes on the map.

#### Acceptance Criteria

1. THE FleetTracking component SHALL display an asset type filter allowing the user to show/hide assets by type (all, vehicles, vessels, equipment, containers)
2. THE MapView component SHALL render different marker icons for each asset type: truck icon for vehicles, ship icon for vessels, crane icon for equipment, box icon for containers
3. THE FleetTracking list SHALL display the asset_type and asset_subtype alongside the existing plate_number/name and status fields
4. THE FleetSummary display SHALL show counts per asset type in addition to the existing total/active/delayed counts
5. THE frontend Truck TypeScript interface SHALL be extended to an Asset interface with asset_type, asset_subtype, and optional type-specific fields, while maintaining the Truck type as an alias for backward compatibility

### Requirement 5: Extend AI Tools for Multi-Asset Queries

**User Story:** As an operations manager, I want the AI assistant to search and report on all asset types, so that I can ask questions like "show me all idle boats" or "where is crane 7".

#### Acceptance Criteria

1. THE `search_fleet_data` AI tool SHALL accept an optional `asset_type` parameter to filter search results by asset type
2. THE `get_fleet_summary` AI tool SHALL return counts broken down by asset type
3. THE `find_truck_by_id` AI tool SHALL be extended to find any asset by ID regardless of type (renamed internally but keeping backward compatibility)
4. THE AI agent system prompt SHALL be updated to list the supported asset types and example queries for each

### Requirement 6: Asset Registration and Management

**User Story:** As a fleet coordinator, I want to register new assets of any type into the system, so that boats, cranes, and containers can be tracked from the moment they enter the logistics network.

#### Acceptance Criteria

1. THE Fleet_API SHALL expose a POST `/api/fleet/assets` endpoint that registers a new asset with required fields: asset_id, asset_type, asset_subtype, name, status, and current_location
2. THE Fleet_API SHALL validate that asset_type and asset_subtype are from the allowed enumeration
3. THE Fleet_API SHALL expose a PATCH `/api/fleet/assets/{asset_id}` endpoint that updates asset metadata (status, location, assignment) without requiring all fields
4. WHEN a vehicle-type asset is registered, THE Fleet_API SHALL require plate_number and driver fields
5. WHEN a vessel-type asset is registered, THE Fleet_API SHALL require vessel_name
6. WHEN a container-type asset is registered, THE Fleet_API SHALL require container_number
