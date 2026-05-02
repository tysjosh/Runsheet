"""
Data API endpoints for Runsheet Logistics Platform
Provides Elasticsearch-powered data endpoints

Validates:
- Requirement 14.1: THE Backend_Service SHALL implement rate limiting of 100 requests
  per minute per IP address for API endpoints
"""

from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
from pydantic import BaseModel, model_validator
from typing import List, Optional
from enum import Enum
from datetime import datetime
import random
import logging
from services.elasticsearch_service import elasticsearch_service
from middleware.rate_limiter import limiter
from config.settings import get_settings
from ops.middleware.tenant_guard import TenantContext, get_tenant_context, inject_tenant_filter
from errors.exceptions import AppException, internal_error, resource_not_found, validation_error

logger = logging.getLogger(__name__)

# Load settings for rate limit configuration
settings = get_settings()

# Create router for data endpoints
router = APIRouter(prefix="/api")

# Auth policy declaration for this router (Req 5.2)
# Default: JWT_REQUIRED for all data endpoints
ROUTER_AUTH_POLICY = "jwt_required"

# Data Models
class Location(BaseModel):
    id: str
    name: str
    type: str
    coordinates: dict
    address: str

class CargoInfo(BaseModel):
    type: str
    weight: float
    volume: float
    description: str
    priority: str

class Route(BaseModel):
    id: str
    origin: Location
    destination: Location
    waypoints: List[Location]
    distance: float
    estimatedDuration: int
    actualDuration: Optional[int] = None

class Truck(BaseModel):
    id: str
    plateNumber: str
    driverId: str
    driverName: str
    currentLocation: Location
    destination: Location
    route: Route
    status: str
    estimatedArrival: str
    lastUpdate: str
    cargo: Optional[CargoInfo] = None

class FleetSummary(BaseModel):
    totalTrucks: int
    activeTrucks: int
    onTimeTrucks: int
    delayedTrucks: int
    averageDelay: float


# Multi-Asset Models

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


# Mapping of valid subtypes per asset type
ASSET_TYPE_SUBTYPES: dict[AssetType, list[AssetSubtype]] = {
    AssetType.VEHICLE: [AssetSubtype.TRUCK, AssetSubtype.FUEL_TRUCK, AssetSubtype.PERSONNEL_VEHICLE],
    AssetType.VESSEL: [AssetSubtype.BOAT, AssetSubtype.BARGE],
    AssetType.EQUIPMENT: [AssetSubtype.CRANE, AssetSubtype.FORKLIFT],
    AssetType.CONTAINER: [AssetSubtype.CARGO_CONTAINER, AssetSubtype.ISO_TANK],
}


class Asset(BaseModel):
    id: str
    asset_type: AssetType
    asset_subtype: AssetSubtype
    name: str
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
    byType: dict[str, int]
    bySubtype: dict[str, int]
    delayedAssets: int


class CreateAsset(BaseModel):
    asset_id: str
    asset_type: AssetType
    asset_subtype: AssetSubtype
    name: str
    status: str = "active"
    current_location: Location
    # Vehicle-specific optional fields
    plate_number: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    # Vessel-specific optional fields
    vessel_name: Optional[str] = None
    imo_number: Optional[str] = None
    port_of_registry: Optional[str] = None
    draft_meters: Optional[float] = None
    vessel_capacity_tonnes: Optional[float] = None
    # Equipment-specific optional fields
    equipment_model: Optional[str] = None
    lifting_capacity_tonnes: Optional[float] = None
    operational_radius_meters: Optional[float] = None
    # Container-specific optional fields
    container_number: Optional[str] = None
    container_size: Optional[str] = None
    seal_number: Optional[str] = None
    contents_description: Optional[str] = None
    weight_tonnes: Optional[float] = None

    @model_validator(mode="after")
    def validate_subtype_matches_type(self):
        valid_subtypes = ASSET_TYPE_SUBTYPES.get(self.asset_type, [])
        if self.asset_subtype not in valid_subtypes:
            raise ValueError(
                f"asset_subtype '{self.asset_subtype.value}' is not valid for "
                f"asset_type '{self.asset_type.value}'. "
                f"Valid subtypes: {[s.value for s in valid_subtypes]}"
            )
        return self

    @model_validator(mode="after")
    def validate_type_specific_fields(self):
        if self.asset_type == AssetType.VEHICLE:
            if not self.plate_number:
                raise ValueError("plate_number is required for vehicle assets")
        elif self.asset_type == AssetType.VESSEL:
            if not self.vessel_name:
                raise ValueError("vessel_name is required for vessel assets")
        elif self.asset_type == AssetType.CONTAINER:
            if not self.container_number:
                raise ValueError("container_number is required for container assets")
        return self


