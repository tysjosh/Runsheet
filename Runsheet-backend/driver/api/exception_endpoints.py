"""
Driver exception reporting endpoints for the Driver Communication module.

Provides a REST endpoint for drivers to report field exceptions
(road closures, vehicle breakdowns, customer unavailable, etc.).

The exception business rule does **not** live here. ``report_exception``
resolves the path parameter and the verified :class:`TenantContext` into a
:class:`~driver.services.work_ref.WorkRef` and delegates to
:class:`~driver.services.exception_service.ExceptionReportService`, which holds
persistence, the job-timeline event, ``RiskSignal`` publication, and the
escalation broadcast exactly once (R7.16, R7.18). The order-keyed sibling
resolves through the same resolver and calls the same service, so the two paths
cannot diverge on a rule or on an error code (R7.19).

Collaborators still arrive through the module-level globals
:func:`configure_exception_endpoints` sets — that wiring pattern is preserved
rather than replaced (R7.20).

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.15, 7.16, 7.18, 7.19, 7.20
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request

from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.models import ExceptionRequest
from driver.services.exception_service import ExceptionReportService
from driver.services.work_ref import WorkRefResolver
from middleware.rate_limiter import driver_rate_key, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level service references, wired via configure_exception_endpoints()
_es_service = None
_job_service = None
_signal_bus = None
_scheduling_ws_manager = None
_driver_ws_manager = None

# The two globals task 5.5 adds: the resolver that turns a path parameter into a
# ``WorkRef`` and the service that holds the whole exception rule. Both are
# built by ``configure_exception_endpoints`` from the same globals above
# (R7.20).
_work_ref_resolver: Optional[WorkRefResolver] = None
_exception_service: Optional[ExceptionReportService] = None

router = APIRouter(prefix="/api/driver", tags=["driver-exceptions"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_exception_endpoints(
    *,
    es_service,
    job_service=None,
    order_repository=None,
    signal_bus=None,
    scheduling_ws_manager=None,
    driver_ws_manager=None,
    push_notifier=None,
) -> None:
    """
    Wire service dependencies into the exception endpoints module.

    Called once during application startup (from bootstrap) so that the
    router handlers can access the shared services.

    ``order_repository`` feeds :meth:`WorkRefResolver.resolve_order` on the
    order-keyed sibling; ``push_notifier`` is the escalation push emission point
    (R9.7). Both are optional and default to ``None``. Because this function
    assigns every module global unconditionally, a caller that omits an argument
    resets it to ``None`` — every call site must pass its full argument set.
    """
    global _es_service, _job_service, _signal_bus
    global _scheduling_ws_manager, _driver_ws_manager
    global _work_ref_resolver, _exception_service
    _es_service = es_service
    _job_service = job_service
    _signal_bus = signal_bus
    _scheduling_ws_manager = scheduling_ws_manager
    _driver_ws_manager = driver_ws_manager

    # The resolver and the service, built from the globals just assigned.
    _work_ref_resolver = WorkRefResolver(
        job_service=job_service,
        order_repository=order_repository,
    )
    _exception_service = (
        ExceptionReportService(
            es_service=es_service,
            job_service=job_service,
            order_repository=order_repository,
            signal_bus=signal_bus,
            driver_ws_manager=driver_ws_manager,
            scheduling_ws_manager=scheduling_ws_manager,
            push_notifier=push_notifier,
        )
        if es_service is not None
        else None
    )


def _get_resolver() -> WorkRefResolver:
    """Return the configured :class:`WorkRefResolver` or raise."""
    if _work_ref_resolver is None:
        raise RuntimeError(
            "Exception endpoints not configured. "
            "Call configure_exception_endpoints() during startup."
        )
    return _work_ref_resolver


def _get_exception_service() -> ExceptionReportService:
    """Return the configured :class:`ExceptionReportService` or raise."""
    if _exception_service is None:
        raise RuntimeError(
            "Exception endpoints not configured. "
            "Call configure_exception_endpoints() during startup."
        )
    return _exception_service


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/exceptions")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def report_exception(
    job_id: str,
    body: ExceptionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Report a field exception for a job.

    Resolve the ``Work_Ref``, delegate, store the idempotency response. The
    persistence, the ``RiskSignal`` publication, and the escalation broadcast
    all live below the resolution in
    :class:`~driver.services.exception_service.ExceptionReportService` (R7.18).

    The ``exception_type`` field is validated by Pydantic against the
    ``ExceptionType`` enum (road_closure, vehicle_breakdown,
    customer_unavailable, access_denied, weather, cargo_damage, other).

    The request and response contract of this endpoint is unchanged (R7.15).

    Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.15, 7.18, 7.19, 14.1, 14.3,
    14.4
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    ref = await _get_resolver().resolve_job(job_id, tenant)
    result = await _get_exception_service().report(
        ref, body, request_id=_get_request_id(request)
    )

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result


@router.post("/orders/{order_id}/exceptions")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def report_exception_for_order(
    order_id: str,
    body: ExceptionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Report a field exception against a fuel order (R7.13).

    The order-keyed sibling of :func:`report_exception`. Byte-for-byte
    identical to it below the resolver call: ``resolve_order`` replaces
    ``resolve_job`` and everything after is the same
    :class:`~driver.services.exception_service.ExceptionReportService` call and
    the same idempotency store, so the two paths hold identical validation
    rules and identical error codes by construction rather than by promise
    (R7.16, R7.19). This handler carries no rule of its own.

    Authorization is the resolver's: the named order must exist in the
    caller's tenant, and its ``assigned_driver_id`` must equal
    ``TenantContext.driver_id``. There is no dual acceptance on this path —
    a missing order is 404 ``RESOURCE_NOT_FOUND`` and a mismatch is 403
    ``FORBIDDEN`` (R7.21).

    Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.13, 7.16, 7.19, 7.21
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    ref = await _get_resolver().resolve_order(order_id, tenant)
    result = await _get_exception_service().report(
        ref, body, request_id=_get_request_id(request)
    )

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result
