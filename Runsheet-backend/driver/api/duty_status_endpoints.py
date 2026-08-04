"""
Duty_Status router — ``POST /api/driver/duty-status`` and
``GET /api/driver/duty-status/history``.

Two handlers, one service. Every rule about what a duty-status transition means
lives in :class:`~driver.services.duty_status_service.DutyStatusService`: the
403 on a driver-submitted ``inactive`` (R13.2), the 409
``ACTIVE_DELIVERY_IN_PROGRESS`` while an assigned order is ``in_transit``
(R13.6), the append-then-project ordering, and the 202
``DUTY_STATUS_PROJECTION_PENDING`` that tells the offline queue to dequeue
rather than retry (R13.18). This module resolves the caller's identity, decides
*whose* history the caller may read, and stamps the correlation id.

**The write has no ``driver_id`` parameter.** ``POST /api/driver/duty-status``
takes the ``(tenant_id, driver_id)`` pair from
:func:`~auth.authorization.require_driver_identity` and passes it as both the
subject and the ``actor_id``, with ``source="driver"`` — so this surface cannot
be used to move another driver's duty status, and the administrator-set
``inactive`` of R13.19 is deliberately not reachable from here (it belongs to the
ops surface, where the actor is an administrator).

**The read has one, and it is role-scoped.** ``driver_id`` is an optional query
parameter on the history read because R13.20 gives a ``dispatcher`` or ``admin``
caller a driver's timeline. A caller holding only ``driver`` may name its own id
or omit it; naming a different one is 403 ``FORBIDDEN`` (R13.21) — never a 404,
because the caller is not permitted to learn whether that driver exists. The
role decision is made here rather than in the service, which is scoped to a
single ``(tenant_id, driver_id)`` pair and has no parameter that could widen it.

The write carries ``@limiter.limit(_driver_rate, key_func=driver_rate_key)`` and
accepts an optional ``X-Idempotency-Key``, matching every other driver write
(R11.1-R11.5, R15.13). The read is IP-keyed, matching the other driver reads.

Every rejection on this surface is an ``AppException`` from
``errors/exceptions.py`` — this module raises **zero** raw ``HTTPException``
(R15.10).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Data Models,
"New index: ``duty_status_events``".

Validates: Requirements 13.1, 13.20, 13.21
- 13.1: a transition to ``active``, ``on_break``, or ``off_duty`` from an
  authenticated driver
- 13.20: a ``dispatcher`` or ``admin`` caller reads a driver's events inside a
  time range, sorted by ``event_timestamp`` ascending
- 13.21: a ``driver``-role caller reads only its own events, and naming another
  ``driver_id`` is 403 ``FORBIDDEN``
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.authorization import require_driver_identity, require_role
from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.services.duty_status_service import DutyStatusService
from errors.exceptions import forbidden, internal_error, invalid_request
from middleware.rate_limiter import driver_rate_key, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

#: The roles that may read another driver's duty-status history (R13.20).
HISTORY_WIDE_ROLES: tuple[str, ...] = ("dispatcher", "admin")

#: The role that may read its own (R13.21).
DRIVER_ROLE: str = "driver"

# Module-level collaborators, wired via configure_duty_status_endpoints().
_es_service: Optional[Any] = None
_driver_repository: Optional[Any] = None
_order_repository: Optional[Any] = None
_duty_status_service: Optional[DutyStatusService] = None

router = APIRouter(prefix="/api/driver", tags=["driver-duty-status"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_duty_status_endpoints(
    *,
    es_service: Any = None,
    driver_repository: Any = None,
    order_repository: Any = None,
) -> None:
    """Wire the Duty_Status collaborators and build the service.

    Called once during startup from ``bootstrap/driver.py``. Like every other
    ``configure_*`` on this surface it assigns each module global
    unconditionally, so an argument a caller omits is reset to ``None`` — every
    call site must pass its full argument set.

    ``es_service`` is what writes and reads ``duty_status_events``; without it
    there is no service at all and both handlers fail closed.
    ``driver_repository`` is the preferred writer of the
    ``drivers_current.status`` projection. ``order_repository`` is read by the
    R13.6 gate, which fails **closed** when it is absent: a driver-submitted
    ``off_duty`` is then rejected with 409 ``ACTIVE_DELIVERY_IN_PROGRESS``
    rather than let through over a delivery in progress.
    """
    global _es_service, _driver_repository, _order_repository
    global _duty_status_service

    _es_service = es_service
    _driver_repository = driver_repository
    _order_repository = order_repository
    _duty_status_service = (
        DutyStatusService(
            es_service=es_service,
            driver_repository=driver_repository,
            order_repository=order_repository,
        )
        if es_service is not None
        else None
    )


def configured_duty_status_service() -> Optional[DutyStatusService]:
    """Return the service :func:`configure_duty_status_endpoints` built, or ``None``.

    The ops surface needs the same instance: an administrator-set transition on
    ``PATCH /api/ops/drivers/{driver_id}`` has to append an event exactly like a
    driver's own does (R13.16, R13.19). Exposing the one instance rather than
    letting each surface construct its own keeps a single set of collaborators
    behind the field.
    """
    return _duty_status_service


def _get_duty_status_service() -> DutyStatusService:
    """Return the configured :class:`DutyStatusService`, failing closed."""
    if _duty_status_service is None:
        logger.error(
            "Duty-status endpoints not configured. "
            "Call configure_duty_status_endpoints() during startup."
        )
        raise internal_error(
            message="Duty status changes are temporarily unavailable",
            details={"reason": "duty_status_endpoints_not_configured"},
        )
    return _duty_status_service


def _get_request_id(request: Request) -> str:
    """Extract ``request_id`` from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