class UpdateAsset(BaseModel):
    """Partial update body for PATCH /fleet/assets/{asset_id}. All fields are optional."""
    name: Optional[str] = None
    status: Optional[str] = None
    current_location: Optional[Location] = None
    # Vehicle fields
    plate_number: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    # Vessel fields
    vessel_name: Optional[str] = None
    imo_number: Optional[str] = None
    port_of_registry: Optional[str] = None
    draft_meters: Optional[float] = None
    vessel_capacity_tonnes: Optional[float] = None
    # Equipment fields
    equipment_model: Optional[str] = None
    lifting_capacity_tonnes: Optional[float] = None
    operational_radius_meters: Optional[float] = None
    # Container fields
    container_number: Optional[str] = None
    container_size: Optional[str] = None
    seal_number: Optional[str] = None
    contents_description: Optional[str] = None
    weight_tonnes: Optional[float] = None



class InventoryItem(BaseModel):
    id: str
    name: str
    category: str
    quantity: int
    unit: str
    location: str
    status: str
    lastUpdated: str

class Order(BaseModel):
    id: str
    customer: str
    status: str
    value: float
    items: str
    truckId: Optional[str] = None
    region: str
    createdAt: str
    deliveryEta: str
    priority: str

class SupportTicket(BaseModel):
    id: str
    customer: str
    issue: str
    description: str
    priority: str
    status: str
    createdAt: str
    assignedTo: Optional[str] = None
    relatedOrder: Optional[str] = None

# Mock Data Functions
def get_mock_locations():
    return [
        Location(
            id="nairobi-station",
            name="Nairobi Station",
            type="station",
            coordinates={"lat": -1.2921, "lng": 36.8219},
            address="Nairobi, Kenya"
        ),
        Location(
            id="mombasa-port",
            name="Mombasa Port",
            type="station",
            coordinates={"lat": -4.0435, "lng": 39.6682},
            address="Mombasa, Kenya"
        ),
        Location(
            id="kisumu-depot",
            name="Kisumu Depot",
            type="depot",
            coordinates={"lat": -0.0917, "lng": 34.7680},
            address="Kisumu, Kenya"
        ),
        Location(
            id="kinara-warehouse",
            name="Kinara Warehouse",
            type="warehouse",
            coordinates={"lat": -1.3733, "lng": 36.7516},
            address="Kinara, Kenya"
        )
    ]

def get_mock_trucks():
    locations = get_mock_locations()
    route = Route(
        id="kisumu-mombasa",
        origin=locations[2],
        destination=locations[1],
        waypoints=[locations[0]],
        distance=580,
        estimatedDuration=480
    )
    
    return [
        Truck(
            id="GI-58A",
            plateNumber="GI-58A",
            driverId="driver-001",
            driverName="John Kamau",
            currentLocation=locations[2],
            destination=locations[1],
            route=route,
            status="on_time",
            estimatedArrival="2024-01-15T14:15:00Z",
            lastUpdate="2024-01-15T12:00:00Z",
            cargo=CargoInfo(
                type="General Cargo",
                weight=15000,
                volume=45,
                description="Mixed goods",
                priority="medium"
            )
        ),
        Truck(
            id="MO-84A",
            plateNumber="MO-84A",
            driverId="driver-002",
            driverName="Mary Wanjiku",
            currentLocation=locations[0],
            destination=locations[3],
            route=route,
            status="delayed",
            estimatedArrival="2024-01-15T16:25:00Z",
            lastUpdate="2024-01-15T12:05:00Z",
            cargo=CargoInfo(
                type="Perishables",
                weight=8000,
                volume=25,
                description="Fresh produce",
                priority="high"
            )
        ),
        Truck(
            id="CE-57A",
            plateNumber="CE-57A",
            driverId="driver-003",
            driverName="Peter Ochieng",
            currentLocation=locations[2],
            destination=locations[1],
            route=route,
            status="delayed",
            estimatedArrival="2024-01-15T12:25:00Z",
            lastUpdate="2024-01-15T12:10:00Z"
        ),
        Truck(
            id="AL-94J",
            plateNumber="AL-94J",
            driverId="driver-004",
            driverName="Grace Mutua",
            currentLocation=locations[1],
            destination=locations[0],
            route=route,
            status="delayed",
            estimatedArrival="2024-01-15T12:25:00Z",
            lastUpdate="2024-01-15T12:15:00Z"
        ),
        Truck(
            id="PL-56A",
            plateNumber="PL-56A",
            driverId="driver-005",
            driverName="Samuel Kiprotich",
            currentLocation=locations[0],
            destination=locations[2],
            route=route,
            status="delayed",
            estimatedArrival="2024-01-15T12:25:00Z",
            lastUpdate="2024-01-15T12:20:00Z"
        ),
        Truck(
            id="DU-265",
            plateNumber="DU-265",
            driverId="driver-006",
            driverName="Alice Nyong",
            currentLocation=locations[1],
            destination=locations[0],
            route=route,
            status="delayed",
            estimatedArrival="2024-01-15T19:23:00Z",
            lastUpdate="2024-01-15T12:25:00Z"
        )
    ]

