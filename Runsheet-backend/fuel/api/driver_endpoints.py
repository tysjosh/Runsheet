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
* ``POST /api/ops/drivers/{driver_id}/app-access`` — grant driver app access
  (admin only): provision the SuperTokens user for an email, assign the
  ``driver`` role, and link ``auth_users.driver_id``.
* ``DELETE /api/ops/drivers/{driver_id}/app-access`` — revoke it again.

Every handler depends on :func:`get_tenant_context` and tenant-scopes
through :func:`inject_tenant_filter` (via the DriverRepository).

Validates: Requirements 3.1.2, 3.1.3, 3.1.4, 3.2.3 (order-intake-pipeline) and
Requirements 1.16–1.26 (driver-mobile-app, App_Access_Service).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from errors.exceptions import (
    AppException,
    app_access_already_linked,
    internal_error,
    resource_not_found,
)
from auth.authorization import require_role
from fuel.order_models import Driver, DriverStatus
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.ref_resolver import get_ref_resolver
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
#: Resolver used to expand cross-module references on reads (the
#: ``assigned_truck_id`` → fleet asset link for the profile read). Defaults to
#: the process-wide resolver; tests may inject one pre-loaded with fake loaders.
_ref_resolver: Any = None
#: Compliance driver-qualification service used to correlate qualification
#: status by ``driver_id``. Wired by the compliance bootstrap (which runs after
#: fuel), so it is injected via :func:`set_driver_qualification_service`.
_driver_qualification_service: Any = None
#: App_Access_Service instance backing the two ``/app-access`` operations.
#: Rebuilt on every :func:`configure_driver_endpoints` call from the same
#: collaborators, so wiring stays a single step.
_app_access_service: Any = None


def configure_driver_endpoints(
    *,
    driver_repository: Any,
    ref_resolver: Any = None,
    driver_qualification_service: Any = None,
    app_access_uow_factory: Any = None,
    supertokens_admin: Any = None,
    session_revoker: Any = None,
    telemetry_service: Any = None,
) -> None:
    """Wire service dependencies into the driver endpoints module.

    Called once during application startup (from ``bootstrap/fuel.py``).
    Tests inject fakes so the router can be exercised without ES.

    ``ref_resolver`` overrides the process-wide resolver used to resolve the
    ``assigned_truck_id`` → fleet asset link on the profile read; when omitted
    the shared resolver is used. ``driver_qualification_service`` correlates the
    compliance qualification record by ``driver_id`` (may also be injected later
    via :func:`set_driver_qualification_service`).

    The remaining keyword arguments are the App_Access_Service collaborators
    (driver-mobile-app Req 1.17–1.26). Each defaults to its production
    implementation, so omitting them keeps the previous call contract working:

    * ``app_access_uow_factory`` — zero-argument callable returning an async
      context manager yielding an :class:`AppAccessUnitOfWork` (one PostgreSQL
      transaction). Defaults to :func:`default_app_access_uow`.
    * ``supertokens_admin`` — :class:`auth.provisioner.SuperTokensAdmin` seam;
      defaults to the SDK-backed admin, resolved lazily on first use.
    * ``session_revoker`` — async callable revoking every session for a
      SuperTokens user id; defaults to the SDK's
      ``revoke_all_sessions_for_user``, resolved lazily.
    * ``telemetry_service`` — audit sink exposing ``log_audit_event``; grants
      and revokes are always logged, with or without it.
    """
    global _driver_repository, _ref_resolver, _driver_qualification_service
    global _app_access_service
    _driver_repository = driver_repository
    if ref_resolver is not None:
        _ref_resolver = ref_resolver
    if driver_qualification_service is not None:
        _driver_qualification_service = driver_qualification_service

    _app_access_service = AppAccessService(
        driver_repository=driver_repository,
        uow_factory=app_access_uow_factory,
        supertokens_admin=supertokens_admin,
        session_revoker=session_revoker,
        telemetry_service=telemetry_service,
    )


def set_driver_qualification_service(driver_qualification_service: Any) -> None:
    """Inject the compliance ``DriverQualificationService`` post-construction.

    Compliance bootstrap runs after fuel, so the qualification service is
    wired here once it exists. When absent, the profile read degrades the
    qualification correlation to an explicit ``unresolved`` marker rather than
    failing the read.
    """
    global _driver_qualification_service
    _driver_qualification_service = driver_qualification_service


