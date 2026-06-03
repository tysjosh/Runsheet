"""
Data API endpoints for Runsheet Logistics Platform
Provides Elasticsearch-powered data endpoints

Validates:
- Requirement 14.1: THE Backend_Service SHALL implement rate limiting of 100 requests
  per minute per IP address for API endpoints
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, model_validator
from typing import List, Optional
from enum import Enum
from datetime import datetime
import logging
from services.elasticsearch_service import elasticsearch_service
from services.time_utils import utcnow
from middleware.rate_limiter import limiter
from config.settings import Environment, get_settings
from ops.middleware.tenant_guard import TenantContext, get_tenant_context, inject_tenant_filter
from errors.exceptions import AppException, forbidden, internal_error, resource_not_found, validation_error

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
            logger.warning(
                "Failed to fetch asset aggregations, returning zeros: %s",
                agg_err,
            )
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
            "timestamp": utcnow().isoformat()
        }
    except Exception as e:
        logger.exception("Error getting fleet summary")
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
            "timestamp": utcnow().isoformat()
        }
    except Exception as e:
        logger.exception("Error getting trucks")
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
            "timestamp": utcnow().isoformat()
        }
    except AppException:
        raise
    except Exception as e:
        logger.exception("Error getting truck %s", truck_id)
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
            "timestamp": utcnow().isoformat(),
        }
    except Exception as e:
        logger.exception("Error getting fleet assets")
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
            "timestamp": utcnow().isoformat(),
        }
    except AppException:
        raise
    except Exception as e:
        logger.exception("Error getting asset %s", asset_id)
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
        now = utcnow().isoformat()
        doc["last_update"] = now

        # Index into the trucks index using asset_id as the document ID
        await elasticsearch_service.index_document("trucks", body.asset_id, doc)

        # Dual-write the truck/asset to the Postgres source-of-truth.
        from commerce.services.commerce_persistence_bridge import (
            mirror_current_state_upsert,
        )
        await mirror_current_state_upsert("truck", doc, doc_id=body.asset_id)

        return {
            "data": _format_asset(doc),
            "success": True,
            "timestamp": now,
        }
    except Exception as e:
        logger.exception("Error creating asset")
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

        partial_doc["last_update"] = utcnow().isoformat()

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
            "timestamp": utcnow().isoformat(),
        }
    except AppException:
        raise
    except Exception as e:
        logger.exception("Error updating asset %s", asset_id)
        raise internal_error(message="Failed to update asset", details={"asset_id": asset_id, "error": str(e)})


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
            "timestamp": utcnow().isoformat()
        }
    except Exception as e:
        logger.exception("Error getting support tickets")
        raise internal_error(message="Failed to fetch support tickets", details={"error": str(e)})

# Analytics
@router.get("/analytics/metrics")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_analytics_metrics(request: Request, tenant: TenantContext = Depends(get_tenant_context), timeRange: str = "7d"):
    try:
        metrics = await elasticsearch_service.get_current_metrics(tenant.tenant_id)
    except Exception:
        metrics = {}
    return {
        "data": metrics,
        "success": True,
        "timestamp": utcnow().isoformat()
    }

@router.get("/analytics/routes")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def get_route_performance(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    try:
        routes = await elasticsearch_service.get_route_performance_data(tenant.tenant_id)
    except Exception:
        routes = []
    return {
        "data": routes,
        "success": True,
        "timestamp": utcnow().isoformat()
    }

# Semantic Search
@router.get("/search")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def semantic_search(request: Request, q: str, tenant: TenantContext = Depends(get_tenant_context), index: str = "trucks", limit: int = 10):
    """
    Perform semantic search across different indices
    """
    try:
        if index == "trucks":
            results = await elasticsearch_service.semantic_search(
                tenant.tenant_id, "trucks", q, ["cargo.description", "driver_name"], limit
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
                tenant.tenant_id, "support_tickets", q, ["issue", "description"], limit
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
            raise validation_error(message="Invalid index. Use: trucks, or support_tickets")
        
        return {
            "data": formatted_results,
            "query": q,
            "index": index,
            "success": True,
            "timestamp": utcnow().isoformat()
        }
    except AppException as exc:
        # Handle missing index gracefully — return empty results
        error_detail = str(exc.details) if hasattr(exc, 'details') and exc.details else str(exc)
        if "index_not_found" in error_detail or "no such index" in error_detail or f"search_documents({index})" in str(exc):
            logger.info("Search index '%s' does not exist yet — returning empty results", index)
            return {
                "data": [],
                "query": q,
                "index": index,
                "success": True,
                "timestamp": utcnow().isoformat()
            }
        raise
    except Exception as e:
        error_str = str(e)
        if "index_not_found_exception" in error_str or "no such index" in error_str:
            logger.info("Search index '%s' does not exist yet — returning empty results", index)
            return {
                "data": [],
                "query": q,
                "index": index,
                "success": True,
                "timestamp": utcnow().isoformat()
            }
        logger.exception("Error in semantic search")
        raise internal_error(message="Failed to perform semantic search", details={"error": str(e)})

# Data Management
#
# ``POST /api/data/cleanup`` is the highest blast-radius endpoint in the
# file: ``data_seeder.clear_all_data`` runs ``delete_by_query {"match_all":{}}``
# across every shared index (``trucks`` / ``locations`` / ``inventory`` /
# ``support_tickets`` / ``analytics_events``) with no tenant filter, then
# ``seed_all_data(force=True)`` re-seeds demo content. Left open to any
# authenticated caller this is a single-POST platform wipe. We gate it
# with defence in depth:
#
#   1. Refuse outright in production — the endpoint has no legitimate
#      production use, it exists for local-dev demo recycling only.
#   2. Require the caller carry the ``admin`` role so regular dispatcher
#      / driver JWTs cannot trigger a wipe even in staging.
#   3. Keep the existing tenant dependency so requests without a valid
#      JWT never reach the handler at all.
#
# The data_seeder helpers themselves are untouched — scoping the cleanup
# to a single tenant is tracked under the broader demo-seeding effort
# (sprint item 5) and the data_seeder refactor that follows.
@router.post("/data/cleanup")
@limiter.limit(f"{settings.rate_limit_requests_per_minute}/minute")
async def cleanup_duplicate_data(request: Request, tenant: TenantContext = Depends(get_tenant_context)):
    """Clean up duplicate data in Elasticsearch. Admin-only; disabled in production."""
    if settings.environment == Environment.PRODUCTION:
        # Refuse outright in production — there is no legitimate
        # production use for a platform-wide wipe + reseed endpoint.
        raise forbidden(
            message="Data cleanup is not available in production",
            details={"environment": settings.environment.value},
        )
    if "admin" not in (tenant.roles or []):
        raise forbidden(
            message="Data cleanup requires the admin role",
            details={"required_role": "admin"},
        )
    try:
        from services.data_seeder import data_seeder

        # Clear all existing data
        await data_seeder.clear_all_data()

        # Reseed with fresh data
        await data_seeder.seed_all_data(force=True)

        return {
            "message": "Data cleanup and reseed completed successfully",
            "success": True,
            "timestamp": utcnow().isoformat()
        }
    except AppException:
        # forbidden / validation errors must propagate with their original
        # status code instead of being re-wrapped as 500.
        raise
    except Exception as e:
        logger.exception("Error during data cleanup: %s", e)
        raise internal_error(message="Failed to clean up data", details={"error": str(e)})
