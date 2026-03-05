"""
Pydantic models for the Fuel Monitoring module.

Provides data models for fuel stations, consumption/refill events,
alerts, analytics, and API response envelopes.

Requirements covered:
- 1.1-1.7: Fuel station registry and stock tracking
- 2.1-2.7: Fuel consumption recording
- 3.1-3.5: Fuel refill recording
"""

import math
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Shared / Utility Models
# ---------------------------------------------------------------------------


class GeoPoint(BaseModel):
    """Geographic coordinate pair for station location."""

    lat: float = Field(..., ge=-90, le=90, description="Latitude")
    lon: float = Field(..., ge=-180, le=180, description="Longitude")


class PaginationMeta(BaseModel):
    """Pagination metadata included in every paginated response."""

    page: int = Field(..., ge=1, description="Current page number (1-indexed)")
    size: int = Field(..., ge=1, le=100, description="Number of items per page")
    total: int = Field(..., ge=0, description="Total number of matching items")
    total_pages: int = Field(..., ge=0, description="Total number of pages")

    @classmethod
    def compute(cls, page: int, size: int, total: int) -> "PaginationMeta":
        """Create PaginationMeta with total_pages computed from total and size."""
        total_pages = math.ceil(total / size) if size > 0 else 0
        return cls(page=page, size=size, total=total, total_pages=total_pages)


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response envelope for fuel API endpoints."""

    data: list[T] = Field(default_factory=list, description="List of result items")
    pagination: PaginationMeta = Field(..., description="Pagination metadata")
    request_id: str = Field(..., description="Unique request identifier for tracing")


# ---------------------------------------------------------------------------
# Fuel Station Models
# ---------------------------------------------------------------------------


class FuelStation(BaseModel):
    """Current state of a fuel station from the fuel_stations index."""

    station_id: str = Field(..., description="Unique station identifier")
    name: str = Field(..., description="Station display name")
    fuel_type: str = Field(..., description="Fuel type: AGO, PMS, ATK, LPG")
    capacity_liters: float = Field(..., description="Maximum fuel capacity in liters")
    current_stock_liters: float = Field(..., description="Current fuel stock in liters")
    daily_consumption_rate: float = Field(..., description="Rolling average daily consumption in liters")
    days_until_empty: float = Field(..., description="Estimated days until stock reaches zero")
    alert_threshold_pct: float = Field(default=20.0, description="Low-stock alert threshold percentage")
    status: str = Field(..., description="Stock status: normal, low, critical, empty")
    location: Optional[GeoPoint] = Field(default=None, description="Station geographic coordinates")
    location_name: Optional[str] = Field(default=None, description="Human-readable location name")
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")
    last_updated: str = Field(..., description="ISO-8601 timestamp of last update")


class CreateFuelStation(BaseModel):
    """Payload for registering a new fuel station."""

    station_id: str = Field(..., description="Unique station identifier")
    name: str = Field(..., description="Station display name")
    fuel_type: str = Field(..., description="Fuel type: AGO, PMS, ATK, LPG")
    capacity_liters: float = Field(..., gt=0, description="Maximum fuel capacity in liters (must be > 0)")
    initial_stock_liters: float = Field(..., ge=0, description="Initial fuel stock in liters (must be <= capacity)")
    alert_threshold_pct: float = Field(default=20.0, ge=0, le=100, description="Low-stock alert threshold percentage")
    location: Optional[GeoPoint] = Field(default=None, description="Station geographic coordinates")
    location_name: Optional[str] = Field(default=None, description="Human-readable location name")


class UpdateFuelStation(BaseModel):
    """Payload for updating fuel station metadata (PATCH). All fields optional."""

    name: Optional[str] = Field(default=None, description="Station display name")
    capacity_liters: Optional[float] = Field(default=None, gt=0, description="Maximum fuel capacity in liters")
    alert_threshold_pct: Optional[float] = Field(default=None, ge=0, le=100, description="Alert threshold percentage")
    location: Optional[GeoPoint] = Field(default=None, description="Station geographic coordinates")
    location_name: Optional[str] = Field(default=None, description="Human-readable location name")


class FuelStationDetail(BaseModel):
    """Station with recent events, returned by the detail endpoint."""

    station: FuelStation = Field(..., description="Station current state")
    recent_consumption_events: list["ConsumptionEvent"] = Field(
        default_factory=list, description="Recent consumption events"
    )
    recent_refill_events: list["RefillEvent"] = Field(
        default_factory=list, description="Recent refill events"
    )


# ---------------------------------------------------------------------------
# Fuel Event Models
# ---------------------------------------------------------------------------


class ConsumptionEvent(BaseModel):
    """Payload for recording a fuel dispensing event."""

    station_id: str = Field(..., description="Station where fuel was dispensed")
    fuel_type: str = Field(..., description="Fuel type: AGO, PMS, ATK, LPG")
    quantity_liters: float = Field(..., gt=0, description="Quantity dispensed in liters (must be > 0)")
    asset_id: str = Field(..., description="Truck/boat/vehicle receiving fuel")
    operator_id: str = Field(..., description="Operator who dispensed fuel")
    odometer_reading: Optional[float] = Field(default=None, ge=0, description="Vehicle odometer reading at time of fueling")


class RefillEvent(BaseModel):
    """Payload for recording a fuel delivery event."""

    station_id: str = Field(..., description="Station receiving fuel delivery")
    fuel_type: str = Field(..., description="Fuel type: AGO, PMS, ATK, LPG")
    quantity_liters: float = Field(..., gt=0, description="Quantity delivered in liters (must be > 0)")
    supplier: str = Field(..., description="Fuel supplier name")
    delivery_reference: Optional[str] = Field(default=None, description="Delivery reference number")
    operator_id: str = Field(..., description="Operator who received delivery")


# ---------------------------------------------------------------------------
# Result Models (returned after recording events)
# ---------------------------------------------------------------------------


class ConsumptionResult(BaseModel):
    """Result returned after recording a consumption event."""

    event_id: str = Field(..., description="Generated event identifier")
    station_id: str = Field(..., description="Station identifier")
    new_stock_liters: float = Field(..., description="Updated stock level after consumption")
    status: str = Field(..., description="Updated station status")


class RefillResult(BaseModel):
    """Result returned after recording a refill event."""

    event_id: str = Field(..., description="Generated event identifier")
    station_id: str = Field(..., description="Station identifier")
    new_stock_liters: float = Field(..., description="Updated stock level after refill")
    status: str = Field(..., description="Updated station status")


class BatchResult(BaseModel):
    """Result returned after batch consumption recording."""

    processed: int = Field(..., ge=0, description="Number of events successfully processed")
    failed: int = Field(..., ge=0, description="Number of events that failed")
    results: list[ConsumptionResult] = Field(default_factory=list, description="Individual event results")
    errors: list[str] = Field(default_factory=list, description="Error messages for failed events")


# ---------------------------------------------------------------------------
# Alert Models
# ---------------------------------------------------------------------------


class FuelAlert(BaseModel):
    """Active fuel alert for a station with stock below threshold."""

    station_id: str = Field(..., description="Station identifier")
    name: str = Field(..., description="Station display name")
    fuel_type: str = Field(..., description="Fuel type: AGO, PMS, ATK, LPG")
    status: str = Field(..., description="Alert status: low, critical, empty")
    current_stock_liters: float = Field(..., description="Current stock level in liters")
    capacity_liters: float = Field(..., description="Station capacity in liters")
    stock_percentage: float = Field(..., description="Current stock as percentage of capacity")
    days_until_empty: float = Field(..., description="Estimated days until stock reaches zero")
    location_name: Optional[str] = Field(default=None, description="Human-readable location name")


# ---------------------------------------------------------------------------
# Analytics / Metrics Models
# ---------------------------------------------------------------------------


class FuelNetworkSummary(BaseModel):
    """Network-wide fuel summary aggregated across all stations."""

    total_stations: int = Field(..., ge=0, description="Total number of registered stations")
    total_capacity_liters: float = Field(..., ge=0, description="Sum of all station capacities")
    total_current_stock_liters: float = Field(..., ge=0, description="Sum of all current stock levels")
    total_daily_consumption: float = Field(..., ge=0, description="Sum of all daily consumption rates")
    average_days_until_empty: float = Field(..., ge=0, description="Average days until empty across stations")
    stations_normal: int = Field(..., ge=0, description="Stations with normal stock status")
    stations_low: int = Field(..., ge=0, description="Stations with low stock status")
    stations_critical: int = Field(..., ge=0, description="Stations with critical stock status")
    stations_empty: int = Field(..., ge=0, description="Stations with empty stock status")
    active_alerts: int = Field(..., ge=0, description="Number of stations with active alerts")


class MetricsBucket(BaseModel):
    """A single time bucket in a consumption metrics response."""

    timestamp: str = Field(..., description="ISO-8601 start of the time bucket")
    total_liters: float = Field(..., ge=0, description="Total fuel consumed in this bucket")
    event_count: int = Field(..., ge=0, description="Number of consumption events in this bucket")
    station_id: Optional[str] = Field(default=None, description="Station filter if applied")
    fuel_type: Optional[str] = Field(default=None, description="Fuel type filter if applied")


class EfficiencyMetric(BaseModel):
    """Fuel efficiency metric for an asset."""

    asset_id: str = Field(..., description="Asset identifier")
    total_liters: float = Field(..., ge=0, description="Total fuel consumed")
    total_distance_km: Optional[float] = Field(default=None, ge=0, description="Total distance traveled in km")
    liters_per_km: Optional[float] = Field(default=None, ge=0, description="Fuel efficiency in liters per km")
    event_count: int = Field(..., ge=0, description="Number of consumption events")


# Rebuild FuelStationDetail to resolve forward references
FuelStationDetail.model_rebuild()
