"""
REST API endpoints for the Driver domain surface.

Exposes tenant-scoped endpoints for driver CRUD and utilization:

* ``GET /api/ops/drivers`` — list all drivers for the tenant (any role).
* ``GET /api/ops/drivers/{driver_id}`` — single driver (any role).
* ``GET /api/ops/drivers/utilization`` — per-driver utilization summary
  including active_order_count, completed_today, last_seen,
  current_location, on_duty_minutes_today, qualification_warnings (any role).
* ``POST /api/ops/drivers`` — create a new driver (admin only).
* ``PATCH /api/ops/drivers/{driver_id}`` — partial update (admin only).

Every handler depends on :func:`get_tenant_context` and tenant-scopes
through :func:`inject_tenant_filter` (via the DriverRepository).

Validates: Requirements 3.1.2, 3.1.3, 3.1.4, 3.2.3.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from errors.exceptions import resource_not_found
from auth.authorization import require_role
from fuel.order_models import Driver, DriverStatus
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/ops/drivers", tags=["drivers"])

ROUTER_AUTH_POLICY = "jwt_required"


# ---------------------------------------------------------------------------
# Module-level service references (set during app wiring)
# ---------------------------------------------------------------------------

_driver_repository: Any = None


def configure_driver_endpoints(
    *,
    driver_repository: Any,
) -> None:
    """Wire service dependencies into the driver endpoints module.

    Called once during application startup (from ``bootstrap/fuel.py``).
    Tests inject fakes so the router can be exercised without ES.
    """
    global _driver_repository
    _driver_repository = driver_repository


def _get_driver_repository():
    """Return the configured DriverRepository or raise."""
    if _driver_repository is None:
        raise RuntimeError(
            "Driver endpoints not configured. "
            "Call configure_driver_endpoints() during startup."
        )
    return _driver_repository


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _require_admin_role(tenant: TenantContext) -> None:
    """Raise ``INSUFFICIENT_ROLE`` (HTTP 403) if the caller is not admin.

    Delegates to the shared :func:`auth.authorization.require_role` helper
    so this router applies the one consistent, exact-match authorization
    mechanism (Req 4.7).
    """
    require_role(tenant, "admin")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateDriverRequest(BaseModel):
    """Body for ``POST /api/ops/drivers``."""

    model_config = ConfigDict(extra="forbid")

    driver_id: str = Field(..., min_length=1)
    driver_name: str = Field(..., min_length=1)
    phone: Optional[str] = None
    status: DriverStatus = "active"
    availability: Optional[str] = None
    assigned_truck_id: Optional[str] = None
    cdl_class: Optional[str] = None
    hazmat_endorsement: Optional[bool] = None
    medical_card_expiry: Optional[str] = None
    current_location: Optional[Dict[str, float]] = None


class UpdateDriverRequest(BaseModel):
    """Body for ``PATCH /api/ops/drivers/{driver_id}``."""

    model_config = ConfigDict(extra="forbid")

    driver_name: Optional[str] = Field(default=None, min_length=1)
    phone: Optional[str] = None
    status: Optional[DriverStatus] = None
    availability: Optional[str] = None
    assigned_truck_id: Optional[str] = None
    cdl_class: Optional[str] = None
    hazmat_endorsement: Optional[bool] = None
    medical_card_expiry: Optional[str] = None
    current_location: Optional[Dict[str, float]] = None


class DriverResponse(BaseModel):
    """Response shape for a single Driver."""

    model_config = ConfigDict(extra="forbid")

    driver_id: str
    tenant_id: str
    driver_name: str
    phone: Optional[str] = None
    status: str
    availability: Optional[str] = None
    assigned_truck_id: Optional[str] = None
    cdl_class: Optional[str] = None
    hazmat_endorsement: Optional[bool] = None
    medical_card_expiry: Optional[str] = None
    current_location: Optional[Dict[str, float]] = None
    last_seen: Optional[str] = None
    active_order_count: int = 0
    completed_today: int = 0
    qualification_warnings: List[str] = Field(default_factory=list)
    created_at: str
    updated_at: str

    @classmethod
    def from_model(cls, driver: Driver) -> "DriverResponse":
        """Build a DriverResponse from a Driver model, computing warnings."""
        dumped = driver.model_dump(mode="json")
        warnings = _compute_qualification_warnings(driver)
        return cls(
            driver_id=dumped["driver_id"],
            tenant_id=dumped["tenant_id"],
            driver_name=dumped["driver_name"],
            phone=dumped.get("phone"),
            status=dumped["status"],
            availability=dumped.get("availability"),
            assigned_truck_id=dumped.get("assigned_truck_id"),
            cdl_class=dumped.get("cdl_class"),
            hazmat_endorsement=dumped.get("hazmat_endorsement"),
            medical_card_expiry=dumped.get("medical_card_expiry"),
            current_location=dumped.get("current_location"),
            last_seen=dumped.get("last_seen"),
            active_order_count=dumped.get("active_order_count", 0),
            completed_today=dumped.get("completed_today", 0),
            qualification_warnings=warnings,
            created_at=dumped["created_at"],
            updated_at=dumped["updated_at"],
        )


class DriverListResponse(BaseModel):
    """Envelope for ``GET /api/ops/drivers``."""

    model_config = ConfigDict(extra="forbid")

    items: List[DriverResponse]
    total: int


class DriverUtilizationItem(BaseModel):
    """Per-driver utilization summary."""

    model_config = ConfigDict(extra="forbid")

    driver_id: str
    driver_name: Optional[str] = None
    status: Optional[str] = None
    active_order_count: int = 0
    completed_today: int = 0
    last_seen: Optional[str] = None
    current_location: Optional[Dict[str, float]] = None
    on_duty_minutes_today: int = 0
    qualification_warnings: List[str] = Field(default_factory=list)
    medical_card_expiry: Optional[str] = None
    assigned_truck_id: Optional[str] = None
    cdl_class: Optional[str] = None
    hazmat_endorsement: Optional[bool] = None


class DriverUtilizationResponse(BaseModel):
    """Envelope for ``GET /api/ops/drivers/utilization``."""

    model_config = ConfigDict(extra="forbid")

    items: List[DriverUtilizationItem]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_qualification_warnings(driver: Driver) -> List[str]:
    """Compute qualification warnings for a driver.

    - medical_card_expiry within 30 days → "medical_card_expiring_soon"
    - medical_card_expiry in the past → "medical_card_expired"

    Validates: Requirement 3.1.4.
    """
    warnings: List[str] = []
    if driver.medical_card_expiry is not None:
        now = utcnow()
        expiry = driver.medical_card_expiry
        # Ensure both are timezone-aware for comparison
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        if expiry <= now:
            warnings.append("medical_card_expired")
        elif expiry <= now + timedelta(days=30):
            warnings.append("medical_card_expiring_soon")
    return warnings


def _compute_on_duty_minutes_today(driver: Driver) -> int:
    """Compute approximate on-duty minutes today.

    Uses the driver's ``last_seen`` timestamp as a proxy: if the driver
    has been seen today and their status is active/on_break, we estimate
    on-duty minutes as the difference between now and the start of the
    current UTC day (capped at the time since last_seen if last_seen is
    today). This is a best-effort heuristic — a full time-tracking
    system is out of scope for this spec.
    """
    if driver.last_seen is None:
        return 0
    if driver.status in ("off_duty", "inactive"):
        return 0

    now = utcnow()
    last_seen = driver.last_seen
    # Ensure timezone-aware
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Start of today (UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # If last_seen is before today, driver hasn't been active today
    if last_seen < today_start:
        return 0

    # On-duty minutes = time from last_seen to now (capped at today)
    effective_start = max(last_seen, today_start)
    delta = now - effective_start
    minutes = int(delta.total_seconds() / 60)
    return max(0, minutes)


# ---------------------------------------------------------------------------
# GET /api/ops/drivers (Req 3.1.2)
# ---------------------------------------------------------------------------


@router.get("", response_model=DriverListResponse)
async def list_drivers(
    tenant: TenantContext = Depends(get_tenant_context),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    availability: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
) -> DriverListResponse:
    """List all drivers for the tenant with optional filters.

    Any authenticated role can read drivers.
    Validates: Requirement 3.1.2.
    """
    repo = _get_driver_repository()
    result = await repo.search(
        tenant_id=tenant.tenant_id,
        status=status_filter,
        availability=availability,
        page=page,
        size=size,
    )
    items = [DriverResponse.from_model(d) for d in result["drivers"]]
    return DriverListResponse(items=items, total=result["total"])


# ---------------------------------------------------------------------------
# GET /api/ops/drivers/utilization (Req 3.2.3)
# NOTE: This route MUST be defined BEFORE /{driver_id} to avoid
# "utilization" being captured as a path parameter.
# ---------------------------------------------------------------------------


@router.get("/utilization", response_model=DriverUtilizationResponse)
async def get_driver_utilization(
    tenant: TenantContext = Depends(get_tenant_context),
) -> DriverUtilizationResponse:
    """Return per-driver utilization summary for the tenant.

    Includes active_order_count, completed_today, last_seen,
    current_location, on_duty_minutes_today, and qualification_warnings.

    Any authenticated role can read utilization.
    Validates: Requirement 3.2.3.
    """
    repo = _get_driver_repository()
    drivers = await repo.list_for_tenant(tenant.tenant_id)

    items: List[DriverUtilizationItem] = []
    for driver in drivers:
        warnings = _compute_qualification_warnings(driver)
        on_duty = _compute_on_duty_minutes_today(driver)
        dumped = driver.model_dump(mode="json")
        items.append(
            DriverUtilizationItem(
                driver_id=driver.driver_id,
                driver_name=driver.driver_name,
                status=driver.status,
                active_order_count=driver.active_order_count,
                completed_today=driver.completed_today,
                last_seen=dumped.get("last_seen"),
                current_location=dumped.get("current_location"),
                on_duty_minutes_today=on_duty,
                qualification_warnings=warnings,
                medical_card_expiry=dumped.get("medical_card_expiry"),
                assigned_truck_id=driver.assigned_truck_id,
                cdl_class=driver.cdl_class,
                hazmat_endorsement=driver.hazmat_endorsement,
            )
        )

    return DriverUtilizationResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# GET /api/ops/drivers/{driver_id} (Req 3.1.2)
# ---------------------------------------------------------------------------


@router.get("/{driver_id}", response_model=DriverResponse)
async def get_driver(
    driver_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> DriverResponse:
    """Fetch a single driver by ID.

    Returns 404 on missing or cross-tenant access.
    Any authenticated role can read drivers.
    Validates: Requirement 3.1.2.
    """
    repo = _get_driver_repository()
    driver = await repo.get(tenant.tenant_id, driver_id)
    if driver is None:
        raise resource_not_found(
            message=f"Driver '{driver_id}' not found",
            details={"driver_id": driver_id},
        )
    return DriverResponse.from_model(driver)


# ---------------------------------------------------------------------------
# POST /api/ops/drivers (Req 3.1.3) — admin only
# ---------------------------------------------------------------------------


@router.post("", response_model=DriverResponse, status_code=201)
async def create_driver(
    body: CreateDriverRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> DriverResponse:
    """Create a new driver.

    Admin role required.
    Validates: Requirement 3.1.3.
    """
    _require_admin_role(tenant)
    repo = _get_driver_repository()

    now = utcnow()
    driver_data: Dict[str, Any] = {
        "driver_id": body.driver_id,
        "tenant_id": tenant.tenant_id,
        "driver_name": body.driver_name,
        "phone": body.phone,
        "status": body.status,
        "availability": body.availability,
        "assigned_truck_id": body.assigned_truck_id,
        "cdl_class": body.cdl_class,
        "hazmat_endorsement": body.hazmat_endorsement,
        "medical_card_expiry": body.medical_card_expiry,
        "current_location": body.current_location,
        "last_seen": None,
        "active_order_count": 0,
        "completed_today": 0,
        "last_event_timestamp": now.isoformat(),
        "source_schema_version": "1.0",
        "trace_id": f"drv_{body.driver_id}",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }

    driver = await repo.create(tenant.tenant_id, driver_data)
    return DriverResponse.from_model(driver)


# ---------------------------------------------------------------------------
# PATCH /api/ops/drivers/{driver_id} (Req 3.1.3) — admin only
# ---------------------------------------------------------------------------


@router.patch("/{driver_id}", response_model=DriverResponse)
async def update_driver(
    driver_id: str,
    body: UpdateDriverRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> DriverResponse:
    """Partially update a driver.

    Admin role required.
    Validates: Requirement 3.1.3.
    """
    _require_admin_role(tenant)
    repo = _get_driver_repository()

    # Build the update dict from non-None fields
    updates: Dict[str, Any] = {}
    body_dict = body.model_dump(exclude_unset=True)
    for key, value in body_dict.items():
        updates[key] = value

    if not updates:
        # Nothing to update — just return the current driver
        driver = await repo.get(tenant.tenant_id, driver_id)
        if driver is None:
            raise resource_not_found(
                message=f"Driver '{driver_id}' not found",
                details={"driver_id": driver_id},
            )
        return DriverResponse.from_model(driver)

    driver = await repo.update(tenant.tenant_id, driver_id, updates)
    if driver is None:
        raise resource_not_found(
            message=f"Driver '{driver_id}' not found",
            details={"driver_id": driver_id},
        )
    return DriverResponse.from_model(driver)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "router",
    "configure_driver_endpoints",
]
