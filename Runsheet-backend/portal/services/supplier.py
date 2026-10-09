"""The supplier name a customer sees (customer portal PE1)."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Shown when the tenant has no display name (portal-fixes A1). Never the
#: tenant id: a slug such as ``demo-tenant`` means nothing to a customer.
SUPPLIER_FALLBACK = "Your fuel supplier"


async def supplier_name(tenant_id: str) -> str:
    """The tenant's display name from tenant settings, else
    :data:`SUPPLIER_FALLBACK`.

    Never raises: the name is cosmetic, so a settings failure falls back like
    an unset name does.
    """
    from ops.middleware.tenant_guard import get_tenant_settings_service

    service = get_tenant_settings_service()
    if service is None:
        return SUPPLIER_FALLBACK
    try:
        name = await service.get_display_name(tenant_id)
    except Exception as exc:  # noqa: BLE001 — cosmetic, use the fallback
        logger.warning(
            "Portal: tenant display name read failed for tenant=%s: %s",
            tenant_id,
            type(exc).__name__,
        )
        return SUPPLIER_FALLBACK
    return name or SUPPLIER_FALLBACK


__all__ = ["SUPPLIER_FALLBACK", "supplier_name"]
