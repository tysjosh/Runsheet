# Implementation Plan: Multi-Asset Tracking

## Overview

This plan extends the existing truck-only fleet tracking to support multiple asset types (vehicles, vessels, equipment, containers). The approach is additive — existing functionality is preserved while new capabilities are layered on top. Tasks are ordered by dependency: data model first, then backend APIs, ingestion, AI tools, frontend, and finally migration/testing.

## Tasks

- [x] 1. Extend Elasticsearch data model
  - [x] 1.1 Add asset classification fields to trucks mapping
    - Add `asset_type` (keyword), `asset_subtype` (keyword), `asset_name` (text+keyword) to `_get_trucks_mapping()`
    - Add vessel fields: vessel_name, imo_number, port_of_registry, draft_meters, vessel_capacity_tonnes
    - Add equipment fields: equipment_model, lifting_capacity_tonnes, operational_radius_meters
    - Add container fields: container_number, container_size, seal_number, contents_description, weight_tonnes
    - Change dynamic mapping to `false` (allow unmapped fields without rejection)
    - _Requirements: 1.1-1.7_

  - [x] 1.2 Create assets index alias
    - On startup, create Elasticsearch alias `assets` pointing to `trucks` index
    - Add to `setup_indices()` in ElasticsearchService
    - Verify alias exists on startup, log warning if missing
    - _Requirements: 1.8_

  - [x] 1.3 Create backfill migration script
    - Create `scripts/backfill_asset_type.py`
    - Update all existing truck documents: set `asset_type: "vehicle"`, `asset_subtype: "truck"`, `asset_name: plate_number`
    - Use bulk update API for efficiency
    - Log count of updated documents
    - Make idempotent (skip documents that already have asset_type set)
    - _Requirements: 1.3_

- [x] 2. Extend backend API endpoints
  - [x] 2.1 Add Pydantic models for multi-asset support
    - Create `AssetType` and `AssetSubtype` enums
    - Create `Asset` response model with all type-specific optional fields
    - Create `AssetSummary` model with byType and bySubtype counts
    - Create `CreateAsset` model with type-specific validation
    - _Requirements: 2.6, 6.1-6.6_

  - [x] 2.2 Implement GET /api/fleet/assets endpoint
    - Query `assets` alias (or `trucks` index)
    - Support query params: asset_type, asset_subtype, status
    - Return paginated list of Asset objects
    - Format response with asset_type and asset_subtype fields
    - _Requirements: 2.2, 2.5, 2.6_

  - [x] 2.3 Implement GET /api/fleet/assets/{asset_id} endpoint
    - Look up by asset_id (which maps to truck_id in ES)
    - Return single Asset object with all type-specific fields
    - _Requirements: 2.3_

  - [x] 2.4 Implement POST /api/fleet/assets endpoint
    - Accept CreateAsset body
    - Validate asset_type and asset_subtype are from allowed enums
    - Validate type-specific required fields (plate_number for vehicles, vessel_name for vessels, container_number for containers)
    - Index document into trucks/assets index
    - _Requirements: 6.1-6.6_

  - [x] 2.5 Implement PATCH /api/fleet/assets/{asset_id} endpoint
    - Accept partial update body
    - Update only provided fields in ES document
    - _Requirements: 6.3_

  - [x] 2.6 Extend GET /api/fleet/summary
    - Add byType and bySubtype counts to existing FleetSummary response
    - Keep existing totalTrucks, activeTrucks, onTimeTrucks, delayedTrucks fields for backward compat
    - Add totalAssets, activeAssets, delayedAssets for all types
    - _Requirements: 2.4_

  - [x] 2.7 Ensure backward compatibility of existing truck endpoints
    - GET `/api/fleet/trucks` — add filter `asset_subtype: "truck"` (or no asset_type set) to query
    - GET `/api/fleet/trucks/{truck_id}` — unchanged
    - Verify existing frontend still works without changes
    - _Requirements: 2.1_