def _resolve_history_subject(
    tenant: TenantContext, requested_driver_id: Optional[str]
) -> str:
    """Return the ``driver_id`` this caller is permitted to read.

    A ``dispatcher`` or ``admin`` may name any driver in its own tenant, and
    falls back to its own ``driver_id`` claim when it names none (R13.20). A
    caller holding only ``driver`` reads itself, and naming another driver is
    403 ``FORBIDDEN`` — not 404, which would confirm whether that driver exists
    (R13.21).

    Validates: Requirements 13.20, 13.21
    """
    requested = (requested_driver_id or "").strip()
    held = {role for role in (tenant.roles or []) if isinstance(role, str)}

    if held.intersection(HISTORY_WIDE_ROLES):
        subject = requested or (tenant.driver_id or "")
        if not subject:
            raise invalid_request(
                message=(
                    "driver_id is required to read a driver's duty-status "
                    "history"
                ),
                details={"required": "driver_id"},
            )
        return subject

    # Not a dispatcher or an admin: this has to be the driver itself, so the
    # composed driver gate applies (403 INSUFFICIENT_ROLE / DRIVER_IDENTITY_MISSING).
    own_driver_id = require_driver_identity(tenant)
    if requested and requested != own_driver_id:
        # R15.14 — the rejection names the rule, never the other identity.
        raise forbidden(
            message="A driver may read only its own duty-status history",
            details={"reason": "driver_id_mismatch"},
        )
    return own_driver_id


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class DutyStatusTransitionRequest(BaseModel):
    """Body for ``POST /api/driver/duty-status``.

    ``status`` is the target duty status. ``event_timestamp`` is the **client's**
    ISO-8601 stamp for when the driver flipped the control, which may predate the
    server's receipt by the length of an offline queue drain; the service records
    both it and its own ``server_received_at`` on the appended event (R13.12).
    There is no ``driver_id`` field: the subject is the verified session's.

    Validates: Requirements 13.1
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., min_length=1, max_length=32)
    event_timestamp: Optional[str] = Field(default=None, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/duty-status")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def set_duty_status(
    body: DutyStatusTransitionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> Any:
    """Record the calling driver's own duty-status transition.

    The caller is both the subject and the actor, and ``source`` is fixed to
    ``driver`` — which is what makes the service's R13.2 gate reject
    ``inactive`` on this path.

    Args:
        body: The target ``status``, the client's ``event_timestamp``, and an
            optional ``reason``.
        request: The inbound request, for the correlation id.
        tenant: The verified Auth_Context.
        idempotency: The ``X-Idempotency-Key`` lookup result.

    Returns:
        ``{"data": <event summary>, "request_id": ...}`` with HTTP 200.

    Raises:
        AppException: 403 ``FORBIDDEN`` for a driver-submitted ``inactive``
            (R13.2); 409 ``ACTIVE_DELIVERY_IN_PROGRESS`` while an assigned order
            is ``in_transit`` (R13.6); 400 ``INVALID_REQUEST`` for a status the
            vocabulary does not know; 202 ``DUTY_STATUS_PROJECTION_PENDING``
            when the event is durable but the projection write is not (R13.18).

    Validates: Requirements 13.1
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    driver_id = require_driver_identity(tenant)
    result = await _get_duty_status_service().transition(
        tenant.tenant_id,
        driver_id,
        body.status,
        actor_id=driver_id,
        source="driver",
        event_timestamp=body.event_timestamp or "",
        reason=body.reason,
    )

    payload: Dict[str, Any] = {
        "data": result,
        "request_id": _get_request_id(request),
    }
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, payload
        )
    return payload


