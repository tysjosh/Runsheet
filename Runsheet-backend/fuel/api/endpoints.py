"""
Fuel API endpoints for the Fuel Monitoring module.

Provides REST endpoints for fuel station management, consumption/refill
recording, alerts, and analytics under the /fuel prefix.

All endpoints are rate-limited and tenant-scoped via JWT.

Validates: Requirements 1.1-1.6, 2.1-2.7, 3.1-3.5, 4.1, 4.4, 5.1-5.5
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from config.settings import get_settings
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from fuel.models import (
    BatchResult,
    ConsumptionEvent,
    ConsumptionResult,
    CreateFuelStation,
    EfficiencyMetric,
    FuelAlert,
    FuelNetworkSummary,
    FuelStation,
    FuelStationDetail,
    MetricsBucket,
    PaginatedResponse,
    RefillEvent,
    RefillResult,
    UpdateFuelStation,
)
from fuel.services.fuel_service import FuelService

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_fuel_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level service reference, wired via configure_fuel_api()
_fuel_service: Optional[FuelService] = None

router = APIRouter(prefix="/api/fuel", tags=["fuel"])


def configure_fuel_api(*, fuel_service: FuelService) -> None:
    """
    Wire service dependencies into the fuel API module.

    Called once during application startup (from main.py) so that the
    router handlers can access the shared FuelService.
    """
    global _fuel_service
    _fuel_service = fuel_service


def _get_fuel_service() -> FuelService:
    """Return the configured FuelService or raise."""
    if _fuel_service is None:
        raise RuntimeError("Fuel API not configured. Call configure_fuel_api() during startup.")
    return _fuel_service


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request model for threshold update
# ---------------------------------------------------------------------------


class UpdateThresholdRequest(BaseModel):
    """Payload for updating a station's alert threshold."""

    alert_threshold_pct: float = Field(
        ..., ge=0, le=100, description="New alert threshold percentage"
    )


# ---------------------------------------------------------------------------
# Station management endpoints
# Validates: Requirements 1.1-1.7, 4.4
# ---------------------------------------------------------------------------


