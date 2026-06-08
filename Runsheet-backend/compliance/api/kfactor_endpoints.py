"""K-Factor Calibration REST endpoints for the Fuel Compliance Backbone.

Exposes endpoints for the K-factor calibration dashboard, approving
adjustments, computing variance, and suggesting new K-factors under the
``/api/compliance/kfactor`` prefix (design §9, "REST API Endpoints (New)").

Endpoints:

* ``GET  /api/compliance/kfactor/dashboard`` — K-Factor Calibration
  dashboard returning tanks sorted by variance (Req 9.4).
* ``POST /api/compliance/kfactor/{tank_id}/approve`` — Approve a
  K-factor adjustment for a customer tank (Req 9.5, 9.6).
* ``GET  /api/compliance/kfactor/{tank_id}/variance`` — Compute
  variance for a specific delivery (Req 9.1, 9.2).
* ``GET  /api/compliance/kfactor/{tank_id}/suggest`` — Get suggested
  K-factor for a tank (Req 9.3).

Wiring pattern mirrors ``compliance/api/ifta_endpoints.py``:

1. A module-level ``_kfactor_service`` is populated by
   :func:`configure_kfactor_api` at application startup (see
   ``bootstrap/compliance.py``).
2. Each handler extracts the tenant from :func:`get_tenant_context` so
   all queries are tenant-scoped (Constraint C3).
3. ``AppException`` errors raised by the service layer are propagated
   to the global exception handler registered in ``main.py``.

Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from compliance.services.kfactor_calibration_service import (
    KFactorCalibrationService,
)
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_kfactor_api()
# ---------------------------------------------------------------------------

_kfactor_service: Optional[KFactorCalibrationService] = None

router = APIRouter(prefix="/api/compliance/kfactor", tags=["Compliance"])


def configure_kfactor_api(*, kfactor_service: KFactorCalibrationService) -> None:
    """Wire the KFactorCalibrationService into this module.

    Called once during application startup (``bootstrap/compliance.py``)
    so that per-request handlers can delegate to the service without
    taking a hard import dependency on the container.

    Args:
        kfactor_service: The application-scoped KFactorCalibrationService instance.
    """
    global _kfactor_service
    _kfactor_service = kfactor_service


def _get_kfactor_service() -> KFactorCalibrationService:
    """Return the configured KFactorCalibrationService or raise."""
    if _kfactor_service is None:
        raise RuntimeError(
            "K-Factor API not configured. Call configure_kfactor_api() during startup."
        )
    return _kfactor_service


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ApproveAdjustmentRequest(BaseModel):
    """Body for ``POST /api/compliance/kfactor/{tank_id}/approve`` (Req 9.5).

    Approves a K-factor adjustment for a customer tank. The operator_id
    is extracted from the tenant context (authenticated user).
    """

    model_config = ConfigDict(extra="forbid")

    new_kfactor: float = Field(
        ...,
        gt=0,
        description="The new K-factor value to apply (must be positive).",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Optional reason for the adjustment (audit trail).",
    )


# ---------------------------------------------------------------------------
# GET /api/compliance/kfactor/dashboard
# ---------------------------------------------------------------------------


@router.get("/dashboard")
async def get_calibration_dashboard(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Return the K-factor calibration dashboard.

    Returns a list of KFactorEntry records for all customer tanks in the
    tenant, sorted by variance (highest absolute variance first). Tanks
    with fewer than 3 deliveries are returned in read-only mode with a
    message indicating insufficient data for recalibration.

    Validates: Requirement 9.4, 9.7
    """
    svc = _get_kfactor_service()

    try:
        entries = await svc.get_calibration_dashboard(tenant.tenant_id)
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "kfactor.dashboard: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "kfactor.dashboard_failed",
                "message": "Failed to retrieve K-factor calibration dashboard.",
            },
        )

    logger.info(
        "kfactor.dashboard: tenant=%s entries=%d",
        tenant.tenant_id,
        len(entries),
    )

    return {
        "data": [entry.model_dump(mode="json") for entry in entries],
        "count": len(entries),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/compliance/kfactor/{tank_id}/approve
# ---------------------------------------------------------------------------


@router.post("/{tank_id}/approve", status_code=200)
async def approve_kfactor_adjustment(
    request: Request,
    tank_id: str,
    body: ApproveAdjustmentRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Approve a K-factor adjustment for a customer tank.

    Updates the customer tank's K-factor, records the old and new values
    with timestamp and operator_id, and notifies the tank forecasting
    agent of the change.

    The operator_id is extracted from the tenant context (authenticated
    user). The adjustment is rejected if the tank has fewer than 3
    deliveries (Req 9.7).

    Validates: Requirement 9.5, 9.6
    """
    svc = _get_kfactor_service()

    # Extract operator_id from the tenant context (the authenticated user)
    operator_id = getattr(tenant, "user_id", None) or tenant.tenant_id

    try:
        adjustment = await svc.approve_adjustment(
            tank_id=tank_id,
            new_kfactor=body.new_kfactor,
            operator_id=operator_id,
            tenant_id=tenant.tenant_id,
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "kfactor.invalid_adjustment",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "kfactor.approve: unexpected error for tenant=%s tank=%s: %s",
            tenant.tenant_id,
            tank_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "kfactor.approve_failed",
                "message": "Failed to approve K-factor adjustment.",
            },
        )

    logger.info(
        "kfactor.approve: tenant=%s tank=%s old=%.4f new=%.4f operator=%s",
        tenant.tenant_id,
        tank_id,
        adjustment.old_kfactor,
        adjustment.new_kfactor,
        operator_id,
    )

    return {
        "data": adjustment.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/kfactor/{tank_id}/variance?delivery_id=...
# ---------------------------------------------------------------------------


@router.get("/{tank_id}/variance")
async def get_variance(
    request: Request,
    tank_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    delivery_id: str = Query(
        ...,
        description="The delivery ID to compute variance for.",
    ),
) -> Dict[str, Any]:
    """Compute variance for a specific delivery.

    Computes predicted vs actual gallons using the customer's current
    K-factor multiplied by accumulated HDD since the last delivery.
    Returns the variance percentage and a suggested K-factor when the
    variance exceeds the configured threshold (default ±15%).

    Validates: Requirement 9.1, 9.2
    """
    svc = _get_kfactor_service()

    try:
        variance = await svc.compute_variance(
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "kfactor.variance_computation_error",
                "message": str(exc),
            },
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "kfactor.weather_provider_unavailable",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "kfactor.variance: unexpected error for tenant=%s tank=%s "
            "delivery=%s: %s",
            tenant.tenant_id,
            tank_id,
            delivery_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "kfactor.variance_failed",
                "message": "Failed to compute K-factor variance.",
            },
        )

    logger.info(
        "kfactor.variance: tenant=%s tank=%s delivery=%s variance=%.2f%%",
        tenant.tenant_id,
        tank_id,
        delivery_id,
        variance.variance_percent,
    )

    return {
        "data": variance.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/kfactor/{tank_id}/variance-history
# ---------------------------------------------------------------------------


@router.get("/{tank_id}/variance-history")
async def get_variance_history(
    request: Request,
    tank_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    limit: int = Query(
        10,
        ge=1,
        le=50,
        description="Maximum number of recent deliveries to score.",
    ),
) -> Dict[str, Any]:
    """Per-delivery predicted-vs-actual variance history for a tank.

    Powers the per-tank consumption drill-in on the K-Factor screen: a
    timeline of how each recent delivery's actual gallons compared to the
    HDD-based prediction, so an operator can judge whether a suggested
    K-factor is warranted.

    Deliveries that cannot be scored (no prior delivery to anchor the HDD
    window, zero HDD, etc.) are omitted. Returns an empty list when the
    weather provider is unavailable rather than erroring.

    Validates: Requirement 9.1, 9.2
    """
    svc = _get_kfactor_service()
    try:
        history = await svc.get_variance_history(
            tank_id=tank_id, tenant_id=tenant.tenant_id, limit=limit
        )
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "kfactor.variance_history: unexpected error for tenant=%s "
            "tank=%s: %s",
            tenant.tenant_id,
            tank_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "kfactor.variance_history_failed",
                "message": "Failed to load K-factor variance history.",
            },
        )

    return {
        "data": history,
        "count": len(history),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/kfactor/{tank_id}/suggest
# ---------------------------------------------------------------------------


@router.get("/{tank_id}/suggest")
async def suggest_kfactor(
    request: Request,
    tank_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Get suggested K-factor for a tank.

    Computes a suggested K-factor as actual_delivered_gallons /
    accumulated_HDD when the variance exceeds the configured threshold
    (default ±15%) and at least 3 deliveries exist for the tank.

    Returns null for suggested_kfactor when insufficient data exists
    or variance is within threshold.

    Validates: Requirement 9.3
    """
    svc = _get_kfactor_service()

    try:
        suggested = await svc.suggest_new_kfactor(
            tank_id=tank_id,
            tenant_id=tenant.tenant_id,
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "kfactor.suggest_error",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "kfactor.suggest: unexpected error for tenant=%s tank=%s: %s",
            tenant.tenant_id,
            tank_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "kfactor.suggest_failed",
                "message": "Failed to compute suggested K-factor.",
            },
        )

    logger.info(
        "kfactor.suggest: tenant=%s tank=%s suggested=%s",
        tenant.tenant_id,
        tank_id,
        suggested,
    )

    return {
        "data": {
            "tank_id": tank_id,
            "suggested_kfactor": suggested,
        },
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_kfactor_api",
    "router",
    "ApproveAdjustmentRequest",
]
