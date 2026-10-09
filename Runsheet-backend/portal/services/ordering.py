"""Whether a tenant's portal customers can request deliveries (portal-fixes B2).

Portal ordering is its own per-tenant setting, default ON for a tenant with
the portal enabled, set by a tenant admin (``PUT /api/commerce/portal-settings``).
It does not read the shared ``order_intake_pipeline`` rollout flag: that flag
governs the staff and integration intake channels, and the portal request
path writes through the pipeline whatever its state (``ingest_portal``).

The tenant's time zone for portal dates lives here too, because both come
from tenant settings and both are on ``/api/portal/me``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _service():
    from ops.middleware.tenant_guard import get_tenant_settings_service

    return get_tenant_settings_service()


async def portal_ordering_enabled(tenant_id: str) -> bool:
    """The tenant's setting. With no settings store wired the default (on)
    applies; a failed read is off (fail closed)."""
    service = _service()
    if service is None:
        return True
    try:
        return bool(await service.get_portal_ordering_enabled(tenant_id))
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning(
            "Portal: ordering setting read failed for tenant=%s: %s",
            tenant_id,
            type(exc).__name__,
        )
        return False


async def set_portal_ordering_enabled(tenant_id: str, enabled: bool) -> bool:
    service = _service()
    if service is None:
        from errors.exceptions import portal_unavailable

        raise portal_unavailable()
    return await service.set_portal_ordering_enabled(tenant_id, enabled)


def tenant_time_zone(tenant_id: str) -> str:
    """The tenant's IANA zone, by the Dispatch Board's rule (K2.5)."""
    from fuel.services.driver_daily_reset import get_tenant_timezone

    return get_tenant_timezone(tenant_id)


__all__ = [
    "portal_ordering_enabled",
    "set_portal_ordering_enabled",
    "tenant_time_zone",
]
