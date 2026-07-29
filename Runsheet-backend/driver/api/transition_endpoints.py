"""
Driver-initiated fuel-order status transitions — ``POST
/api/driver/orders/{order_id}/status``.

The handler holds four steps and no business rule of its own:

1. **Resolve.** :meth:`WorkRefResolver.resolve_order` turns the path parameter
   plus the verified :class:`TenantContext` into a ``Work_Ref``, answering 404
   for an absent or cross-tenant order and 403 ``FORBIDDEN`` for an order whose
   ``assigned_driver_id`` is not the caller (R4.2).
2. **Short-circuit an equal target status.** A request whose target equals the
   order's current status returns 200 with the *unchanged* order and appends
   nothing (R4.10). This check has to sit **before**
   ``apply_status_transition``: no status maps to itself in
   ``VALID_STATUS_TRANSITIONS``, so that method would answer 409
   ``INVALID_STATUS_TRANSITION`` for ``X → X``. It also sits before the gate
   stack — a no-op changes nothing, so there is nothing for a gate to protect.
3. **Run the gate stack.** :meth:`DriverTransitionGateStack.evaluate` applies
   the out-of-service, pre-trip, ``Dispatch_Eligibility``, and HOS gates in
   that fixed order, and only for a transition to ``in_transit``. The gates run
   here, on the driver path, and never inside ``OrderService`` — that service is
   shared with the agent mutation tools and with dispatcher-initiated
   transitions.
4. **Transition.** ``OrderService.apply_status_transition`` is the single writer
   of fuel-order status, so the state-machine guard, the delivery-window guard,
   the appended order event, the driver counters, the broadcast, and the
   subscribers all execute exactly as they do for every other caller (R4.1).

Nothing in this module re-implements a guard the state machine already carries.
``assert_transition`` answers 409 ``INVALID_STATUS_TRANSITION`` with
``{old_status, new_status}`` in details (R4.3) and ``STATUSES_REQUIRING_WINDOW``
already covers ``in_transit``, so a windowless order answers 409
``MISSING_DELIVERY_WINDOW`` (R4.4). The only pre-flight validation here is that
the requested status is a status the lifecycle knows at all; an unknown string
is a malformed request rather than an illegal transition.

R4.8's three stamps come from three different places: the acting ``driver_id``
travels as ``actor_user_id``, the server receipt stamp is
``apply_status_transition``'s own injected clock (``event_timestamp`` /
``ingested_at``), and the client's stamp is forwarded as
``client_event_timestamp`` into the free-form ``event_payload`` — no
``fuel_order_events`` mapping change.

``X-Idempotency-Key`` is handled exactly as on the POD path: a repeated key
replays the stored response, including the no-op response (R4.11).

Every rejection is an ``AppException`` from ``errors/exceptions.py`` — this
module raises **zero** raw ``HTTPException``.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Order Transition Path.

Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.8, 4.9, 4.10, 4.11
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder

from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.models import DriverStatusTransitionRequest
from driver.services.order_transition_service import (
    get_gate_stack,
    get_order_service,
    get_work_ref_resolver,
)
from driver.services.pod_otp_service import POD_OTP_FIELD
from errors.exceptions import internal_error, invalid_request
from fuel.order_state_machine import VALID_STATUS_TRANSITIONS
from middleware.rate_limiter import driver_rate_key, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

#: Every status the order lifecycle knows, as declared by the state machine.
#: Used for the one pre-flight check this module makes — that the requested
#: target is a status at all. Legality *from the order's current status* is the
#: state machine's decision, not this module's (R4.3).
KNOWN_ORDER_STATUSES: frozenset[str] = frozenset(VALID_STATUS_TRANSITIONS)

router = APIRouter(prefix="/api/driver", tags=["driver-transitions"])


# ---------------------------------------------------------------------------
# Collaborator access
# ---------------------------------------------------------------------------
#
# There is no ``configure_transition_endpoints`` here: the collaborators are
# wired once by
# ``driver/services/order_transition_service.configure_transition_endpoints``
# and read back through its accessors, so the gate stack and the router cannot
# end up holding two different service references.


def _require_resolver():
    """The order-keyed :class:`WorkRefResolver`, failing closed."""
    resolver = get_work_ref_resolver()
    if resolver is None:
        logger.error(
            "Driver transition endpoint not configured — no order_repository "
            "reached configure_transition_endpoints()"
        )
        raise internal_error(
            message="Driver status transitions are temporarily unavailable",
            details={"reason": "transition_endpoints_not_configured"},
        )
    return resolver


def _require_order_service():
    """``OrderService``, the single writer of fuel-order status, failing closed."""
    order_service = get_order_service()
    if order_service is None:
        logger.error(
            "Driver transition endpoint not configured — no order_service "
            "reached configure_transition_endpoints()"
        )
        raise internal_error(
            message="Driver status transitions are temporarily unavailable",
            details={"reason": "order_service_not_configured"},
        )
    return order_service


def _require_gate_stack():
    """The gate stack, failing closed rather than transitioning ungated."""
    stack = get_gate_stack()
    if stack is None:
        logger.error(
            "Driver transition endpoint not configured — no gate stack composed"
        )
        raise internal_error(
            message="Driver status transitions are temporarily unavailable",
            details={"reason": "transition_gates_not_configured"},
        )
    return stack


def _get_request_id(request: Request) -> str:
    """Extract ``request_id`` from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