@router.get("/stations")
@limiter.limit(_fuel_rate)
async def list_stations(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    fuel_type: Optional[str] = Query(None, description="Filter by fuel type: AGO, PMS, ATK, LPG"),
    status: Optional[str] = Query(None, description="Filter by status: normal, low, critical, empty"),
    location: Optional[str] = Query(None, description="Filter by location name"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(50, ge=1, le=100, description="Page size"),
) -> PaginatedResponse[FuelStation]:
    """
    List fuel stations with optional filters and pagination.

    Validates: Requirements 1.1, 1.6
    """
    svc = _get_fuel_service()
    result = await svc.list_stations(
        tenant_id=tenant.tenant_id,
        fuel_type=fuel_type,
        status=status,
        location=location,
        page=page,
        size=size,
    )
    result.request_id = _get_request_id(request)
    return result


@router.get("/stations/{station_id}")
@limiter.limit(_fuel_rate)
async def get_station(
    station_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Get a single station with recent events.

    Validates: Requirement 1.2
    """
    svc = _get_fuel_service()
    detail = await svc.get_station(station_id, tenant.tenant_id)
    return {
        "data": detail.model_dump(),
        "request_id": _get_request_id(request),
    }


@router.post("/stations", status_code=201)
@limiter.limit(_fuel_rate)
async def create_station(
    body: CreateFuelStation,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Register a new fuel station.

    Validates: Requirements 1.3, 1.5
    """
    svc = _get_fuel_service()
    station = await svc.create_station(body, tenant.tenant_id)
    return {
        "data": station.model_dump(),
        "request_id": _get_request_id(request),
    }


@router.patch("/stations/{station_id}")
@limiter.limit(_fuel_rate)
async def update_station(
    station_id: str,
    body: UpdateFuelStation,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Update station metadata (not stock levels).

    Validates: Requirement 1.4
    """
    svc = _get_fuel_service()
    station = await svc.update_station(station_id, body, tenant.tenant_id)
    return {
        "data": station.model_dump(),
        "request_id": _get_request_id(request),
    }


@router.patch("/stations/{station_id}/threshold")
@limiter.limit(_fuel_rate)
async def update_threshold(
    station_id: str,
    body: UpdateThresholdRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Update per-station alert threshold.

    Validates: Requirement 4.4
    """
    svc = _get_fuel_service()
    station = await svc.update_threshold(
        station_id, body.alert_threshold_pct, tenant.tenant_id
    )
    return {
        "data": station.model_dump(),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Fuel event endpoints
# Validates: Requirements 2.1-2.7, 3.1-3.5
# ---------------------------------------------------------------------------


@router.post("/consumption")
@limiter.limit(_fuel_rate)
async def record_consumption(
    body: ConsumptionEvent,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Record a fuel consumption (dispensing) event.

    Validates: Requirements 2.1-2.6
    """
    svc = _get_fuel_service()
    result = await svc.record_consumption(body, tenant.tenant_id)
    return {
        "data": result.model_dump(),
        "request_id": _get_request_id(request),
    }


@router.post("/consumption/batch")
@limiter.limit(_fuel_rate)
async def record_consumption_batch(
    body: list[ConsumptionEvent],
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Batch consumption recording — multiple dispensing events in one request.

    Validates: Requirement 2.7
    """
    svc = _get_fuel_service()
    result = await svc.record_consumption_batch(body, tenant.tenant_id)
    return {
        "data": result.model_dump(),
        "request_id": _get_request_id(request),
    }


@router.post("/refill")
@limiter.limit(_fuel_rate)
async def record_refill(
    body: RefillEvent,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Record a fuel refill (delivery) event.

    Validates: Requirements 3.1-3.5
    """
    svc = _get_fuel_service()
    result = await svc.record_refill(body, tenant.tenant_id)
    return {
        "data": result.model_dump(),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# Alert and metrics endpoints
# Validates: Requirements 4.1, 5.1-5.5
# ---------------------------------------------------------------------------


@router.get("/alerts")
@limiter.limit(_fuel_rate)
async def list_alerts(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    List all active fuel alerts (stations with status != normal).

    Validates: Requirement 4.1
    """
    svc = _get_fuel_service()
    alerts = await svc.get_alerts(tenant.tenant_id)
    return {
        "data": [a.model_dump() for a in alerts],
        "request_id": _get_request_id(request),
    }


@router.get("/metrics/consumption")
@limiter.limit(_fuel_rate)
async def get_consumption_metrics(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    bucket: str = Query("daily", description="Time bucket: hourly, daily, weekly"),
    station_id: Optional[str] = Query(None, description="Filter by station_id"),
    fuel_type: Optional[str] = Query(None, description="Filter by fuel type"),
    asset_id: Optional[str] = Query(None, description="Filter by asset_id"),
    start_date: Optional[str] = Query(None, description="Start of date range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of date range (ISO 8601)"),
) -> dict:
    """
    Consumption aggregated by time bucket.

    Validates: Requirements 5.1, 5.3, 5.5
    """
    svc = _get_fuel_service()
    metrics = await svc.get_consumption_metrics(
        tenant_id=tenant.tenant_id,
        bucket=bucket,
        station_id=station_id,
        fuel_type=fuel_type,
        asset_id=asset_id,
        start_date=start_date,
        end_date=end_date,
    )
    return {
        "data": [m.model_dump() for m in metrics],
        "request_id": _get_request_id(request),
    }


@router.get("/metrics/efficiency")
@limiter.limit(_fuel_rate)
async def get_efficiency_metrics(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    asset_id: Optional[str] = Query(None, description="Filter by asset_id"),
    start_date: Optional[str] = Query(None, description="Start of date range (ISO 8601)"),
    end_date: Optional[str] = Query(None, description="End of date range (ISO 8601)"),
) -> dict:
    """
    Fuel efficiency per asset (liters per km).

    Validates: Requirements 5.2, 5.3
    """
    svc = _get_fuel_service()
    metrics = await svc.get_efficiency_metrics(
        tenant_id=tenant.tenant_id,
        asset_id=asset_id,
        start_date=start_date,
        end_date=end_date,
    )
    return {
        "data": [m.model_dump() for m in metrics],
        "request_id": _get_request_id(request),
    }


@router.get("/metrics/summary")
@limiter.limit(_fuel_rate)
async def get_network_summary(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Network-wide fuel summary across all stations.

    Validates: Requirement 5.4
    """
    svc = _get_fuel_service()
    summary = await svc.get_network_summary(tenant.tenant_id)
    return {
        "data": summary.model_dump(),
        "request_id": _get_request_id(request),
    }