def get_mock_inventory():
    return [
        InventoryItem(
            id="INV-001",
            name="Diesel Fuel",
            category="Fuel",
            quantity=15000,
            unit="liters",
            location="Nairobi Depot",
            status="in_stock",
            lastUpdated="2024-01-15T10:30:00Z"
        ),
        InventoryItem(
            id="INV-002",
            name="Spare Tires",
            category="Parts",
            quantity=25,
            unit="pieces",
            location="Mombasa Warehouse",
            status="low_stock",
            lastUpdated="2024-01-15T09:15:00Z"
        ),
        InventoryItem(
            id="INV-003",
            name="Engine Oil",
            category="Maintenance",
            quantity=0,
            unit="bottles",
            location="Kisumu Station",
            status="out_of_stock",
            lastUpdated="2024-01-14T16:45:00Z"
        ),
        InventoryItem(
            id="INV-004",
            name="Brake Pads",
            category="Parts",
            quantity=120,
            unit="sets",
            location="Nairobi Depot",
            status="in_stock",
            lastUpdated="2024-01-15T08:20:00Z"
        ),
        InventoryItem(
            id="INV-005",
            name="Coolant Fluid",
            category="Maintenance",
            quantity=8,
            unit="bottles",
            location="Mombasa Warehouse",
            status="low_stock",
            lastUpdated="2024-01-15T11:00:00Z"
        )
    ]

def get_mock_orders():
    return [
        Order(
            id="ORD-001",
            customer="Safaricom Ltd",
            status="in_transit",
            value=125000,
            items="Network equipment, cables",
            truckId="GI-58A",
            region="Nairobi",
            createdAt="2024-01-14T08:00:00Z",
            deliveryEta="2024-01-15T14:00:00Z",
            priority="high"
        ),
        Order(
            id="ORD-002",
            customer="Kenya Power",
            status="pending",
            value=89000,
            items="Electrical transformers",
            region="Mombasa",
            createdAt="2024-01-15T09:30:00Z",
            deliveryEta="2024-01-16T16:00:00Z",
            priority="medium"
        ),
        Order(
            id="ORD-003",
            customer="Equity Bank",
            status="delivered",
            value=45000,
            items="ATM machines, security equipment",
            truckId="MO-84A",
            region="Kisumu",
            createdAt="2024-01-13T10:15:00Z",
            deliveryEta="2024-01-14T12:00:00Z",
            priority="urgent"
        ),
        Order(
            id="ORD-004",
            customer="Tusker Breweries",
            status="in_transit",
            value=210000,
            items="Brewing equipment, containers",
            truckId="NA-45B",
            region="Nakuru",
            createdAt="2024-01-14T11:20:00Z",
            deliveryEta="2024-01-15T18:00:00Z",
            priority="medium"
        ),
        Order(
            id="ORD-005",
            customer="Naivas Supermarket",
            status="pending",
            value=67000,
            items="Refrigeration units, shelving",
            region="Eldoret",
            createdAt="2024-01-15T07:45:00Z",
            deliveryEta="2024-01-16T10:00:00Z",
            priority="low"
        )
    ]

def get_mock_support_tickets():
    return [
        SupportTicket(
            id="TKT-001",
            customer="Safaricom Ltd",
            issue="Delivery Delay",
            description="Order ORD-001 is running 3 hours behind schedule. Customer needs urgent update on ETA.",
            priority="high",
            status="open",
            createdAt="2024-01-15T09:30:00Z",
            relatedOrder="ORD-001"
        ),
        SupportTicket(
            id="TKT-002",
            customer="Kenya Power",
            issue="Damaged Goods",
            description="Electrical transformer arrived with visible damage. Customer requesting replacement.",
            priority="urgent",
            status="in_progress",
            createdAt="2024-01-15T11:15:00Z",
            assignedTo="John Kamau",
            relatedOrder="ORD-002"
        ),
        SupportTicket(
            id="TKT-003",
            customer="Equity Bank",
            issue="Invoice Query",
            description="Customer questioning additional charges on delivery invoice.",
            priority="medium",
            status="resolved",
            createdAt="2024-01-14T14:20:00Z",
            assignedTo="Mary Wanjiku"
        ),
        SupportTicket(
            id="TKT-004",
            customer="Nakumatt Holdings",
            issue="Missing Items",
            description="Partial delivery received. 5 items missing from the shipment.",
            priority="high",
            status="open",
            createdAt="2024-01-15T13:45:00Z"
        ),
        SupportTicket(
            id="TKT-005",
            customer="Tusker Breweries",
            issue="Route Change Request",
            description="Customer requesting alternative delivery route due to road closure.",
            priority="medium",
            status="in_progress",
            createdAt="2024-01-15T08:20:00Z",
            assignedTo="Peter Omondi"
        )
    ]

