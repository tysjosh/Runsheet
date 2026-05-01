# Design Document: Multi-Asset Tracking

## Overview

This extension generalizes the Runsheet fleet tracking system from truck-only to multi-asset support. The approach is additive — the existing `trucks` index gets new fields and an `assets` alias, existing endpoints remain unchanged for backward compatibility, and new endpoints/filters are added alongside them. No existing functionality breaks.

## Architecture

### Migration Strategy

The key design decision is to extend the existing `trucks` index rather than creating a new index. This avoids data migration and keeps all existing queries working.

```mermaid
graph LR
    subgraph "Current State"
        TRUCKS_IDX[(trucks index)]
        FLEET_API[/api/fleet/trucks]
    end

    subgraph "After Extension"
        TRUCKS_IDX2[(trucks index<br/>+ asset_type fields)]
        ASSETS_ALIAS[(assets alias → trucks)]
        FLEET_API2[/api/fleet/trucks<br/>backward compat]
        ASSETS_API[/api/fleet/assets<br/>multi-type]
    end

    FLEET_API --> TRUCKS_IDX
    FLEET_API2 --> TRUCKS_IDX2
    ASSETS_API --> ASSETS_ALIAS
    ASSETS_ALIAS --> TRUCKS_IDX2
```

### Asset Type Hierarchy

```
asset_type (top-level)     asset_subtype (specific)
─────────────────────      ────────────────────────
vehicle                    truck, fuel_truck, personnel_vehicle
vessel                     boat, barge
equipment                  crane, forklift
container                  cargo_container, ISO_tank
```

## Components and Interfaces

### 1. Elasticsearch Index Extension

Add new fields to the existing `trucks` mapping. Use `dynamic: false` so type-specific fields that are absent on a document are simply ignored (not rejected).

```python
# Added to _get_trucks_mapping() in elasticsearch_service.py

# Core asset classification (added to all documents)
"asset_type":     {"type": "keyword"},   # vehicle, vessel, equipment, container
"asset_subtype":  {"type": "keyword"},   # truck, boat, crane, cargo_container, etc.
"asset_name":     {"type": "text", "fields": {"keyword": {"type": "keyword"}}},

# Vessel-specific fields
"vessel_name":            {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
"imo_number":             {"type": "keyword"},
"port_of_registry":       {"type": "keyword"},
"draft_meters":           {"type": "float"},
"vessel_capacity_tonnes": {"type": "float"},

# Equipment-specific fields
"equipment_model":          {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
"lifting_capacity_tonnes":  {"type": "float"},
"operational_radius_meters": {"type": "float"},

# Container-specific fields
"container_number":       {"type": "keyword"},
"container_size":         {"type": "keyword"},   # 20ft, 40ft
"seal_number":            {"type": "keyword"},
"contents_description":   {"type": "text"},
"weight_tonnes":          {"type": "float"},
```

**Index alias:** On startup, create an alias `assets` → `trucks` so new code can query via the `assets` name while old code continues using `trucks`.

**Backfill existing data:** A one-time migration script sets `asset_type: "vehicle"` and `asset_subtype: "truck"` on all existing truck documents that lack these fields.

### 2. Pydantic Models

```python
# New models in data_endpoints.py or a dedicated models file

from enum import Enum

class AssetType(str, Enum):
    VEHICLE = "vehicle"
    VESSEL = "vessel"
    EQUIPMENT = "equipment"
    CONTAINER = "container"

class AssetSubtype(str, Enum):
    # Vehicles
    TRUCK = "truck"
    FUEL_TRUCK = "fuel_truck"
    PERSONNEL_VEHICLE = "personnel_vehicle"
    # Vessels
    BOAT = "boat"
    BARGE = "barge"
    # Equipment
    CRANE = "crane"
    FORKLIFT = "forklift"
    # Containers
    CARGO_CONTAINER = "cargo_container"
    ISO_TANK = "ISO_tank"

class Asset(BaseModel):
    id: str
    asset_type: AssetType
    asset_subtype: AssetSubtype
    name: str                          # display name (plate_number for trucks, vessel_name for boats, etc.)
    status: str
    currentLocation: Location
    destination: Optional[Location] = None
    route: Optional[Route] = None
    estimatedArrival: Optional[str] = None
    lastUpdate: str
    # Vehicle fields (optional)
    plateNumber: Optional[str] = None
    driverId: Optional[str] = None
    driverName: Optional[str] = None
    cargo: Optional[CargoInfo] = None
    # Vessel fields (optional)
    vesselName: Optional[str] = None
    imoNumber: Optional[str] = None
    portOfRegistry: Optional[str] = None
    draftMeters: Optional[float] = None
    vesselCapacityTonnes: Optional[float] = None
    # Equipment fields (optional)
    equipmentModel: Optional[str] = None
    liftingCapacityTonnes: Optional[float] = None
    operationalRadiusMeters: Optional[float] = None
    # Container fields (optional)
    containerNumber: Optional[str] = None
    containerSize: Optional[str] = None
    sealNumber: Optional[str] = None
    contentsDescription: Optional[str] = None
    weightTonnes: Optional[float] = None

class AssetSummary(BaseModel):
    totalAssets: int
    activeAssets: int
    byType: dict[str, int]             # {"vehicle": 42, "vessel": 8, ...}
    bySubtype: dict[str, int]          # {"truck": 35, "fuel_truck": 7, "boat": 8, ...}
    delayedAssets: int

class CreateAsset(BaseModel):
    asset_id: str
    asset_type: AssetType
    asset_subtype: AssetSubtype
    name: str
    status: str = "active"
    current_location: Location
    # Type-specific required fields validated in endpoint logic
    plate_number: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    vessel_name: Optional[str] = None
    container_number: Optional[str] = None
```

