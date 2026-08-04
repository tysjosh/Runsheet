"""Asset compliance-status read for the Fleet assignment surface.

Exposes ``GET /api/fleet/assets/{asset_id}/compliance`` — a per-asset
compliance-status signal the Drivers/Fleet assignment decision surface consumes
so an operator does not dispatch a non-compliant asset (Req 11.2).

This is the asset-side mirror of the driver correlation profile
(``GET /api/ops/drivers/{driver_id}/profile``, task 4): it returns the asset's
collapsed ``overall_status`` (``valid`` / ``expiring`` / ``expired`` /
``unknown``) plus the contributing certification/meter items, computed by
:class:`~compliance.services.asset_compliance_status_service.AssetComplianceStatusService`.

Wiring mirrors ``compliance/api/asset_certification_endpoints.py``: a
module-level service reference is populated by
:func:`configure_asset_compliance_api` at startup (``bootstrap/compliance.py``)
and every handler tenant-scopes through :func:`get_tenant_context`.

Validates: Requirements 11.2, 13.1.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request

from compliance.services.asset_compliance_status_service import (
    AssetComplianceStatusService,
)
from errors.exceptions import AppException, internal_error
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_asset_compliance_api()
# ---------------------------------------------------------------------------

_asset_compliance_service: Optional[AssetComplianceStatusService] = None

# NOTE: the prefix sits under the Fleet surface (``/api/fleet/assets``) so the
# assignment UI reaches the asset's compliance signal from the same namespace
# it lists assets. The ``/{asset_id}/compliance`` path is distinct from the
# ``/{asset_id}`` asset read in ``data_endpoints.py`` (no route collision).
router = APIRouter(
    prefix="/api/fleet/assets",
    tags=["Compliance"],
)


def configure_asset_compliance_api(
    *,
    asset_compliance_status_service: AssetComplianceStatusService,
) -> None:
    """Wire the AssetComplianceStatusService into this module.

    Called once during application startup (``bootstrap/compliance.py``) after
    both the AssetCertificationService and MeterAuditService have been
    instantiated. Tests inject a fake so the router can be exercised without ES.
    """
    global _asset_compliance_service
    _asset_compliance_service = asset_compliance_status_service


def _get_service() -> AssetComplianceStatusService:
    """Return the configured AssetComplianceStatusService or raise."""
    if _asset_compliance_service is None:
        raise RuntimeError(
            "Asset Compliance API not configured. "
            "Call configure_asset_compliance_api() during startup."
        )
    return _asset_compliance_service


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# GET /api/fleet/assets/{asset_id}/compliance
# ---------------------------------------------------------------------------


@router.get("/{asset_id}/compliance")
async def get_asset_compliance_status(
    request: Request,
    asset_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Return the collapsed compliance status for an asset.

    Aggregates the asset's certifications and meter calibrations into a single
    ``overall_status`` (``valid`` / ``expiring`` / ``expired`` / ``unknown``)
    so the Fleet assignment surface can flag a non-compliant asset before
    dispatch. All reads are tenant-scoped (Req 5.3).

    An asset with no certification or meter records returns
    ``overall_status="unknown"`` / ``has_records=false`` rather than a 404 —
    "no compliance data" is a valid (unlinked) state for the chip, not an error.

    Validates: Requirements 11.2, 13.1.
    """
    svc = _get_service()

    try:
        summary = await svc.get_asset_compliance_summary(
            tenant.tenant_id, asset_id
        )
    except AppException:
        raise
    except Exception as exc:  # noqa: BLE001 - never 500 the assignment read
        logger.error(
            "asset_compliance.status: unexpected error for tenant=%s asset=%s: %s",
            tenant.tenant_id,
            asset_id,
            exc,
        )
        raise internal_error(
            message="Failed to compute asset compliance status.",
            details={"asset_id": asset_id},
        )

    return {
        "data": summary.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_asset_compliance_api",
    "router",
]
