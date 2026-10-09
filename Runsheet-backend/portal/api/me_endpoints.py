"""``GET /api/portal/me``: account overview and capabilities (design §3.1, R3).

Collaborators come through :func:`configure_portal_me`, called from
``bootstrap/core.py`` (customer and invoice services, when the commerce
backbone is on) and ``bootstrap/fuel.py`` (the intake pipeline). Anything
unconfigured reads as "not available".
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

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
from portal.models import PortalMe, PortalMeasurementUnits, PortalMeEnvelope
from portal.services.portal_payment_service import portal_connector
from portal.services.ordering import portal_ordering_enabled, tenant_time_zone
from portal.services.supplier import supplier_name

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/portal",
    tags=["portal"],
    dependencies=[Depends(reject_scope_params)],
)

_services: dict[str, Any] = {
    "customer_service": None,
    "invoice_service": None,
    "order_intake_pipeline": None,
}


def configure_portal_me(**services: Any) -> None:
    """Register any of ``customer_service`` / ``invoice_service`` /
    ``order_intake_pipeline``. Keys not passed are left as they are, so the
    two bootstrap modules can each register what they own."""
    for name, value in services.items():
        if name not in _services:
            raise TypeError(f"unknown portal /me service: {name}")
        _services[name] = value


async def _ordering_available(tenant_id: str) -> bool:
    """The tenant's portal ordering setting (portal-fixes B2), and only when
    the request path is wired. The ``order_intake_pipeline`` rollout flag is
    not read: portal requests don't depend on it."""
    from portal.services.portal_order_service import get_configured_order_service

    if _services["order_intake_pipeline"] is None or get_configured_order_service() is None:
        return False
    return await portal_ordering_enabled(tenant_id)


def _invoices_available() -> bool:
    settings = get_settings()
    return bool(
        settings.commerce_backbone_enabled
        and settings.commerce_invoicing_enabled
        and _services["invoice_service"] is not None
    )


async def _email(user_id: str) -> str:
    from auth.password_admin import _email_for_st_user_id

    try:
        return await _email_for_st_user_id(user_id)
    except Exception:  # noqa: BLE001 — same fallback as /api/auth/account/me
        return ""


async def _open_balance(scope: PortalScope):
    """PE4: ``OpenBalance`` for the customer, or ``None`` (UI sums itself)."""
    from portal.services.scoped_readers import PortalInvoiceReader

    try:
        return await PortalInvoiceReader(_services["invoice_service"]).open_balance(scope)
    except Exception as exc:  # noqa: BLE001 — the panel falls back, /me answers
        logger.warning(
            "Portal /me: open balance read failed for tenant=%s: %s",
            scope.tenant_id,
            type(exc).__name__,
        )
        return None


async def _customer_display_name(scope: PortalScope) -> str:
    service = _services["customer_service"]
    if service is None:
        return ""
    customer: Optional[dict] = await service.get(scope.tenant_id, scope.customer_id)
    return str((customer or {}).get("display_name") or "")


@router.get("/me", response_model=PortalMeEnvelope)
@limiter.shared_limit(PORTAL_READ_LIMIT, scope=PORTAL_READ_SCOPE, key_func=portal_rate_key)
async def get_portal_me(
    request: Request,
    scope: PortalScope = Depends(require_portal_customer),
) -> PortalMeEnvelope:
    """The signed-in customer's account overview and capabilities."""
    invoices_available = _invoices_available()
    payments_available = bool(
        invoices_available and await portal_connector(scope.tenant_id) is not None
    )
    units = scope.tenant.measurement_units or {}
    balance = await _open_balance(scope) if invoices_available else None
    me = PortalMe(
        email=await _email(scope.user_id),
        customer_display_name=await _customer_display_name(scope),
        supplier_name=await supplier_name(scope.tenant_id),
        time_zone=tenant_time_zone(scope.tenant_id),
        ordering_available=await _ordering_available(scope.tenant_id),
        invoices_available=invoices_available,
        payments_available=payments_available,
        measurement_units=PortalMeasurementUnits(
            volume=str(units.get("volume", "gal")),
            distance=str(units.get("distance", "mi")),
        ),
        open_balance_cents=balance.cents if balance else None,
        open_invoice_count=balance.count if balance else None,
        overdue_count=balance.overdue if balance else None,
    )
    return PortalMeEnvelope(
        data=me, request_id=str(getattr(request.state, "request_id", "") or "")
    )


__all__ = ["configure_portal_me", "get_portal_me", "router"]
