"""Registration of the Compliance Backbone REST surface, behind its flag.

Lives here rather than in ``main.py`` for two reasons: ``main.py`` carries a
line-count ceiling that is meant to ratchet down, and these eight routers are
referenced nowhere else, so nothing is lost by moving both the imports and the
registration out of the top-level module.

What this gates, and what it deliberately does not
--------------------------------------------------

``compliance_backbone_enabled`` controls the eight compliance **routers**:
IFTA, terminal BOL, k-factor, driver qualification, asset certification, meter
tickets, asset compliance, and tax admin. None is exercised by the seven MVP
capabilities, so an MVP pilot can drop all 34 endpoints.

It does NOT gate the compliance *services*, which the delivery pipeline imports
directly and which are load-bearing:

======================  =======================================================
``DyedDieselEnforcer``  ``compartment_loading_agent``, ``invoice_service`` —
                        IRS dyed-fuel rules, not optional
``delivery_filter``     ``route_planning_agent``
``VCFCalculator``       ``reconciliation_service`` — temperature correction on
                        the volume that gets billed
``HOSStatus``           ``driver/hos_advisory_service`` — hours of service
======================  =======================================================

Gating those would break load building, routing, reconciliation, and driver
HOS. Shrinking an API surface is safe; deleting domain logic the pipeline
depends on is not. ``tests/unit/test_module_gating.py`` pins both halves of
that distinction.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)


def _compliance_routers() -> Tuple["APIRouter", ...]:
    """Import and return the eight compliance routers.

    Imported inside the function so a module-level import cycle cannot form
    between ``main`` and the compliance package, and so nothing is imported at
    all when the flag is off.
    """
    from compliance.api.asset_certification_endpoints import (
        router as asset_cert_router,
    )
    from compliance.api.asset_compliance_endpoints import (
        router as asset_compliance_router,
    )
    from compliance.api.driver_endpoints import router as driver_router
    from compliance.api.ifta_endpoints import router as ifta_router
    from compliance.api.kfactor_endpoints import router as kfactor_router
    from compliance.api.meter_endpoints import router as meter_router
    from compliance.api.tax_endpoints import router as tax_router
    from compliance.api.terminal_bol_endpoints import (
        router as terminal_bol_router,
    )

    return (
        tax_router,
        terminal_bol_router,
        ifta_router,
        kfactor_router,
        driver_router,
        asset_cert_router,
        meter_router,
        asset_compliance_router,
    )


def register(app: "FastAPI") -> int:
    """Register the compliance routers when the master flag is on.

    Returns the number of routers registered, so a caller or a smoke test can
    tell "flag off" (0) apart from "import blew up" (also 0 today, which is why
    the failure is logged at exception level rather than swallowed).
    """
    from config.settings import get_settings

    try:
        enabled = bool(getattr(get_settings(), "compliance_backbone_enabled", True))
    except Exception:
        # Settings can fail to construct during some test bootstraps. Default
        # to enabled so a config hiccup does not silently remove endpoints
        # that a deployment expects to be present.
        logger.exception(
            "Could not read compliance_backbone_enabled; defaulting to enabled"
        )
        enabled = True

    if not enabled:
        logger.info(
            "Compliance Backbone REST surface disabled "
            "(compliance_backbone_enabled=false); pipeline compliance "
            "services remain active"
        )
        return 0

    try:
        routers = _compliance_routers()
    except Exception:
        logger.exception(
            "Compliance router import failed; compliance endpoints will be "
            "unavailable"
        )
        return 0

    for router in routers:
        app.include_router(router)
    logger.info("Compliance Backbone: registered %d routers", len(routers))
    return len(routers)


__all__ = ["register"]