@router.get("/duty-status/history")
@limiter.limit(_driver_rate)
async def get_duty_status_history(
    request: Request,
    range_start: str = Query(
        ...,
        description=(
            "Inclusive ISO-8601 lower bound on the event's client-asserted "
            "event_timestamp."
        ),
    ),
    range_end: str = Query(
        ...,
        description=(
            "Inclusive ISO-8601 upper bound on the event's client-asserted "
            "event_timestamp."
        ),
    ),
    driver_id: Optional[str] = Query(
        None,
        description=(
            "The driver whose history to read. A dispatcher or admin may name "
            "any driver in the tenant; a driver-role caller may name only "
            "itself and is rejected with 403 FORBIDDEN otherwise."
        ),
    ),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """Return one driver's duty-status events inside a time range, oldest first.

    Args:
        request: The inbound request, for the correlation id.
        range_start: Inclusive ISO-8601 lower bound on ``event_timestamp``.
        range_end: Inclusive ISO-8601 upper bound on ``event_timestamp``.
        driver_id: The subject, subject to the role scoping above.
        tenant: The verified Auth_Context.

    Returns:
        ``{"data": [<event>, ...], "count": n, "driver_id": ...,
        "range_start": ..., "range_end": ..., "request_id": ...}``, the events
        sorted by ``event_timestamp`` ascending.

    Raises:
        AppException: 403 ``FORBIDDEN`` when a ``driver``-role caller names a
            different ``driver_id`` (R13.21); 400 ``INVALID_REQUEST`` for a
            bound that is not ISO-8601 or an inverted range.

    Validates: Requirements 13.20, 13.21
    """
    # Every caller needs one of the three roles before the subject is resolved.
    require_role(tenant, DRIVER_ROLE, *HISTORY_WIDE_ROLES)
    subject_driver_id = _resolve_history_subject(tenant, driver_id)

    events = await _get_duty_status_service().history(
        tenant.tenant_id,
        subject_driver_id,
        range_start=range_start,
        range_end=range_end,
    )

    return {
        "data": events,
        "count": len(events),
        "driver_id": subject_driver_id,
        "range_start": range_start,
        "range_end": range_end,
        "request_id": _get_request_id(request),
    }


__all__ = [
    "router",
    "configure_duty_status_endpoints",
    "configured_duty_status_service",
    "DutyStatusTransitionRequest",
]
