"""The supplier name a customer sees (customer portal PE1)."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def supplier_name(tenant_id: str) -> str:
    """The tenant's display name from tenant settings, else the tenant id.

    Never raises: the name is cosmetic, so a settings failure falls back to
    the id like an unset name does.
    """
    from ops.middleware.tenant_guard import get_tenant_settings_service

    service = get_tenant_settings_service()
    if service is None:
        return tenant_id
    try:
        name = await service.get_display_name(tenant_id)
    except Exception as exc:  # noqa: BLE001 — cosmetic, fall back to the id
        logger.warning(
            "Portal: tenant display name read failed for tenant=%s: %s",
            tenant_id,
            type(exc).__name__,
        )
        return tenant_id
    return name or tenant_id


__all__ = ["supplier_name"]
