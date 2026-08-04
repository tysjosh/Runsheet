"""
Driver_Work_API router — the driver's own assigned work and own identity.

Three reads, one service. ``GET /api/driver/work`` is the paged assigned-order
list (R3.1-R3.5, R3.14), ``GET /api/driver/work/{order_id}`` is one order with
its compartment manifest and stop sequence (R3.6-R3.11), and
``GET /api/driver/me`` is the caller's identity plus duty status (R1.11, R13.10).
Every rule behind them lives in
:class:`~driver.services.work_service.DriverWorkService`; these handlers resolve
the verified identity, forward the query, and stamp the correlation id.

The scope is not a parameter of this surface. ``GET /api/driver/work`` accepts
``status`` (repeatable), ``window_start``, ``window_end``, ``page``, and
``size`` — and **no** ``driver_id`` in path, query, or body (R3.12). The
``(tenant_id, driver_id)`` pair comes from
:func:`~auth.authorization.require_driver_identity`, which is each handler's
first statement, so there is no code path on which a client-supplied identifier
could widen what the caller sees. The single-order read makes "not yours" and
"does not exist" the same answer — 404 ``RESOURCE_NOT_FOUND`` (R3.6) — so the
endpoint cannot be used to enumerate the tenant's orders.

``has_pii_access`` is forwarded from the verified context on both order reads
because the service defaults it to ``False``: omitting it fails closed and drops
``customer_phone`` rather than leaking it (R15.6).

Every page carries the same envelope ``driver/api/message_endpoints.py`` returns
— ``{"data": ..., "pagination": {page, size, total, total_pages},
"request_id": ...}`` (R3.14). The ``pagination`` object is built by the service,
so the list read and the message thread read cannot drift apart on it.

These are reads, so there is no idempotency handling: a replayed GET is simply
another GET. Rate limiting is IP-keyed, matching the job-keyed message GET.

Every rejection on this surface is an ``AppException`` from
``errors/exceptions.py`` — this module raises **zero** raw ``HTTPException``
(R15.10).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Driver Work Read Model.

Validates: Requirements 1.11, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.12, 3.14, 13.10,
15.6, 15.10
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query, Request

from auth.authorization import require_driver_identity
from config.settings import get_settings
from driver.services.work_service import DriverWorkService
from errors.exceptions import internal_error
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level collaborators, wired via configure_work_endpoints().
_es_service: Optional[Any] = None
_order_repository: Optional[Any] = None
_job_service: Optional[Any] = None
_redis_client: Optional[Any] = None
_work_service: Optional[DriverWorkService] = None

router = APIRouter(prefix="/api/driver", tags=["driver-work"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_work_endpoints(
    *,
    es_service: Any = None,
    order_repository: Any = None,
    job_service: Any = None,
    redis_client: Any = None,
) -> None:
    """Wire the Driver_Work_API collaborators and build the read service.

    Called once during startup from ``bootstrap/driver.py``. Like every other
    ``configure_*`` on this surface, it assigns each module global
    unconditionally, so an argument a caller omits is reset to ``None`` — every
    call site must pass its full argument set.

    ``redis_client`` is the bundle cache for the detail read; absent it every
    read is a cache miss, never an error.
    """
    global _es_service, _order_repository, _job_service, _redis_client
    global _work_service

    _es_service = es_service
    _order_repository = order_repository
    _job_service = job_service
    _redis_client = redis_client
    _work_service = (
        DriverWorkService(
            es_service=es_service,
            order_repository=order_repository,
            job_service=job_service,
            redis_client=redis_client,
        )
        if es_service is not None
        else None
    )


def _get_work_service() -> DriverWorkService:
    """Return the configured :class:`DriverWorkService`, failing closed."""
    if _work_service is None:
        logger.error(
            "Driver work endpoints not configured. "
            "Call configure_work_endpoints() during startup."
        )
        raise internal_error(
            message="Driver work reads are temporarily unavailable",
            details={"reason": "work_endpoints_not_configured"},
        )
    return _work_service


def _get_request_id(request: Request) -> str:
    """Extract ``request_id`` from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/work")
@limiter.limit(_driver_rate)
async def list_work(
    request: Request,
    status: Optional[List[str]] = Query(
        None,
        description=(
            "Status to include; repeatable. Omitted falls back to the "
            "dispatched / in_transit default (R3.3)."
        ),
    ),
    window_start: Optional[str] = Query(
        None,
        description="Include orders whose delivery_window_start is at or after this ISO-8601 timestamp.",
    ),
    window_end: Optional[str] = Query(
        None,
        description="Include orders whose delivery_window_start is at or before this ISO-8601 timestamp.",
    ),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    size: int = Query(50, ge=1, le=200, description="Page size"),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Return the page of fuel orders assigned to the calling driver.

    There is deliberately no ``driver_id`` parameter here: the scope is the
    ``(tenant_id, driver_id)`` pair the verified session carries (R3.12).

    Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.12, 3.14, 15.6
    """
    driver_id = require_driver_identity(tenant)
    result = await _get_work_service().list_work(
        tenant.tenant_id,
        driver_id,
        statuses=tuple(status or ()),
        window_start=window_start,
        window_end=window_end,
        page=page,
        size=size,
        has_pii_access=bool(tenant.has_pii_access),
    )
    return {**result, "request_id": _get_request_id(request)}


@router.get("/work/{order_id}")
@limiter.limit(_driver_rate)
async def get_work(
    order_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Return one assigned order with its manifest and stop sequence.

    An order assigned to another driver is indistinguishable from one that does
    not exist: both are 404 ``RESOURCE_NOT_FOUND`` (R3.6).

    Validates: Requirements 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 15.6
    """
    driver_id = require_driver_identity(tenant)
    result = await _get_work_service().get_work(
        tenant.tenant_id,
        driver_id,
        order_id,
        has_pii_access=bool(tenant.has_pii_access),
    )
    return {**result, "request_id": _get_request_id(request)}


@router.get("/me")
@limiter.limit(_driver_rate)
async def get_identity(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Return the calling driver's identity and server-authoritative duty status.

    ``driver_id``, ``driver_name``, ``tenant_id``, the assigned truck
    identifier, and the duty status all come from the ``drivers_current`` record
    whose ``driver_id`` matches the verified claim (R1.11). ``Driver_App`` reads
    this on launch and adopts the returned duty status when it differs from the
    value it stored locally (R13.10).

    Validates: Requirements 1.11, 13.10
    """
    driver_id = require_driver_identity(tenant)
    result = await _get_work_service().get_identity(tenant.tenant_id, driver_id)
    return {**result, "request_id": _get_request_id(request)}


__all__ = ["router", "configure_work_endpoints"]