- [x] 3. Extend GPS ingestion service
  - [x] 3.1 Extend LocationUpdate model
    - Add optional `asset_id` field alongside existing `truck_id`
    - Add optional `asset_type` field
    - Add model validator: require either asset_id or truck_id; if only truck_id provided, copy to asset_id
    - _Requirements: 3.1-3.4_

  - [x] 3.2 Update DataIngestionService for multi-asset
    - When processing location update, use `asset_id` to look up document in assets/trucks index
    - Remove hard-coded "trucks" index assumption where applicable
    - _Requirements: 3.3, 3.4_

  - [x] 3.3 Extend WebSocket broadcast with asset type
    - Include `asset_type` and `asset_subtype` in location update broadcast payload
    - Look up asset type from ES document if not provided in the update
    - _Requirements: 3.5_

- [x] 4. Extend AI agent tools
  - [x] 4.1 Extend search_fleet_data tool
    - Add optional `asset_type` parameter
    - When provided, add asset_type filter to ES query
    - Update tool docstring with examples for different asset types
    - _Requirements: 5.1_

  - [x] 4.2 Extend get_fleet_summary tool
    - Include byType and bySubtype breakdowns in response
    - _Requirements: 5.2_

  - [x] 4.3 Extend find_truck_by_id tool
    - Search across all asset types, not just trucks
    - Return asset_type and asset_subtype in response
    - Keep function name for backward compat but update docstring
    - _Requirements: 5.3_

  - [x] 4.4 Update AI agent system prompt
    - Add supported asset types to the prompt
    - Add example queries: "show me all idle boats", "where is crane 7", "list all containers in transit"
    - _Requirements: 5.4_

- [x] 5. Extend frontend components
  - [x] 5.1 Add Asset TypeScript types
    - Add AssetType and AssetSubtype union types to types/api.ts
    - Create Asset interface extending existing fields with asset_type, asset_subtype, and type-specific optional fields
    - Keep Truck as a type alias for backward compat
    - Add AssetSummary interface
    - _Requirements: 4.5_

  - [x] 5.2 Add asset API client methods
    - Add `getAssets(filters?)` method to apiService in api.ts
    - Add `getAsset(id)` method
    - Add `createAsset(data)` method
    - Add `updateAsset(id, data)` method
    - _Requirements: 4.1-4.5_

  - [x] 5.3 Extend FleetTracking component
    - Add asset type filter dropdown (All, Vehicles, Vessels, Equipment, Containers)
    - When filter is set, call getAssets with asset_type filter
    - When filter is "All" or "Vehicles/truck", use existing getTrucks for backward compat
    - Display asset_type and asset_subtype in the list rows
    - _Requirements: 4.1, 4.3_

  - [x] 5.4 Extend MapView component
    - Define icon mapping: vehicle→🚛, vessel→🚢, equipment→🏗️, container→📦
    - Render type-appropriate marker icon based on asset_type
    - Keep existing truck marker as default for backward compat
    - _Requirements: 4.2_

  - [x] 5.5 Extend FleetSummary display
    - Show per-type counts alongside existing total/active/delayed
    - Use the extended summary endpoint data
    - _Requirements: 4.4_

  - [x] 5.6 Handle asset_type in WebSocket updates
    - Update useFleetWebSocket hook to pass asset_type from broadcast payload
    - Update FleetTracking to handle updates for non-truck assets
    - _Requirements: 3.5_

- [x] 6. Testing
  - [x] 6.1 Write unit tests for multi-asset backend
    - Test asset CRUD endpoints (create vehicle, vessel, equipment, container)
    - Test type-specific validation (plate_number required for vehicles, vessel_name for vessels, etc.)
    - Test backward compat: existing /fleet/trucks returns only trucks
    - Test extended summary includes byType counts
    - Test asset_type filtering on /fleet/assets
    - _Requirements: 2.1-2.6, 6.1-6.6_

  - [x] 6.2 Write unit tests for extended ingestion
    - Test LocationUpdate with asset_id field
    - Test LocationUpdate with legacy truck_id field (backward compat)
    - Test WebSocket broadcast includes asset_type
    - _Requirements: 3.1-3.5_

  - [x] 6.3 Write unit tests for extended AI tools
    - Test search_fleet_data with asset_type filter
    - Test get_fleet_summary includes type breakdowns
    - Test find_truck_by_id finds non-truck assets
    - _Requirements: 5.1-5.4_

  - [x] 6.4 Run migration script and verify
    - Run backfill_asset_type.py against dev environment
    - Verify all existing trucks have asset_type="vehicle", asset_subtype="truck"
    - Verify existing frontend still works after migration
    - Verify existing API responses include new fields
    - _Requirements: 1.3, 2.1_