def _envelope(
    order: Dict[str, Any], *, status_changed: bool, request_id: str
) -> Dict[str, Any]:
    """Build the response body.

    ``jsonable_encoder`` runs here rather than at serialization time because the
    same body is handed to the idempotency store, and the order document carries
    ``datetime`` values that a stored-then-replayed response has to survive.

    ``pod_otp`` is dropped before encoding, the same way
    :class:`~driver.services.work_service.DriverWorkService` drops it: the code
    ``PODOTPService`` provisions at dispatch is on the order document by the
    time the driver moves that order to ``in_transit``, and it must not appear
    in any ``/api/driver`` response body — nor in the idempotency store this
    body is replayed from (R5.26).
    """
    payload = jsonable_encoder(order)
    if isinstance(payload, dict):
        payload.pop(POD_OTP_FIELD, None)
    return {
        "data": payload,
        "status_changed": status_changed,
        "request_id": request_id,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/orders/{order_id}/status")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def transition_order_status(
    order_id: str,
    body: DriverStatusTransitionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> Any:
    """Transition one of the caller's own fuel orders.

    Args:
        order_id: The fuel order's identifier. The order must belong to the
            caller's tenant and name the caller in ``assigned_driver_id``.
        body: The target ``status`` and the client's own ``event_timestamp``.
        request: The inbound request, for the correlation id.
        tenant: The verified Auth_Context.
        idempotency: The ``X-Idempotency-Key`` lookup result.

    Returns:
        ``{"data": <order>, "status_changed": bool, "request_id": ...}`` with
        HTTP 200. ``status_changed`` is ``False`` for the no-op case, where the
        order comes back untouched and no event was appended.

    Raises:
        AppException: 403 ``FORBIDDEN`` when the order is not this driver's
            (R4.2); 404 when it is absent from the caller's tenant; 422 for a
            status the lifecycle does not know; 409
            ``INVALID_STATUS_TRANSITION`` (R4.3), ``MISSING_DELIVERY_WINDOW``
            (R4.4), or one of the four gate codes.

    Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.8, 4.9, 4.10, 4.11
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    target_status = (body.status or "").strip()
    if target_status not in KNOWN_ORDER_STATUSES:
        raise invalid_request(
            message="Unknown order status",
            details={
                "status": target_status,
                "allowed_statuses": sorted(KNOWN_ORDER_STATUSES),
            },
        )

    ref = await _require_resolver().resolve_order(order_id, tenant)
    order: Dict[str, Any] = dict(ref.order_doc or {})
    request_id = _get_request_id(request)

    # R4.10 — the no-op, decided before ``apply_status_transition`` (which
    # rejects ``X → X``) and before the gates (a no-op changes nothing).
    if order.get("status") == target_status:
        result = _envelope(order, status_changed=False, request_id=request_id)
        await _store(idempotency, tenant.tenant_id, result)
        return result

    await _require_gate_stack().evaluate(
        tenant_id=ref.tenant_id,
        driver_id=ref.driver_id,
        order=order,
        target_status=target_status,
    )

    updated = await _require_order_service().apply_status_transition(
        order=order,
        new_status=target_status,
        reason=body.reason,
        notes=body.notes,
        actor_user_id=ref.driver_id,
        client_event_timestamp=body.event_timestamp,
    )

    result = _envelope(updated, status_changed=True, request_id=request_id)
    await _store(idempotency, tenant.tenant_id, result)
    return result


async def _store(
    idempotency: IdempotencyResult, tenant_id: str, result: Dict[str, Any]
) -> None:
    """Persist the response for replay, when the caller supplied a key (R4.11)."""
    if idempotency.key:
        await store_idempotency_response(idempotency.key, tenant_id, result)
