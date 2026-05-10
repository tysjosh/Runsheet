"""IFTA Reporter REST endpoints for the Fuel Compliance Backbone.

Exposes endpoints for generating quarterly IFTA reports, querying fleet MPG,
recording manual mileage adjustments, retrieving adjustment history, and
checking data completeness under the ``/api/compliance/ifta`` prefix
(design §7, "REST API Endpoints (New)").

Endpoints:

* ``GET  /api/compliance/ifta/report?quarter=2026-Q1`` — Generate and
  return the quarterly IFTA report (Req 7.4).
* ``GET  /api/compliance/ifta/fleet-mpg?quarter=2026-Q1`` — Return the
  fleet MPG for a quarter (Req 7.5).
* ``POST /api/compliance/ifta/adjustments`` — Record a manual mileage
  adjustment (Req 7.7).
* ``GET  /api/compliance/ifta/adjustments?quarter=2026-Q1`` — Get
  adjustment history for a quarter (Req 7.7).
* ``GET  /api/compliance/ifta/completeness?quarter=2026-Q1`` — Check
  data completeness for a quarter (Req 7.6).

Wiring pattern mirrors ``compliance/api/driver_endpoints.py``:

1. A module-level ``_ifta_reporter`` is populated by
   :func:`configure_ifta_api` at application startup (see
   ``bootstrap/compliance.py``).
2. Each handler extracts the tenant from :func:`get_tenant_context` so
   all queries are tenant-scoped (Constraint C3).
3. ``AppException`` errors raised by the service layer are propagated
   to the global exception handler registered in ``main.py``.

Validates: Requirements 7.4, 7.5, 7.6, 7.7
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from compliance.services.ifta_reporter import IFTAReporter
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_ifta_api()
# ---------------------------------------------------------------------------

_ifta_reporter: Optional[IFTAReporter] = None

router = APIRouter(prefix="/api/compliance/ifta", tags=["compliance-ifta"])


# Quarter format regex: YYYY-Q[1-4]
_QUARTER_PATTERN = re.compile(r"^\d{4}-Q[1-4]$")


def configure_ifta_api(*, ifta_reporter: IFTAReporter) -> None:
    """Wire the IFTAReporter into this module.

    Called once during application startup (``bootstrap/compliance.py``)
    so that per-request handlers can delegate to the service without
    taking a hard import dependency on the container.

    Args:
        ifta_reporter: The application-scoped IFTAReporter instance.
    """
    global _ifta_reporter
    _ifta_reporter = ifta_reporter


def _get_ifta_reporter() -> IFTAReporter:
    """Return the configured IFTAReporter or raise."""
    if _ifta_reporter is None:
        raise RuntimeError(
            "IFTA API not configured. Call configure_ifta_api() during startup."
        )
    return _ifta_reporter


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""
    return getattr(request.state, "request_id", "unknown")


def _validate_quarter(quarter: str) -> None:
    """Validate the quarter format (YYYY-Q[1-4]).

    Raises:
        HTTPException: If the quarter format is invalid.
    """
    if not _QUARTER_PATTERN.match(quarter):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ifta.invalid_quarter_format",
                "message": (
                    f"Invalid quarter format: '{quarter}'. "
                    "Expected format: YYYY-Q[1-4] (e.g., '2026-Q1')."
                ),
            },
        )


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class MileageAdjustmentRequest(BaseModel):
    """Body for ``POST /api/compliance/ifta/adjustments`` (Req 7.7).

    Records a manual mileage adjustment for a truck in a specific
    jurisdiction and quarter. Supports both positive (adding miles)
    and negative (subtracting miles) adjustments.
    """

    model_config = ConfigDict(extra="forbid")

    truck_id: str = Field(
        ...,
        description="Identifier of the truck being adjusted.",
    )
    jurisdiction: str = Field(
        ...,
        description="2-letter US state code (e.g., 'TX', 'OK').",
    )
    miles: float = Field(
        ...,
        description="Miles adjustment (positive to add, negative to subtract).",
    )
    quarter: str = Field(
        ...,
        description="Calendar quarter identifier, e.g. '2026-Q1'.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        description="Reason for the manual adjustment (audit trail).",
    )


# ---------------------------------------------------------------------------
# GET /api/compliance/ifta/report?quarter=...
# ---------------------------------------------------------------------------


@router.get("/report")
async def get_ifta_report(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    quarter: str = Query(
        ...,
        description="Calendar quarter to generate the report for (e.g., '2026-Q1').",
    ),
) -> Dict[str, Any]:
    """Generate and return the quarterly IFTA report.

    Produces a per-truck IFTA summary showing jurisdiction, total_miles,
    taxable_miles, tax_paid_gallons, net_taxable_gallons, tax_rate, and
    tax_due for each state. Trucks with missing Geotab data are flagged
    as ``ifta_data_incomplete`` and excluded from the automated return.

    Validates: Requirement 7.4
    """
    _validate_quarter(quarter)
    svc = _get_ifta_reporter()

    try:
        report = await svc.generate_quarterly_report(
            tenant.tenant_id, quarter
        )
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "ifta.report: unexpected error for tenant=%s quarter=%s: %s",
            tenant.tenant_id,
            quarter,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ifta.report_failed",
                "message": "Failed to generate IFTA quarterly report.",
            },
        )

    logger.info(
        "ifta.report: tenant=%s quarter=%s trucks=%d",
        tenant.tenant_id,
        quarter,
        report.truck_count,
    )

    return {
        "data": report.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/ifta/fleet-mpg?quarter=...
# ---------------------------------------------------------------------------


@router.get("/fleet-mpg")
async def get_fleet_mpg(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    quarter: str = Query(
        ...,
        description="Calendar quarter to compute fleet MPG for (e.g., '2026-Q1').",
    ),
) -> Dict[str, Any]:
    """Return the fleet average MPG for a quarter.

    Fleet MPG = total_miles / total_gallons across all qualified vehicles
    for the quarter. Returns 0.0 if no fuel data is available.

    Validates: Requirement 7.5
    """
    _validate_quarter(quarter)
    svc = _get_ifta_reporter()

    try:
        fleet_mpg = await svc.compute_fleet_mpg(tenant.tenant_id, quarter)
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "ifta.fleet_mpg: unexpected error for tenant=%s quarter=%s: %s",
            tenant.tenant_id,
            quarter,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ifta.fleet_mpg_failed",
                "message": "Failed to compute fleet MPG.",
            },
        )

    return {
        "data": {
            "quarter": quarter,
            "fleet_mpg": fleet_mpg,
        },
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/compliance/ifta/adjustments
# ---------------------------------------------------------------------------


@router.post("/adjustments", status_code=201)
async def create_mileage_adjustment(
    request: Request,
    body: MileageAdjustmentRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Record a manual mileage adjustment with an audit trail.

    Creates a new adjustment record in the IFTA mileage index with
    ``source="manual_adjustment"``. Supports both positive (adding miles)
    and negative (subtracting miles) adjustments for corrections
    identified during quarterly review.

    The operator_id is extracted from the tenant context (authenticated
    user). The reason field is required for audit trail purposes.

    Validates: Requirement 7.7
    """
    _validate_quarter(body.quarter)
    svc = _get_ifta_reporter()

    # Extract operator_id from the tenant context (the authenticated user)
    operator_id = getattr(tenant, "user_id", None) or tenant.tenant_id

    try:
        adjustment = await svc.record_manual_adjustment(
            tenant_id=tenant.tenant_id,
            truck_id=body.truck_id,
            jurisdiction=body.jurisdiction,
            miles=body.miles,
            quarter=body.quarter,
            operator_id=operator_id,
            reason=body.reason,
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "ifta.invalid_adjustment",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "ifta.adjustments.create: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ifta.adjustment_failed",
                "message": "Failed to record mileage adjustment.",
            },
        )

    logger.info(
        "ifta.adjustments.create: tenant=%s truck=%s jurisdiction=%s "
        "miles=%.1f quarter=%s",
        tenant.tenant_id,
        body.truck_id,
        body.jurisdiction,
        body.miles,
        body.quarter,
    )

    return {
        "data": adjustment.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/ifta/adjustments?quarter=...
# ---------------------------------------------------------------------------


@router.get("/adjustments")
async def get_adjustment_history(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    quarter: str = Query(
        ...,
        description="Calendar quarter to retrieve adjustments for (e.g., '2026-Q1').",
    ),
) -> Dict[str, Any]:
    """Get manual mileage adjustment history for a quarter.

    Returns all manual adjustment records for the tenant and quarter,
    sorted by created_at descending (most recent first).

    Validates: Requirement 7.7
    """
    _validate_quarter(quarter)
    svc = _get_ifta_reporter()

    try:
        adjustments = await svc.get_adjustment_history(
            tenant.tenant_id, quarter
        )
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "ifta.adjustments.list: unexpected error for tenant=%s quarter=%s: %s",
            tenant.tenant_id,
            quarter,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ifta.adjustments_list_failed",
                "message": "Failed to retrieve adjustment history.",
            },
        )

    return {
        "data": [adj.model_dump(mode="json") for adj in adjustments],
        "count": len(adjustments),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/ifta/completeness?quarter=...
# ---------------------------------------------------------------------------


@router.get("/completeness")
async def check_data_completeness(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    quarter: str = Query(
        ...,
        description="Calendar quarter to check completeness for (e.g., '2026-Q1').",
    ),
) -> Dict[str, Any]:
    """Check data completeness for a quarter.

    Identifies trucks in the fleet that have missing Geotab telemetry
    data for the given quarter. Flagged trucks are excluded from the
    automated IFTA return and the fleet manager is alerted.

    Validates: Requirement 7.6
    """
    _validate_quarter(quarter)
    svc = _get_ifta_reporter()

    try:
        flags = await svc.check_data_completeness(tenant.tenant_id, quarter)
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "ifta.completeness: unexpected error for tenant=%s quarter=%s: %s",
            tenant.tenant_id,
            quarter,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ifta.completeness_check_failed",
                "message": "Failed to check data completeness.",
            },
        )

    return {
        "data": [flag.model_dump(mode="json") for flag in flags],
        "count": len(flags),
        "complete": len(flags) == 0,
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_ifta_api",
    "router",
    "MileageAdjustmentRequest",
]