def _get_driver_repository():
    """Return the configured DriverRepository or raise."""
    if _driver_repository is None:
        raise RuntimeError(
            "Driver endpoints not configured. "
            "Call configure_driver_endpoints() during startup."
        )
    return _driver_repository


def _get_ref_resolver():
    """Return the resolver used to resolve the truck → asset link."""
    return _ref_resolver if _ref_resolver is not None else get_ref_resolver()


def _get_app_access_service() -> "AppAccessService":
    """Return the configured :class:`AppAccessService` or raise."""
    if _app_access_service is None:
        raise RuntimeError(
            "Driver endpoints not configured. "
            "Call configure_driver_endpoints() during startup."
        )
    return _app_access_service


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


class DriverProfileResponse(BaseModel):
    """Correlated driver profile for ``GET /api/ops/drivers/{driver_id}/profile``.

    Joins the ops ``driver_utilization`` record with two cross-module
    references resolved by ``driver_id`` (Req 4.1–4.3):

    * ``assigned_truck`` — the driver's ``assigned_truck_id`` resolved to a
      fleet asset summary via the shared ``RefResolver`` (or an explicit
      ``{status: "unresolved", id}`` / ``{status: "empty", id}`` marker).
    * ``qualification`` — the compliance qualification summary keyed by the
      same ``driver_id`` (``{status: "resolved", summary: {...}}`` or an
      explicit ``{status: "unresolved", driver_id}`` when no compliance record
      exists in this tenant).
    """

    model_config = ConfigDict(extra="forbid")

    driver_id: str
    utilization: DriverUtilizationItem
    assigned_truck: Dict[str, Any]
    qualification: Dict[str, Any]


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


