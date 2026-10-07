"""Portal ACH payments: the connector registry (design §3.1 "Portal Stripe factory").

FEAT-001 adds only the registry that ``GET /api/portal/me`` reads to answer
``payments_available``. ``bootstrap/agents.py`` registers the portal factory
(``_portal_stripe_connector_factory``) and payment service in FEAT-005; until
then :func:`portal_connector` returns ``None`` and payments are unavailable.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

#: ``async (tenant_id) -> connector | None``: an *enabled* Stripe connector.
PortalConnectorFactory = Callable[[str], Awaitable[Optional[Any]]]

_connector_factory: Optional[PortalConnectorFactory] = None
_payment_service: Optional[Any] = None


def configure_portal_payments(
    connector_factory: Optional[PortalConnectorFactory] = None,
    payment_service: Optional[Any] = None,
) -> None:
    """Register the portal Stripe factory and the commerce payment service."""
    global _connector_factory, _payment_service
    _connector_factory = connector_factory
    _payment_service = payment_service


def get_portal_payment_service() -> Optional[Any]:
    return _payment_service


async def portal_connector(tenant_id: str) -> Optional[Any]:
    """The tenant's enabled portal connector, or ``None``.

    ``None`` when no factory is configured, the factory finds no enabled
    instance, or the factory raises (logged at WARN).
    """
    if _connector_factory is None:
        return None
    try:
        return await _connector_factory(tenant_id)
    except Exception as exc:  # noqa: BLE001 — unavailable, never an error
        logger.warning(
            "Portal payment connector lookup failed for tenant=%s: %s",
            tenant_id,
            type(exc).__name__,
        )
        return None


__all__ = [
    "PortalConnectorFactory",
    "configure_portal_payments",
    "get_portal_payment_service",
    "portal_connector",
]
