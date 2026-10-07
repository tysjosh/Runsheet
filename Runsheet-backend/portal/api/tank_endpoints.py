"""Portal tank routes (design §7; R7).

* ``GET /api/portal/tanks`` — active tanks with forecast and next delivery.
* ``GET /api/portal/tanks/{customer_tank_id}`` — one tank.
* ``GET /api/portal/tanks/{customer_tank_id}/deliveries`` — 24-month history.

Data comes only through ``portal.services.scoped_readers``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from config.settings import get_settings
from middleware.rate_limiter import limiter
from portal.api._authz import (
    PORTAL_READ_LIMIT,
    PORTAL_READ_SCOPE,
    PortalScope,
    portal_rate_key,
    reject_scope_params,
    require_portal_customer,
)
from portal.models import (
    PortalTankDeliveryListEnvelope,
    PortalTankEnvelope,
    PortalTankListEnvelope,
    is_path_id,
)
from portal.services import projection
from portal.services.scoped_readers import get_portal_readers, tank_not_found

router = APIRouter(
    prefix="/api/portal",
    tags=["portal"],
    dependencies=[Depends(reject_scope_params)],
)

#: The tank list is one page: every active tank of the customer.
TANK_LIST_LIMIT = 500


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def _tank_id(customer_tank_id: str) -> str:
    if not is_path_id(customer_tank_id):
        raise tank_not_found(customer_tank_id)
    return customer_tank_id


@router.get("/tanks", response_model=PortalTankListEnvelope)
@limiter.shared_limit(PORTAL_READ_LIMIT, scope=PORTAL_READ_SCOPE, key_func=portal_rate_key)
async def list_portal_tanks(
    request: Request,
    scope: PortalScope = Depends(require_portal_customer),
) -> PortalTankListEnvelope:
    """Active tanks, each with its latest forecast and next delivery (R7.1-R7.3)."""
    readers = get_portal_readers()
    tanks = await readers.tanks.list(scope)
    forecasts = await readers.forecasts.latest_by_tank(scope) if tanks else {}
    next_by_tank = (
        await readers.orders.next_deliveries(
            scope, statuses=projection.OPEN_DELIVERY_STATUSES
        )
        if tanks
        else {}
    )
    now = projection.now_utc()
    stale_days = get_settings().portal_stale_reading_days
    data = [
        projection.project_tank(
            tank,
            forecast=forecasts.get(tank.customer_tank_id),
            next_delivery=next_by_tank.get(tank.customer_tank_id),
            now=now,
            stale_days=stale_days,
        )
        for tank in tanks
    ]
    return PortalTankListEnvelope(
        data=data, next_cursor=None, limit=TANK_LIST_LIMIT, request_id=_request_id(request)
    )


@router.get("/tanks/{customer_tank_id}", response_model=PortalTankEnvelope)
@limiter.shared_limit(PORTAL_READ_LIMIT, scope=PORTAL_READ_SCOPE, key_func=portal_rate_key)
async def get_portal_tank(
    request: Request,
    customer_tank_id: str,
    scope: PortalScope = Depends(require_portal_customer),
) -> PortalTankEnvelope:
    """One of the customer's active tanks; anyone else's is 404."""
    readers = get_portal_readers()
    tank = await readers.tanks.get(scope, _tank_id(customer_tank_id))
    forecasts = await readers.forecasts.latest_by_tank(scope)
    next_by_tank = await readers.orders.next_deliveries(
        scope,
        statuses=projection.OPEN_DELIVERY_STATUSES,
        customer_tank_id=tank.customer_tank_id,
    )
    data = projection.project_tank(
        tank,
        forecast=forecasts.get(tank.customer_tank_id),
        next_delivery=next_by_tank.get(tank.customer_tank_id),
        now=projection.now_utc(),
        stale_days=get_settings().portal_stale_reading_days,
    )
    return PortalTankEnvelope(data=data, request_id=_request_id(request))


@router.get(
    "/tanks/{customer_tank_id}/deliveries",
    response_model=PortalTankDeliveryListEnvelope,
)
@limiter.shared_limit(PORTAL_READ_LIMIT, scope=PORTAL_READ_SCOPE, key_func=portal_rate_key)
async def list_portal_tank_deliveries(
    request: Request,
    customer_tank_id: str,
    limit: int = Query(default=25, ge=1, le=50),
    cursor: Optional[str] = Query(default=None, max_length=256),
    scope: PortalScope = Depends(require_portal_customer),
) -> PortalTankDeliveryListEnvelope:
    """Delivered orders for one tank over the last 24 months, newest first (R7.4)."""
    readers = get_portal_readers()
    tank = await readers.tanks.get(scope, _tank_id(customer_tank_id))
    page = await readers.orders.deliveries(
        scope,
        tank.customer_tank_id,
        limit=limit,
        cursor=cursor,
        now=projection.now_utc(),
    )
    return PortalTankDeliveryListEnvelope(
        data=[projection.project_delivery(o) for o in page.items],
        next_cursor=page.next_cursor,
        limit=limit,
        request_id=_request_id(request),
    )


__all__ = ["router"]