def _build_utilization_item(driver: Driver) -> DriverUtilizationItem:
    """Build a :class:`DriverUtilizationItem` from a Driver model.

    Shared by the utilization list and the correlated profile read so both
    surfaces compute identical warnings / on-duty estimates.
    """
    warnings = _compute_qualification_warnings(driver)
    on_duty = _compute_on_duty_minutes_today(driver)
    dumped = driver.model_dump(mode="json")
    return DriverUtilizationItem(
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


async def _resolve_qualification_summary(
    tenant_id: str, driver_id: str
) -> Dict[str, Any]:
    """Correlate the compliance qualification record for ``driver_id``.

    Returns a resolution-marked payload mirroring ``RefResolver`` semantics so
    the reference is never silently dropped (Req 4.2, 5.4):

    * service unavailable / no compliance record → ``{status: "unresolved", ...}``
    * compliance record found → ``{status: "resolved", summary: {...}}``
    """
    svc = _driver_qualification_service
    if svc is None:
        return {"status": "unresolved", "driver_id": driver_id}
    try:
        summary = await svc.get_qualification_summary(tenant_id, driver_id)
    except AppException:
        # No compliance qualification record in this tenant — unresolved.
        return {"status": "unresolved", "driver_id": driver_id}
    except Exception as exc:  # noqa: BLE001 - defensive; never 500 the read
        logger.warning(
            "Qualification correlation failed for driver %s (tenant %s): %s",
            driver_id,
            tenant_id,
            exc,
        )
        return {"status": "unresolved", "driver_id": driver_id}
    return {
        "status": "resolved",
        "driver_id": driver_id,
        "summary": summary.model_dump(mode="json"),
    }


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

    items: List[DriverUtilizationItem] = [
        _build_utilization_item(driver) for driver in drivers
    ]

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
# GET /api/ops/drivers/{driver_id}/profile (Req 4.1, 4.2, 4.3, 13.1)
# ---------------------------------------------------------------------------


@router.get("/{driver_id}/profile", response_model=DriverProfileResponse)
async def get_driver_profile(
    driver_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> DriverProfileResponse:
    """Return a correlated driver profile keyed by ``driver_id``.

    Joins the ops ``driver_utilization`` record with (a) the
    ``assigned_truck_id`` resolved to a fleet asset summary via the shared
    ``RefResolver`` and (b) the compliance qualification summary keyed by the
    same ``driver_id``. References that do not resolve in this tenant are
    returned with an explicit ``unresolved`` marker rather than omitted
    (Req 5.4); all reads are tenant-scoped and never cross tenants (Req 5.3).

    Returns 404 on a missing or cross-tenant driver. Any authenticated role
    can read the profile.

    Validates: Requirements 4.1, 4.2, 4.3, 13.1.
    """
    repo = _get_driver_repository()
    driver = await repo.get(tenant.tenant_id, driver_id)
    if driver is None:
        raise resource_not_found(
            message=f"Driver '{driver_id}' not found",
            details={"driver_id": driver_id},
        )

    utilization = _build_utilization_item(driver)

    # Resolve assigned_truck_id → fleet asset (tenant-scoped; cross-tenant or
    # missing ids resolve to an explicit "unresolved" marker).
    resolver = _get_ref_resolver()
    truck_ref = await resolver.resolve(
        tenant.tenant_id, "asset", driver.assigned_truck_id
    )

    qualification = await _resolve_qualification_summary(
        tenant.tenant_id, driver_id
    )

    return DriverProfileResponse(
        driver_id=driver_id,
        utilization=utilization,
        assigned_truck=truck_ref.to_dict(),
        qualification=qualification,
    )


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
# App_Access_Service (driver-mobile-app Req 1.17–1.26)
# ---------------------------------------------------------------------------
#
# ``POST /api/ops/drivers`` above is deliberately untouched (Req 1.16): it
# creates only the ``drivers_current`` record and performs no SuperTokens write.
# Granting a driver the ability to sign into the mobile app is a separate,
# admin-gated operation, and this module is the ONLY writer of
# ``auth_users.driver_id`` in the application (Req 1.19).

#: The canonical role a mobile driver must hold.
DRIVER_ROLE = "driver"


class AppAccessGrantRequest(BaseModel):
    """Body for ``POST /api/ops/drivers/{driver_id}/app-access``.

    ``email`` is the SuperTokens idempotency key (the ``auth_users.email``
    CITEXT UNIQUE column). It is validated with a deliberately minimal shape
    check rather than ``EmailStr`` so the module never requires the optional
    ``email-validator`` dependency at import time.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=3, max_length=320)
    has_pii_access: bool = False

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        candidate = (value or "").strip()
        local, _, domain = candidate.partition("@")
        if not local or not domain or "." not in domain or " " in candidate:
            raise ValueError("email must be a valid address")
        return candidate


class AppAccessResponse(BaseModel):
    """Response for both app-access operations.

    ``provision_status`` carries the :class:`~auth.provisioner.ProvisionStatus`
    values ``created`` / ``updated`` so the caller can tell a newly created
    SuperTokens user from a reused one (Req 1.21).
    """

    model_config = ConfigDict(extra="forbid")

    driver_id: str
    email: str
    tenant_id: str
    #: ``None`` only when a revoke ran against a link whose SuperTokens user
    #: could not be resolved (the link is still cleared).
    st_user_id: Optional[str] = None
    provision_status: Literal["created", "updated"]


@runtime_checkable
class AppAccessUnitOfWork(Protocol):
    """One PostgreSQL transaction over the ``auth_users`` app-access columns.

    Also satisfies :class:`auth.provisioner.AuthUserStore`, so
    ``provision_user`` writes its ``st_user_id`` back inside *this*
    transaction rather than the independent ``session_scope()`` the default
    ``PostgresAuthUserStore`` opens. That is what lets the observable link
    commit last (Req 1.18).
    """

    async def email_linked_to_driver(
        self, *, tenant_id: str, driver_id: str
    ) -> Optional[str]:
        """Return the email currently linked to ``driver_id``, if any."""
        ...

    async def read_row(self, email: str) -> Optional[Dict[str, Any]]:
        """Return the ``auth_users`` row for ``email``, or ``None``."""
        ...

    async def upsert_app_access(
        self,
        *,
        email: str,
        tenant_id: str,
        driver_id: str,
        roles: Sequence[str],
        has_pii_access: bool,
    ) -> None:
        """Insert or update the row keyed on ``email`` (Req 1.17)."""
        ...

    async def clear_app_access(
        self, *, email: str, roles: Sequence[str]
    ) -> None:
        """Clear ``driver_id`` and store ``roles`` without it (Req 1.25)."""
        ...

    async def mark_provisioned(self, *, email: str, st_user_id: str) -> None:
        ...

    async def mark_failed(self, *, email: str, error: str) -> None:
        ...


class PostgresAppAccessUnitOfWork:
    """:class:`AppAccessUnitOfWork` bound to one SQLAlchemy ``AsyncSession``.

    Every statement runs on the supplied session and none of them commit —
    the surrounding ``session_scope()`` commits once, last (Req 1.18).
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    @staticmethod
    def _roles_bindparam():
        from sqlalchemy import Text, bindparam
        from sqlalchemy.dialects.postgresql import ARRAY

        return bindparam("roles", type_=ARRAY(Text()))

    async def email_linked_to_driver(
        self, *, tenant_id: str, driver_id: str
    ) -> Optional[str]:
        from sqlalchemy import text

        row = (
            await self._session.execute(
                text(
                    "SELECT email FROM auth_users "
                    "WHERE tenant_id = :tenant_id AND driver_id = :driver_id "
                    "LIMIT 1"
                ),
                {"tenant_id": tenant_id, "driver_id": driver_id},
            )
        ).first()
        return None if row is None else row[0]

    async def read_row(self, email: str) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text

        row = (
            await self._session.execute(
                text(
                    "SELECT email, tenant_id, roles, has_pii_access, driver_id, "
                    "st_user_id FROM auth_users WHERE email = :email"
                ),
                {"email": email},
            )
        ).first()
        if row is None:
            return None
        return {
            "email": row[0],
            "tenant_id": row[1],
            "roles": list(row[2] or []),
            "has_pii_access": bool(row[3]),
            "driver_id": row[4],
            "st_user_id": row[5],
        }

    async def upsert_app_access(
        self,
        *,
        email: str,
        tenant_id: str,
        driver_id: str,
        roles: Sequence[str],
        has_pii_access: bool,
    ) -> None:
        from sqlalchemy import text

        statement = text(
            """
            INSERT INTO auth_users
                   (email, tenant_id, roles, has_pii_access, driver_id, updated_at)
            VALUES (:email, :tenant_id, :roles, :has_pii_access, :driver_id, :now)
            ON CONFLICT (email) DO UPDATE
               SET tenant_id      = EXCLUDED.tenant_id,
                   roles          = EXCLUDED.roles,
                   has_pii_access = EXCLUDED.has_pii_access,
                   driver_id      = EXCLUDED.driver_id,
                   updated_at     = EXCLUDED.updated_at
            """
        ).bindparams(self._roles_bindparam())
        await self._session.execute(
            statement,
            {
                "email": email,
                "tenant_id": tenant_id,
                "roles": list(roles),
                "has_pii_access": bool(has_pii_access),
                "driver_id": driver_id,
                "now": utcnow(),
            },
        )

    async def clear_app_access(
        self, *, email: str, roles: Sequence[str]
    ) -> None:
        from sqlalchemy import text

        statement = text(
            """
            UPDATE auth_users
               SET roles      = :roles,
                   driver_id  = NULL,
                   updated_at = :now
             WHERE email = :email
            """
        ).bindparams(self._roles_bindparam())
        await self._session.execute(
            statement, {"roles": list(roles), "now": utcnow(), "email": email}
        )

    async def mark_provisioned(self, *, email: str, st_user_id: str) -> None:
        from sqlalchemy import text

        await self._session.execute(
            text(
                """
                UPDATE auth_users
                   SET st_user_id      = :st_user_id,
                       provisioned_at  = :now,
                       provision_error = NULL,
                       updated_at      = :now
                 WHERE email = :email
                """
            ),
            {"st_user_id": st_user_id, "now": utcnow(), "email": email},
        )

    async def mark_failed(self, *, email: str, error: str) -> None:
        from sqlalchemy import text

        await self._session.execute(
            text(
                """
                UPDATE auth_users
                   SET provision_error = :error,
                       updated_at      = :now
                 WHERE email = :email
                """
            ),
            {"error": error, "now": utcnow(), "email": email},
        )


@asynccontextmanager
async def default_app_access_uow() -> AsyncIterator[AppAccessUnitOfWork]:
    """Open one PostgreSQL transaction for an app-access operation.

    ``session_scope`` commits on clean exit and rolls back on any exception,
    which is exactly the ordered-commit contract the design requires: the
    observable ``auth_users.driver_id`` link is committed last, so a failure
    anywhere leaves no link behind.
    """
    from persistence.database import is_persistence_enabled, session_scope

    if not is_persistence_enabled():
        raise internal_error(
            message=(
                "Driver app access cannot be administered: the persistence "
                "layer is dormant (database_url is not set)."
            ),
            details={"reason": "persistence_dormant"},
        )

    async with session_scope() as session:
        yield PostgresAppAccessUnitOfWork(session)


def _error_code_value(exc: AppException) -> str:
    """Return the stable string error code carried by an ``AppException``."""
    return str(getattr(exc.error_code, "value", exc.error_code))


def _emails_equal(left: Optional[str], right: Optional[str]) -> bool:
    """Case-insensitive email comparison matching the CITEXT column."""
    return (left or "").strip().casefold() == (right or "").strip().casefold()


def _roles_with_driver(roles: Sequence[str]) -> List[str]:
    """Return ``roles`` with ``driver`` appended once, order preserved."""
    result = [r for r in roles if isinstance(r, str) and r.strip()]
    if DRIVER_ROLE not in result:
        result.append(DRIVER_ROLE)
    return result


def _roles_without_driver(roles: Sequence[str]) -> List[str]:
    """Return ``roles`` with every ``driver`` entry removed."""
    return [
        r
        for r in roles
        if isinstance(r, str) and r.strip() and r != DRIVER_ROLE
    ]


class AppAccessService:
    """Admin-gated grant / revoke of driver mobile-app access.

    The grant is *ordered commit plus compensation plus idempotency*, not a
    distributed transaction — PostgreSQL and the managed SuperTokens core
    cannot enlist in one. Concretely (Req 1.18):

    1. ``require_role(tenant, "admin")`` (Req 1.23).
    2. The ``driver_id`` must exist in ``drivers_current`` for the caller's
       tenant; otherwise 404 ``RESOURCE_NOT_FOUND`` with no SuperTokens write
       (Req 1.24).
    3. One transaction upserts the ``auth_users`` row keyed on ``email``,
       rejecting 409 ``APP_ACCESS_ALREADY_LINKED`` when that ``driver_id`` is
       already linked to a different email.
    4. ``auth.provisioner.provision_user`` runs inside that transaction with
       the unit of work as its store, so it is the only SuperTokens write path
       (Req 1.22).
    5. The transaction commits last. On any failure it rolls back and the
       ``driver`` role is removed again unless the user already held it.
    6. Every grant and revoke — including rejections — emits an audit event
       carrying the acting user, ``driver_id``, email, and outcome (Req 1.26).
    """

    def __init__(
        self,
        *,
        driver_repository: Any,
        uow_factory: Optional[Callable[[], Any]] = None,
        supertokens_admin: Any = None,
        session_revoker: Any = None,
        telemetry_service: Any = None,
        provision_user: Any = None,
    ) -> None:
        self._driver_repository = driver_repository
        self._uow_factory = uow_factory or default_app_access_uow
        self._supertokens_admin = supertokens_admin
        self._session_revoker = session_revoker
        self._telemetry = telemetry_service
        self._provision_user = provision_user

    # -- collaborator resolution (lazy so the SDK is never imported early) --

    def _admin(self) -> Any:
        if self._supertokens_admin is None:
            from auth.provisioner import SDKSuperTokensAdmin

            self._supertokens_admin = SDKSuperTokensAdmin()
        return self._supertokens_admin

    def _provisioner(self) -> Any:
        if self._provision_user is None:
            from auth.provisioner import provision_user

            self._provision_user = provision_user
        return self._provision_user

    async def _revoke_sessions(self, st_user_id: str) -> None:
        revoker = self._session_revoker
        if revoker is None:
            from supertokens_python.recipe.session.asyncio import (
                revoke_all_sessions_for_user,
            )

            revoker = revoke_all_sessions_for_user
            self._session_revoker = revoker
        await revoker(st_user_id)

    # -- audit (Req 1.26) ---------------------------------------------------

    def _audit(
        self,
        *,
        action: str,
        tenant: TenantContext,
        driver_id: str,
        email: Optional[str],
        outcome: str,
    ) -> None:
        """Record the grant / revoke attempt and its outcome."""
        payload = {
            "acting_user_id": tenant.user_id,
            "tenant_id": tenant.tenant_id,
            "driver_id": driver_id,
            "email": email,
            "outcome": outcome,
        }
        telemetry = self._telemetry
        if telemetry is not None and hasattr(telemetry, "log_audit_event"):
            try:
                telemetry.log_audit_event(
                    event_type=f"driver_app_access_{action}",
                    user_id=tenant.user_id,
                    resource_type="driver_app_access",
                    resource_id=driver_id,
                    action=action,
                    details=payload,
                )
            except Exception as exc:  # noqa: BLE001 — audit must never 500
                logger.warning("App-access audit sink failed: %s", exc)
        logger.info(
            "Audit: driver_app_access_%s %s for driver %s",
            action,
            outcome,
            driver_id,
            extra={"extra_data": {"audit_event": True, **payload}},
        )

    # -- operations ---------------------------------------------------------

    async def grant(
        self,
        tenant: TenantContext,
        driver_id: str,
        body: AppAccessGrantRequest,
    ) -> AppAccessResponse:
        """Grant mobile-app access to ``driver_id`` for ``body.email``.

        Validates: Requirements 1.17, 1.18, 1.19, 1.20, 1.21, 1.22, 1.23,
        1.24, 1.26.
        """
        require_role(tenant, "admin")
        email = body.email.strip()

        driver = await self._driver_repository.get(tenant.tenant_id, driver_id)
        if driver is None:
            self._audit(
                action="grant",
                tenant=tenant,
                driver_id=driver_id,
                email=email,
                outcome="driver_not_found",
            )
            raise resource_not_found(
                message=f"Driver '{driver_id}' not found",
                details={"driver_id": driver_id},
            )

        existing_roles: List[str] = []
        provisioned = False

        try:
            async with self._uow_factory() as uow:
                linked_email = await uow.email_linked_to_driver(
                    tenant_id=tenant.tenant_id, driver_id=driver_id
                )
                if linked_email and not _emails_equal(linked_email, email):
                    # Rejected before any SuperTokens write (Req 1.17/1.19).
                    raise app_access_already_linked(
                        details={"driver_id": driver_id},
                    )

                existing = await uow.read_row(email) or {}
                existing_roles = [
                    r for r in (existing.get("roles") or []) if isinstance(r, str)
                ]

                await uow.upsert_app_access(
                    email=email,
                    tenant_id=tenant.tenant_id,
                    driver_id=driver_id,
                    roles=_roles_with_driver(existing_roles),
                    has_pii_access=body.has_pii_access,
                )

                from auth.provisioner import AuthUserRow

                row = AuthUserRow(
                    email=email,
                    tenant_id=tenant.tenant_id,
                    roles=tuple(_roles_with_driver(existing_roles)),
                    has_pii_access=bool(body.has_pii_access),
                    driver_id=driver_id,
                    st_user_id=existing.get("st_user_id"),
                )
                provisioned = True
                result = await self._provisioner()(
                    row, admin=self._admin(), store=uow
                )
        except AppException as exc:
            # 404 / 409 rejections happen before the SuperTokens write; a
            # translated failure after it still needs compensating.
            if provisioned:
                await self._compensate(email, existing_roles)
            self._audit(
                action="grant",
                tenant=tenant,
                driver_id=driver_id,
                email=email,
                outcome=f"rejected:{_error_code_value(exc)}",
            )
            raise
        except Exception:
            if provisioned:
                await self._compensate(email, existing_roles)
            self._audit(
                action="grant",
                tenant=tenant,
                driver_id=driver_id,
                email=email,
                outcome="failed",
            )
            raise

        status = getattr(result.status, "value", result.status)
        self._audit(
            action="grant",
            tenant=tenant,
            driver_id=driver_id,
            email=email,
            outcome=f"granted:{status}",
        )
        return AppAccessResponse(
            driver_id=driver_id,
            email=email,
            tenant_id=tenant.tenant_id,
            st_user_id=result.st_user_id,
            provision_status=status,
        )

    async def _compensate(
        self, email: str, previous_roles: Sequence[str]
    ) -> None:
        """Remove a ``driver`` role the rolled-back grant would have dangled.

        A user who already held ``driver`` before the attempt keeps it — only
        a role this operation could have added is taken back. Compensation
        failure is logged, never raised: the caller already has an error to
        report, and the grant is idempotent so a retry converges.
        """
        if DRIVER_ROLE in previous_roles:
            return
        try:
            admin = self._admin()
            st_user_id = await admin.get_user_id_by_email(email)
            if st_user_id is None:
                return
            await admin.set_user_roles(
                st_user_id, _roles_without_driver(previous_roles)
            )
            logger.warning(
                "Compensated a failed app-access grant for %s — driver role "
                "removed again",
                email,
            )
        except Exception as exc:  # noqa: BLE001 — never mask the original error
            logger.error(
                "App-access compensation failed for %s: %s. The SuperTokens "
                "user may hold the driver role with no auth_users link; "
                "re-running the grant converges.",
                email,
                exc,
            )

    async def revoke(
        self, tenant: TenantContext, driver_id: str
    ) -> AppAccessResponse:
        """Revoke mobile-app access from ``driver_id``.

        Removes the ``driver`` role, clears ``auth_users.driver_id``, revokes
        every active session for that user, and leaves the ``drivers_current``
        record in place.

        Validates: Requirements 1.23, 1.25, 1.26.
        """
        require_role(tenant, "admin")

        email: Optional[str] = None
        st_user_id: Optional[str] = None
        try:
            async with self._uow_factory() as uow:
                email = await uow.email_linked_to_driver(
                    tenant_id=tenant.tenant_id, driver_id=driver_id
                )
                if email is None:
                    raise resource_not_found(
                        message=f"Driver '{driver_id}' has no app access to revoke",
                        details={"driver_id": driver_id},
                    )

                existing = await uow.read_row(email) or {}
                remaining_roles = _roles_without_driver(
                    [r for r in (existing.get("roles") or []) if isinstance(r, str)]
                )

                st_user_id = existing.get("st_user_id")
                admin = self._admin()
                if not st_user_id:
                    st_user_id = await admin.get_user_id_by_email(email)
                if st_user_id:
                    await admin.set_user_roles(st_user_id, remaining_roles)
                    await self._revoke_sessions(st_user_id)

                await uow.clear_app_access(email=email, roles=remaining_roles)
        except AppException as exc:
            self._audit(
                action="revoke",
                tenant=tenant,
                driver_id=driver_id,
                email=email,
                outcome=f"rejected:{_error_code_value(exc)}",
            )
            raise
        except Exception:
            self._audit(
                action="revoke",
                tenant=tenant,
                driver_id=driver_id,
                email=email,
                outcome="failed",
            )
            raise

        self._audit(
            action="revoke",
            tenant=tenant,
            driver_id=driver_id,
            email=email,
            outcome="revoked",
        )
        return AppAccessResponse(
            driver_id=driver_id,
            email=email,
            tenant_id=tenant.tenant_id,
            st_user_id=st_user_id,
            provision_status="updated",
        )


# ---------------------------------------------------------------------------
# POST /api/ops/drivers/{driver_id}/app-access (Req 1.17) — admin only
# ---------------------------------------------------------------------------


@router.post("/{driver_id}/app-access", response_model=AppAccessResponse)
async def grant_driver_app_access(
    driver_id: str,
    body: AppAccessGrantRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> AppAccessResponse:
    """Grant driver mobile-app access for the supplied email.

    Admin role required. Idempotent: repeating the request for an already
    linked ``driver_id`` / email pair returns 200 with the existing mapping and
    creates no second SuperTokens user (Req 1.20).

    Validates: Requirements 1.17–1.24, 1.26.
    """
    return await _get_app_access_service().grant(tenant, driver_id, body)


# ---------------------------------------------------------------------------
# DELETE /api/ops/drivers/{driver_id}/app-access (Req 1.25) — admin only
# ---------------------------------------------------------------------------


@router.delete("/{driver_id}/app-access", response_model=AppAccessResponse)
async def revoke_driver_app_access(
    driver_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> AppAccessResponse:
    """Revoke driver mobile-app access, leaving ``drivers_current`` in place.

    Admin role required.

    Validates: Requirements 1.23, 1.25, 1.26.
    """
    return await _get_app_access_service().revoke(tenant, driver_id)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "router",
    "configure_driver_endpoints",
    "set_driver_qualification_service",
    "AppAccessGrantRequest",
    "AppAccessResponse",
    "AppAccessService",
    "AppAccessUnitOfWork",
    "PostgresAppAccessUnitOfWork",
    "default_app_access_uow",
]
