"""Meter Audit REST endpoints for the Fuel Compliance Backbone.

Exposes CRUD operations for MeterRegistration records and the per-meter
audit trail under the ``/api/compliance/meters`` prefix (design §8,
"REST API Endpoints (New)").

Endpoints:

* ``GET  /api/compliance/meters`` — list meters with pagination and
  optional filters (Req 8.3).
* ``POST /api/compliance/meters`` — register a new meter (Req 8.1).
* ``GET  /api/compliance/meters/{meter_id}`` — get a single meter
  (Req 8.3).
* ``GET  /api/compliance/meters/{meter_id}/audit-trail`` — per-meter
  audit trail (Req 8.6).

Wiring pattern mirrors ``compliance/api/driver_endpoints.py``:

1. A module-level ``_meter_service`` is populated by
   :func:`configure_meter_api` at application startup (see
   ``bootstrap/compliance.py``).
2. Each handler extracts the tenant from :func:`get_tenant_context` so
   all queries are tenant-scoped (Constraint C3).
3. ``AppException`` errors raised by the service layer are propagated
   to the global exception handler registered in ``main.py``.

Validates: Requirements 8.1, 8.3, 8.6
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from compliance.api._authz import compliance_ops_dependency
from compliance.services.meter_audit_service import MeterAuditService
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_meter_api()
# ---------------------------------------------------------------------------

_meter_service: Optional[MeterAuditService] = None

# DOT / IRS records, gated to the operations roles. Attached to the router
# rather than to each handler so a route added later inherits it: every module
# in this package previously had no role check at all.
router = APIRouter(
    prefix="/api/compliance/meters", tags=["Compliance"],
    dependencies=[Depends(compliance_ops_dependency)],
)


def configure_meter_api(*, meter_service: MeterAuditService) -> None:
    """Wire the MeterAuditService into this module.

    Called once during application startup (``bootstrap/compliance.py``)
    so that per-request handlers can delegate to the service without
    taking a hard import dependency on the container.

    Args:
        meter_service: The application-scoped MeterAuditService instance.
    """
    global _meter_service
    _meter_service = meter_service


def _get_meter_service() -> MeterAuditService:
    """Return the configured MeterAuditService or raise."""
    if _meter_service is None:
        raise RuntimeError(
            "Meter API not configured. Call configure_meter_api() during startup."
        )
    return _meter_service


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class MeterCreateRequest(BaseModel):
    """Body for ``POST /api/compliance/meters`` (Req 8.1)."""

    model_config = ConfigDict(extra="forbid")

    meter_number: str = Field(
        ...,
        description="Physical meter serial/identification number.",
    )
    truck_id: str = Field(
        ...,
        description="Identifier of the truck this meter is installed on.",
    )
    calibration_certificate_number: str = Field(
        ...,
        description="Official calibration certificate number.",
    )
    calibration_date: date = Field(
        ...,
        description="Date the calibration was performed.",
    )
    calibration_expiry_date: date = Field(
        ...,
        description="Date the calibration expires.",
    )
    weights_measures_authority: str = Field(
        ...,
        description="The certifying weights and measures authority.",
    )
    status: Optional[str] = Field(
        default="active",
        description="Meter status: active or expired_calibration.",
    )


# ---------------------------------------------------------------------------
# GET /api/compliance/meters
# ---------------------------------------------------------------------------


@router.get("")
async def list_meters(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    truck_id: Optional[str] = Query(
        default=None,
        description="Filter by truck ID.",
    ),
    status: Optional[str] = Query(
        default=None,
        description="Filter by meter status: active or expired_calibration.",
    ),
    cursor: Optional[str] = Query(
        default=None,
        description=(
            "Cursor for keyset pagination — the meter_id of the last "
            "item on the previous page."
        ),
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Page size (max 200).",
    ),
) -> Dict[str, Any]:
    """List meters for the tenant with pagination and optional filters.

    Validates: Requirement 8.3
    """
    svc = _get_meter_service()

    try:
        result = await svc.list_meters(
            tenant.tenant_id,
            truck_id=truck_id,
            status=status,
            cursor=cursor,
            limit=limit,
        )
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "meters.list: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "meters.list_failed",
                "message": "Failed to list meters.",
            },
        )

    return {
        "data": result["items"],
        "next_cursor": result.get("next_cursor"),
        "limit": result.get("limit", limit),
        "count": len(result["items"]),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/compliance/meters
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_meter(
    request: Request,
    body: MeterCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Register a new meter.

    The router stamps ``tenant_id`` from the verified JWT context so
    clients cannot seed cross-tenant records. Input validation is
    delegated to the MeterRegistration Pydantic model via the service
    layer.

    Validates: Requirement 8.1
    """
    svc = _get_meter_service()

    try:
        meter_doc = await svc.register_meter(
            tenant.tenant_id,
            meter_number=body.meter_number,
            truck_id=body.truck_id,
            calibration_certificate_number=body.calibration_certificate_number,
            calibration_date=body.calibration_date,
            calibration_expiry_date=body.calibration_expiry_date,
            weights_measures_authority=body.weights_measures_authority,
            status=body.status or "active",
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "meters.invalid_payload",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "meters.create: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "meters.create_failed",
                "message": "Failed to register meter.",
            },
        )

    logger.info(
        "meters.create: tenant=%s meter=%s number=%s truck=%s",
        tenant.tenant_id,
        meter_doc.get("meter_id"),
        meter_doc.get("meter_number"),
        meter_doc.get("truck_id"),
    )

    return {
        "data": meter_doc,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/meters/{meter_id}
# ---------------------------------------------------------------------------


@router.get("/{meter_id}")
async def get_meter(
    request: Request,
    meter_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Retrieve a single meter by ID, scoped to the tenant.

    Returns HTTP 404 if the meter does not exist or does not belong
    to the requesting tenant.

    Validates: Requirement 8.3
    """
    svc = _get_meter_service()

    try:
        meter_doc = await svc.get_meter(tenant.tenant_id, meter_id)
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "meters.get: unexpected error for tenant=%s meter=%s: %s",
            tenant.tenant_id,
            meter_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "meters.get_failed",
                "message": "Failed to retrieve meter.",
            },
        )

    return {
        "data": meter_doc,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/meters/{meter_id}/audit-trail
# ---------------------------------------------------------------------------


@router.get("/{meter_id}/audit-trail")
async def get_meter_audit_trail(
    request: Request,
    meter_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    cursor: Optional[str] = Query(
        default=None,
        description=(
            "Cursor for keyset pagination — the audit_id of the last "
            "item on the previous page."
        ),
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Page size (max 200).",
    ),
) -> Dict[str, Any]:
    """Retrieve the per-meter audit trail.

    Returns all audit entries for a specific meter, ordered by
    timestamp descending (most recent first).

    Validates: Requirement 8.6
    """
    svc = _get_meter_service()

    try:
        result = await svc.get_meter_audit_trail(
            tenant.tenant_id,
            meter_id,
            cursor=cursor,
            limit=limit,
        )
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "meters.audit_trail: unexpected error for tenant=%s meter=%s: %s",
            tenant.tenant_id,
            meter_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "meters.audit_trail_failed",
                "message": "Failed to retrieve meter audit trail.",
            },
        )

    return {
        "data": result["items"],
        "next_cursor": result.get("next_cursor"),
        "limit": result.get("limit", limit),
        "count": len(result["items"]),
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_meter_api",
    "router",
    "MeterCreateRequest",
]