### 3. API Endpoints

```python
# New endpoints added to data_endpoints.py

# Multi-asset endpoints (new)
@router.get("/fleet/assets")          # List all assets, filter by asset_type, asset_subtype, status
@router.get("/fleet/assets/{id}")     # Get single asset by ID
@router.post("/fleet/assets")         # Register new asset
@router.patch("/fleet/assets/{id}")   # Update asset metadata

# Existing endpoints (unchanged, backward compatible)
@router.get("/fleet/trucks")          # Returns only asset_type=vehicle, asset_subtype=truck
@router.get("/fleet/trucks/{id}")     # Returns single truck
@router.get("/fleet/summary")         # Extended: adds byType/bySubtype counts
```

### 4. GPS Ingestion Extension

```python
# Extended LocationUpdate model in ingestion/service.py

class LocationUpdate(BaseModel):
    truck_id: Optional[str] = None     # Legacy field, kept for backward compat
    asset_id: Optional[str] = None     # New preferred field
    asset_type: Optional[str] = None   # Optional classification
    latitude: float
    longitude: float
    timestamp: Optional[str] = None
    speed_kmh: Optional[float] = None
    heading: Optional[float] = None

    @model_validator(mode="after")
    def require_id(self):
        if not self.asset_id and not self.truck_id:
            raise ValueError("Either asset_id or truck_id is required")
        if not self.asset_id:
            self.asset_id = self.truck_id  # Treat truck_id as asset_id
        return self
```

### 5. Frontend TypeScript Types

```typescript
// Extended types in runsheet/src/types/api.ts

export type AssetType = "vehicle" | "vessel" | "equipment" | "container";

export type AssetSubtype =
  | "truck" | "fuel_truck" | "personnel_vehicle"
  | "boat" | "barge"
  | "crane" | "forklift"
  | "cargo_container" | "ISO_tank";

export interface Asset {
  id: string;
  assetType: AssetType;
  assetSubtype: AssetSubtype;
  name: string;
  status: string;
  currentLocation: Location;
  destination?: Location;
  route?: Route;
  estimatedArrival?: string;
  lastUpdate: string;
  // Vehicle fields
  plateNumber?: string;
  driverId?: string;
  driverName?: string;
  cargo?: CargoInfo;
  // Vessel fields
  vesselName?: string;
  imoNumber?: string;
  // Equipment fields
  equipmentModel?: string;
  liftingCapacityTonnes?: number;
  // Container fields
  containerNumber?: string;
  containerSize?: string;
  weightTonnes?: number;
}

// Backward compat: Truck is just an Asset with asset_type=vehicle
export type Truck = Asset;

export interface AssetSummary extends FleetSummary {
  byType: Record<AssetType, number>;
  bySubtype: Record<AssetSubtype, number>;
}
```

### 6. Map Marker Icons

```typescript
// Icon mapping in MapView.tsx
const ASSET_ICONS: Record<AssetType, string> = {
  vehicle: "🚛",
  vessel: "🚢",
  equipment: "🏗️",
  container: "📦",
};
```

### 7. AI Tool Extension

The existing `search_fleet_data` tool gets an optional `asset_type` parameter. The `get_fleet_summary` tool returns the extended summary with type breakdowns. The agent system prompt is updated with examples like "show me all idle boats" and "where is crane 7".

## File Changes Summary

```
Runsheet-backend/
├── services/elasticsearch_service.py   # Extend _get_trucks_mapping(), add alias
├── data_endpoints.py                   # Add /fleet/assets endpoints, extend summary
├── ingestion/service.py                # Extend LocationUpdate model
├── Agents/tools/search_tools.py        # Add asset_type param to search_fleet_data
├── Agents/tools/summary_tools.py       # Extend get_fleet_summary
├── Agents/tools/lookup_tools.py        # Extend find_truck_by_id
├── Agents/mainagent.py                 # Update system prompt
├── scripts/backfill_asset_type.py      # One-time migration script (new)

runsheet/
├── src/types/api.ts                    # Add Asset types, extend Truck
├── src/services/api.ts                 # Add getAssets(), getAsset() methods
├── src/components/FleetTracking.tsx     # Add asset type filter
├── src/components/MapView.tsx           # Add type-specific markers
├── src/hooks/useFleetWebSocket.ts       # Handle asset_type in updates
```