# API Endpoints

# Fleet Management

@router.get("/fleet/summary")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_fleet_summary(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    try:
        # Build tenant-scoped query for trucks
        trucks_query = inject_tenant_filter(
            {"query": {"match_all": {}}},
            tenant.tenant_id,
        )
        trucks_query["size"] = 1000
        trucks_response = await elasticsearch_service.search_documents("trucks", trucks_query, size=1000)
        trucks = [hit["_source"] for hit in trucks_response["hits"]["hits"]]

        summary = FleetSummary(
            totalTrucks=len(trucks),
            activeTrucks=len([t for t in trucks if t.get("status") in ['on_time', 'delayed']]),
            onTimeTrucks=len([t for t in trucks if t.get("status") == 'on_time']),
            delayedTrucks=len([t for t in trucks if t.get("status") == 'delayed']),
            averageDelay=45
        )

        # Multi-asset counts via ES aggregations (tenant-scoped)
        agg_query = inject_tenant_filter(
            {"query": {"match_all": {}}},
            tenant.tenant_id,
        )
        agg_query["size"] = 0
        agg_query["aggs"] = {
            "by_type": {
                "terms": {"field": "asset_type", "size": 50}
            },
            "by_subtype": {
                "terms": {"field": "asset_subtype", "size": 50}
            },
            "active_count": {
                "filter": {
                    "terms": {"status": ["active", "in_transit"]}
                }
            },
            "delayed_count": {
                "filter": {
                    "term": {"status": "delayed"}
                }
            }
        }

        try:
            agg_result = await elasticsearch_service.search_documents("assets", agg_query)
            aggs = agg_result.get("aggregations", {})

            total_assets = agg_result.get("hits", {}).get("total", {}).get("value", 0)
            active_assets = aggs.get("active_count", {}).get("doc_count", 0)
            delayed_assets = aggs.get("delayed_count", {}).get("doc_count", 0)

            by_type = {
                bucket["key"]: bucket["doc_count"]
                for bucket in aggs.get("by_type", {}).get("buckets", [])
            }
            by_subtype = {
                bucket["key"]: bucket["doc_count"]
                for bucket in aggs.get("by_subtype", {}).get("buckets", [])
            }
        except Exception as agg_err:
            logger.warning(f"Failed to fetch asset aggregations, returning zeros: {agg_err}")
            total_assets = 0
            active_assets = 0
            delayed_assets = 0
            by_type = {}
            by_subtype = {}

        data = summary.dict()
        data["totalAssets"] = total_assets
        data["activeAssets"] = active_assets
        data["delayedAssets"] = delayed_assets
        data["byType"] = by_type
        data["bySubtype"] = by_subtype

        return {
            "data": data,
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting fleet summary: {e}")
        raise internal_error(message="Failed to fetch fleet summary", details={"error": str(e)})



@router.get("/fleet/trucks")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_trucks(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    try:
        # Filter for only truck assets: asset_subtype is "truck" OR asset_type is not set (legacy documents)
        inner_query = {
            "query": {
                "bool": {
                    "should": [
                        {"term": {"asset_subtype": "truck"}},
                        {"bool": {"must_not": {"exists": {"field": "asset_type"}}}}
                    ],
                    "minimum_should_match": 1
                }
            }
        }
        query = inject_tenant_filter(inner_query, tenant.tenant_id)
        query["sort"] = [{"created_at": {"order": "desc"}}]
        response = await elasticsearch_service.search_documents("trucks", query, size=1000)
        trucks = [hit["_source"] for hit in response["hits"]["hits"]]

        # Convert to Truck model format for consistency
        formatted_trucks = []
        for truck in trucks:
            # Build route with origin and destination for frontend compatibility
            route_data = truck.get("route", {})
            current_location = truck.get("current_location", {})
            destination = truck.get("destination", {})

            formatted_route = {
                "id": route_data.get("id", ""),
                "origin": current_location,
                "destination": destination,
                "waypoints": [],
                "distance": route_data.get("distance", 0),
                "estimatedDuration": route_data.get("estimated_duration", 0),
                "actualDuration": route_data.get("actual_duration")
            }

            formatted_truck = {
                "id": truck.get("truck_id"),
                "plateNumber": truck.get("plate_number"),
                "driverId": truck.get("driver_id"),
                "driverName": truck.get("driver_name"),
                "currentLocation": current_location,
                "destination": destination,
                "route": formatted_route,
                "status": truck.get("status"),
                "estimatedArrival": truck.get("estimated_arrival"),
                "lastUpdate": truck.get("last_update"),
                "cargo": truck.get("cargo")
            }
            formatted_trucks.append(formatted_truck)

        return {
            "data": formatted_trucks,
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting trucks: {e}")
        raise internal_error(message="Failed to fetch trucks", details={"error": str(e)})


@router.get("/fleet/trucks/{truck_id}")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_truck_by_id(truck_id: str, request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    try:
        # Tenant-scoped lookup by truck_id
        query = inject_tenant_filter(
            {"query": {"term": {"_id": truck_id}}},
            tenant.tenant_id,
        )
        query["size"] = 1
        result = await elasticsearch_service.search_documents("trucks", query, size=1)
        hits = result["hits"]["hits"]
        if not hits:
            raise resource_not_found(message="Truck not found", details={"truck_id": truck_id})
        truck = hits[0]["_source"]
        
        # Convert to Truck model format
        route_data = truck.get("route", {})
        current_location = truck.get("current_location", {})
        destination = truck.get("destination", {})
        
        formatted_route = {
            "id": route_data.get("id", ""),
            "origin": current_location,
            "destination": destination,
            "waypoints": [],
            "distance": route_data.get("distance", 0),
            "estimatedDuration": route_data.get("estimated_duration", 0),
            "actualDuration": route_data.get("actual_duration")
        }
        
        formatted_truck = {
            "id": truck.get("truck_id"),
            "plateNumber": truck.get("plate_number"),
            "driverId": truck.get("driver_id"),
            "driverName": truck.get("driver_name"),
            "currentLocation": current_location,
            "destination": destination,
            "route": formatted_route,
            "status": truck.get("status"),
            "estimatedArrival": truck.get("estimated_arrival"),
            "lastUpdate": truck.get("last_update"),
            "cargo": truck.get("cargo")
        }
        
        return {
            "data": formatted_truck,
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Error getting truck {truck_id}: {e}")
        raise internal_error(message="Failed to fetch truck", details={"truck_id": truck_id, "error": str(e)})

def _format_asset(doc: dict) -> dict:
    """Format an ES document as an Asset response object."""
    route_data = doc.get("route", {})
    current_location = doc.get("current_location", {})
    destination = doc.get("destination", {})

    formatted_route = {
        "id": route_data.get("id", ""),
        "origin": current_location,
        "destination": destination,
        "waypoints": [],
        "distance": route_data.get("distance", 0),
        "estimatedDuration": route_data.get("estimated_duration", 0),
        "actualDuration": route_data.get("actual_duration"),
    }

    # Determine display name: prefer asset_name, fall back to plate_number, vessel_name, etc.
    name = (
        doc.get("asset_name")
        or doc.get("plate_number")
        or doc.get("vessel_name")
        or doc.get("container_number")
        or doc.get("equipment_model")
        or doc.get("truck_id", "")
    )

    return {
        "id": doc.get("truck_id") or doc.get("asset_id", ""),
        "asset_type": doc.get("asset_type", "vehicle"),
        "asset_subtype": doc.get("asset_subtype", "truck"),
        "name": name,
        "status": doc.get("status"),
        "currentLocation": current_location,
        "destination": destination,
        "route": formatted_route,
        "estimatedArrival": doc.get("estimated_arrival"),
        "lastUpdate": doc.get("last_update"),
        # Vehicle fields
        "plateNumber": doc.get("plate_number"),
        "driverId": doc.get("driver_id"),
        "driverName": doc.get("driver_name"),
        "cargo": doc.get("cargo"),
        # Vessel fields
        "vesselName": doc.get("vessel_name"),
        "imoNumber": doc.get("imo_number"),
        "portOfRegistry": doc.get("port_of_registry"),
        "draftMeters": doc.get("draft_meters"),
        "vesselCapacityTonnes": doc.get("vessel_capacity_tonnes"),
        # Equipment fields
        "equipmentModel": doc.get("equipment_model"),
        "liftingCapacityTonnes": doc.get("lifting_capacity_tonnes"),
        "operationalRadiusMeters": doc.get("operational_radius_meters"),
        # Container fields
        "containerNumber": doc.get("container_number"),
        "containerSize": doc.get("container_size"),
        "sealNumber": doc.get("seal_number"),
        "contentsDescription": doc.get("contents_description"),
        "weightTonnes": doc.get("weight_tonnes"),
    }


@router.get("/fleet/assets")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_fleet_assets(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    asset_type: Optional[str] = None,
    asset_subtype: Optional[str] = None,
    status: Optional[str] = None,
):
    """Return all assets with optional filtering by asset_type, asset_subtype, and status."""
    try:
        # Build ES query with optional filters
        filters: List[dict] = []
        if asset_type:
            filters.append({"term": {"asset_type": asset_type}})
        if asset_subtype:
            filters.append({"term": {"asset_subtype": asset_subtype}})
        if status:
            filters.append({"term": {"status": status}})

        if filters:
            inner_query = {
                "query": {"bool": {"filter": filters}},
            }
        else:
            inner_query = {
                "query": {"match_all": {}},
            }

        # Inject tenant scoping
        query = inject_tenant_filter(inner_query, tenant.tenant_id)
        query["sort"] = [{"created_at": {"order": "desc"}}]

        # Query the assets alias (points to trucks index)
        response = await elasticsearch_service.search_documents("assets", query, size=1000)
        docs = [hit["_source"] for hit in response["hits"]["hits"]]

        formatted_assets = [_format_asset(doc) for doc in docs]

        return {
            "data": formatted_assets,
            "success": True,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"Error getting fleet assets: {e}")
        raise internal_error(message="Failed to fetch fleet assets", details={"error": str(e)})


@router.get("/fleet/assets/{asset_id}")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_asset_by_id(asset_id: str, request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    """Return a single asset by ID regardless of type."""
    try:
        # Tenant-scoped lookup by asset_id
        query = inject_tenant_filter(
            {"query": {"term": {"_id": asset_id}}},
            tenant.tenant_id,
        )
        query["size"] = 1
        result = await elasticsearch_service.search_documents("assets", query, size=1)
        hits = result["hits"]["hits"]
        if not hits:
            raise resource_not_found(message="Asset not found", details={"asset_id": asset_id})
        doc = hits[0]["_source"]
        return {
            "data": _format_asset(doc),
            "success": True,
            "timestamp": datetime.now().isoformat(),
        }
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Error getting asset {asset_id}: {e}")
        raise internal_error(message="Failed to fetch asset", details={"asset_id": asset_id, "error": str(e)})

@router.post("/fleet/assets")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def create_fleet_asset(body: CreateAsset, request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    """Register a new asset. Validates type/subtype enums and type-specific required fields via CreateAsset model."""
    try:
        # Build the ES document from the CreateAsset body (camelCase -> snake_case)
        doc = {
            "truck_id": body.asset_id,  # truck_id is the ES doc ID field for backward compat
            "asset_id": body.asset_id,
            "asset_type": body.asset_type.value,
            "asset_subtype": body.asset_subtype.value,
            "asset_name": body.name,
            "status": body.status,
            "current_location": body.current_location.model_dump(),
            "tenant_id": tenant.tenant_id,
        }

        # Add type-specific fields when present
        optional_fields = {
            "plate_number": body.plate_number,
            "driver_id": body.driver_id,
            "driver_name": body.driver_name,
            "vessel_name": body.vessel_name,
            "imo_number": body.imo_number,
            "port_of_registry": body.port_of_registry,
            "draft_meters": body.draft_meters,
            "vessel_capacity_tonnes": body.vessel_capacity_tonnes,
            "equipment_model": body.equipment_model,
            "lifting_capacity_tonnes": body.lifting_capacity_tonnes,
            "operational_radius_meters": body.operational_radius_meters,
            "container_number": body.container_number,
            "container_size": body.container_size,
            "seal_number": body.seal_number,
            "contents_description": body.contents_description,
            "weight_tonnes": body.weight_tonnes,
        }
        for field, value in optional_fields.items():
            if value is not None:
                doc[field] = value

        # Set timestamps
        now = datetime.now().isoformat()
        doc["last_update"] = now

        # Index into the trucks index using asset_id as the document ID
        await elasticsearch_service.index_document("trucks", body.asset_id, doc)

        return {
            "data": _format_asset(doc),
            "success": True,
            "timestamp": now,
        }
    except Exception as e:
        logger.error(f"Error creating asset: {e}")
        raise internal_error(message="Failed to create asset", details={"error": str(e)})


@router.patch("/fleet/assets/{asset_id}")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def update_fleet_asset(asset_id: str, body: UpdateAsset, request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    """Partially update an asset. Only the provided (non-None) fields are written."""
    try:
        # Build a partial doc containing only the fields the caller supplied.
        # Map camelCase model fields to the snake_case ES field names used by the
        # POST endpoint so the stored document stays consistent.
        field_mapping = {
            "name": "asset_name",
            "status": "status",
            "plate_number": "plate_number",
            "driver_id": "driver_id",
            "driver_name": "driver_name",
            "vessel_name": "vessel_name",
            "imo_number": "imo_number",
            "port_of_registry": "port_of_registry",
            "draft_meters": "draft_meters",
            "vessel_capacity_tonnes": "vessel_capacity_tonnes",
            "equipment_model": "equipment_model",
            "lifting_capacity_tonnes": "lifting_capacity_tonnes",
            "operational_radius_meters": "operational_radius_meters",
            "container_number": "container_number",
            "container_size": "container_size",
            "seal_number": "seal_number",
            "contents_description": "contents_description",
            "weight_tonnes": "weight_tonnes",
        }

        partial_doc: dict = {}
        body_data = body.model_dump(exclude_none=True)

        # Handle current_location separately (needs serialisation)
        if "current_location" in body_data:
            partial_doc["current_location"] = body.current_location.model_dump()

        for model_field, es_field in field_mapping.items():
            if model_field in body_data:
                partial_doc[es_field] = body_data[model_field]

        if not partial_doc:
            raise validation_error(message="No fields provided for update")

        partial_doc["last_update"] = datetime.now().isoformat()

        # Verify the asset belongs to this tenant before updating
        verify_query = inject_tenant_filter(
            {"query": {"term": {"_id": asset_id}}},
            tenant.tenant_id,
        )
        verify_query["size"] = 1
        verify_result = await elasticsearch_service.search_documents("trucks", verify_query, size=1)
        if not verify_result["hits"]["hits"]:
            raise resource_not_found(message="Asset not found", details={"asset_id": asset_id})

        # Partial update via ES _update API
        await elasticsearch_service.update_document("trucks", asset_id, partial_doc)

        # Return the full updated document (tenant-scoped)
        updated_query = inject_tenant_filter(
            {"query": {"term": {"_id": asset_id}}},
            tenant.tenant_id,
        )
        updated_query["size"] = 1
        updated_result = await elasticsearch_service.search_documents("trucks", updated_query, size=1)
        updated_doc = updated_result["hits"]["hits"][0]["_source"] if updated_result["hits"]["hits"] else {}
        return {
            "data": _format_asset(updated_doc),
            "success": True,
            "timestamp": datetime.now().isoformat(),
        }
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Error updating asset {asset_id}: {e}")
        raise internal_error(message="Failed to update asset", details={"asset_id": asset_id, "error": str(e)})


# Inventory Management
@router.get("/inventory")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_inventory(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    try:
        # Tenant-scoped query for inventory
        query = inject_tenant_filter(
            {"query": {"match_all": {}}},
            tenant.tenant_id,
        )
        query["sort"] = [{"created_at": {"order": "desc"}}]
        response = await elasticsearch_service.search_documents("inventory", query, size=1000)
        inventory = [hit["_source"] for hit in response["hits"]["hits"]]
        
        # Convert to InventoryItem model format
        formatted_inventory = []
        for item in inventory:
            formatted_item = {
                "id": item.get("item_id"),
                "name": item.get("name"),
                "category": item.get("category"),
                "quantity": item.get("quantity"),
                "unit": item.get("unit"),
                "location": item.get("location"),
                "status": item.get("status"),
                "lastUpdated": item.get("last_updated")
            }
            formatted_inventory.append(formatted_item)
        
        return {
            "data": formatted_inventory,
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting inventory: {e}")
        raise internal_error(message="Failed to fetch inventory", details={"error": str(e)})

# Orders Management
@router.get("/orders")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_orders(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    try:
        # Tenant-scoped query for orders
        query = inject_tenant_filter(
            {"query": {"match_all": {}}},
            tenant.tenant_id,
        )
        query["sort"] = [{"created_at": {"order": "desc"}}]
        response = await elasticsearch_service.search_documents("orders", query, size=1000)
        orders = [hit["_source"] for hit in response["hits"]["hits"]]
        
        # Convert to Order model format
        formatted_orders = []
        for order in orders:
            formatted_order = {
                "id": order.get("order_id"),
                "customer": order.get("customer"),
                "status": order.get("status"),
                "value": order.get("value"),
                "items": order.get("items"),
                "truckId": order.get("truck_id"),
                "region": order.get("region"),
                "createdAt": order.get("created_at"),
                "deliveryEta": order.get("delivery_eta"),
                "priority": order.get("priority")
            }
            formatted_orders.append(formatted_order)
        
        return {
            "data": formatted_orders,
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting orders: {e}")
        raise internal_error(message="Failed to fetch orders", details={"error": str(e)})

# Support Management
@router.get("/support/tickets")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_support_tickets(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    try:
        # Tenant-scoped query for support tickets
        query = inject_tenant_filter(
            {"query": {"match_all": {}}},
            tenant.tenant_id,
        )
        query["sort"] = [{"created_at": {"order": "desc"}}]
        response = await elasticsearch_service.search_documents("support_tickets", query, size=1000)
        tickets = [hit["_source"] for hit in response["hits"]["hits"]]
        
        # Convert to SupportTicket model format
        formatted_tickets = []
        for ticket in tickets:
            formatted_ticket = {
                "id": ticket.get("ticket_id"),
                "customer": ticket.get("customer"),
                "issue": ticket.get("issue"),
                "description": ticket.get("description"),
                "priority": ticket.get("priority"),
                "status": ticket.get("status"),
                "createdAt": ticket.get("created_at"),
                "assignedTo": ticket.get("assigned_to"),
                "relatedOrder": ticket.get("related_order")
            }
            formatted_tickets.append(formatted_ticket)
        
        return {
            "data": formatted_tickets,
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting support tickets: {e}")
        raise internal_error(message="Failed to fetch support tickets", details={"error": str(e)})

# Analytics
@router.get("/analytics/metrics")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_analytics_metrics(request: Request, tenant: TenantContext = Depends(get_tenant_context), timeRange: str = "7d"):
    metrics = await elasticsearch_service.get_current_metrics()
    return {
        "data": metrics,
        "success": True,
        "timestamp": datetime.now().isoformat()
    }

@router.get("/analytics/routes")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_route_performance(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    routes = await elasticsearch_service.get_route_performance_data()
    return {
        "data": routes,
        "success": True,
        "timestamp": datetime.now().isoformat()
    }

@router.get("/analytics/delay-causes")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_delay_causes(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    causes = await elasticsearch_service.get_delay_causes_data()
    return {
        "data": causes,
        "success": True,
        "timestamp": datetime.now().isoformat()
    }

@router.get("/analytics/regional")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_regional_performance(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    regions = await elasticsearch_service.get_regional_performance_data()
    return {
        "data": regions,
        "success": True,
        "timestamp": datetime.now().isoformat()
    }

@router.get("/analytics/time-series")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_time_series_data(request: Request, tenant: TenantContext = Depends(get_tenant_context), metric: str = "delivery_performance_pct", timeRange: str = "7d"):
    """Get time-series data for trending charts"""
    event_type = "hourly_metrics" if timeRange == "24h" else "daily_performance"
    data = await elasticsearch_service.get_time_series_data(event_type, metric, timeRange)
    
    return {
        "data": data,
        "metric": metric,
        "timeRange": timeRange,
        "success": True,
        "timestamp": datetime.now().isoformat()
    }

# Semantic Search
@router.get("/search")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def semantic_search(request: Request, q: str, tenant: TenantContext = Depends(get_tenant_context), index: str = "orders", limit: int = 10):
    """
    Perform semantic search across different indices
    """
    try:
        if index == "orders":
            results = await elasticsearch_service.semantic_search(
                "orders", q, ["items", "customer"], limit
            )
            # Format results
            formatted_results = []
            for result in results:
                formatted_result = {
                    "id": result.get("order_id"),
                    "customer": result.get("customer"),
                    "status": result.get("status"),
                    "value": result.get("value"),
                    "items": result.get("items"),
                    "region": result.get("region"),
                    "priority": result.get("priority")
                }
                formatted_results.append(formatted_result)
            
        elif index == "trucks":
            results = await elasticsearch_service.semantic_search(
                "trucks", q, ["cargo.description", "driver_name"], limit
            )
            formatted_results = []
            for result in results:
                formatted_result = {
                    "id": result.get("truck_id"),
                    "plateNumber": result.get("plate_number"),
                    "driverName": result.get("driver_name"),
                    "status": result.get("status"),
                    "cargo": result.get("cargo")
                }
                formatted_results.append(formatted_result)
                
        elif index == "support_tickets":
            results = await elasticsearch_service.semantic_search(
                "support_tickets", q, ["issue", "description"], limit
            )
            formatted_results = []
            for result in results:
                formatted_result = {
                    "id": result.get("ticket_id"),
                    "customer": result.get("customer"),
                    "issue": result.get("issue"),
                    "description": result.get("description"),
                    "priority": result.get("priority"),
                    "status": result.get("status")
                }
                formatted_results.append(formatted_result)
        else:
            raise validation_error(message="Invalid index. Use: orders, trucks, or support_tickets")
        
        return {
            "data": formatted_results,
            "query": q,
            "index": index,
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
    except AppException:
        raise
    except Exception as e:
        logger.error(f"Error in semantic search: {e}")
        raise internal_error(message="Failed to perform semantic search", details={"error": str(e)})

# Data Management
@router.post("/data/cleanup")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def cleanup_duplicate_data(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    """Clean up duplicate data in Elasticsearch"""
    try:
        from services.data_seeder import data_seeder
        
        # Clear all existing data
        await data_seeder.clear_all_data()
        
        # Reseed with fresh data
        await data_seeder.seed_all_data(force=True)
        
        return {
            "message": "Data cleanup and reseed completed successfully",
            "success": True,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error during data cleanup: {e}")
        raise internal_error(message="Failed to clean up data", details={"error": str(e)})

# Data Upload
@router.post("/data/upload/sheets")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def upload_from_sheets(request: Request, body: dict, tenant: TenantContext = Depends(get_tenant_context)):
    # Simulate processing
    record_count = random.randint(50, 150)
    
    return {
        "data": {"recordCount": record_count},
        "success": True,
        "timestamp": datetime.now().isoformat()
    }

@router.post("/data/upload/csv")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def upload_csv(request: Request, file: UploadFile = File(...), dataType: str = Form(...), tenant: TenantContext = Depends(get_tenant_context)):
    # Simulate processing
    record_count = random.randint(100, 300)
    
    return {
        "data": {"recordCount": record_count},
        "success": True,
        "timestamp": datetime.now().isoformat()
    }