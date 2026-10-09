"""Portal order routes (design §3, §4; R4).

* ``GET /api/portal/orders`` — the customer's orders, newest first.
* ``GET /api/portal/orders/{order_id}`` — one order.
* ``POST /api/portal/orders`` — request a delivery (order limit).
* ``POST /api/portal/orders/{order_id}/cancel`` — cancel while awaiting
  confirmation (cancel limit).

Data comes only through ``portal.services`` (scoped readers and
``PortalOrderService``); this module imports no store.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from middleware.rate_limiter import limiter
from portal.api._authz import (
    PORTAL_CANCEL_LIMIT,
    PORTAL_ORDER_LIMIT,
    PORTAL_READ_LIMIT,
    PORTAL_READ_SCOPE,
    PortalScope,
    portal_rate_key,
    reject_scope_params,
    require_portal_customer,
)
from portal.models import (
    PortalCancelRequest,
    PortalOrderEnvelope,
    PortalOrderListEnvelope,
    PortalOrderRequest,
    is_path_id,
)
from portal.services.portal_order_service import get_portal_order_service
from portal.services.scoped_readers import get_portal_readers, order_not_found

router = APIRouter(
    prefix="/api/portal",
    tags=["portal"],
    dependencies=[Depends(reject_scope_params)],
)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def _order_id(order_id: str) -> str:
    """A malformed id answers exactly like an unknown one (§3.2)."""
    if not is_path_id(order_id):
        raise order_not_found(order_id)
    return order_id


@router.get("/orders", response_model=PortalOrderListEnvelope)
@limiter.shared_limit(PORTAL_READ_LIMIT, scope=PORTAL_READ_SCOPE, key_func=portal_rate_key)
async def list_portal_orders(
    request: Request,
    limit: int = Query(default=25, ge=1, le=50),
    cursor: Optional[str] = Query(default=None, max_length=256),
    status_group: Optional[Literal["active", "past"]] = Query(
        default=None,
        description=(
            "PE7: ``active`` (not delivered, not delivered-failed, not "
            "cancelled) or ``past``. Omitted: every order."
        ),
    ),
    scope: PortalScope = Depends(require_portal_customer),
) -> PortalOrderListEnvelope:
    """The customer's orders, newest first (R4.11)."""
    service = get_portal_order_service()
    page = await get_portal_readers().orders.list(
        scope, limit=limit, cursor=cursor, status_group=status_group
    )
    data = await service.project_all(scope, page.items)
    return PortalOrderListEnvelope(
        data=data, next_cursor=page.next_cursor, limit=limit, request_id=_request_id(request)
    )


@router.get("/orders/{order_id}", response_model=PortalOrderEnvelope)
@limiter.shared_limit(PORTAL_READ_LIMIT, scope=PORTAL_READ_SCOPE, key_func=portal_rate_key)
async def get_portal_order(
    request: Request,
    order_id: str,
    scope: PortalScope = Depends(require_portal_customer),
) -> PortalOrderEnvelope:
    """One of the customer's orders; anyone else's is 404."""
    service = get_portal_order_service()
    order = await get_portal_readers().orders.get(scope, _order_id(order_id))
    return PortalOrderEnvelope(
        data=await service.project(scope, order), request_id=_request_id(request)
    )


@router.post("/orders", response_model=PortalOrderEnvelope, status_code=201)
@limiter.limit(PORTAL_ORDER_LIMIT, key_func=portal_rate_key)
async def create_portal_order(
    request: Request,
    body: PortalOrderRequest,
    scope: PortalScope = Depends(require_portal_customer),
) -> JSONResponse:
    """Request a delivery. 201 + ``Location`` when created; 200 with the
    original order for a replayed ``client_event_id`` (R4.1-R4.5)."""
    result = await get_portal_order_service().submit(scope, body, _request_id(request))
    request.state.portal_audit_target_ids = {"order_id": result.order.order_id}
    envelope = PortalOrderEnvelope(data=result.order, request_id=_request_id(request))
    headers = {}
    if result.status_code == 201:
        headers["Location"] = f"/api/portal/orders/{result.order.order_id}"
    return JSONResponse(
        status_code=result.status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


@router.post("/orders/{order_id}/cancel", response_model=PortalOrderEnvelope)
@limiter.limit(PORTAL_CANCEL_LIMIT, key_func=portal_rate_key)
async def cancel_portal_order(
    request: Request,
    order_id: str,
    body: PortalCancelRequest,
    scope: PortalScope = Depends(require_portal_customer),
) -> PortalOrderEnvelope:
    """Cancel a request that is still awaiting confirmation (R4.10)."""
    order = await get_portal_order_service().cancel(scope, _order_id(order_id))
    return PortalOrderEnvelope(data=order, request_id=_request_id(request))


__all__ = ["router"]
