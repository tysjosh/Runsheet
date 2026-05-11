"""Asset Certification REST endpoints for the Fuel Compliance Backbone.

Exposes CRUD operations for AssetCertification records and the fleet
certification dashboard under the ``/api/compliance/asset-certifications``
prefix (design §13, "REST API Endpoints (New)").

Endpoints:

* ``GET  /api/compliance/asset-certifications`` — list certifications with
  pagination and optional filters (Req 13.1).
* ``POST /api/compliance/asset-certifications`` — create a new certification
  record (Req 13.1).
* ``GET  /api/compliance/asset-certifications/dashboard`` — fleet certification
  dashboard (Req 13.7).
* ``GET  /api/compliance/asset-certifications/{cert_id}`` — get a single
  certification (Req 13.1).
* ``PUT  /api/compliance/asset-certifications/{cert_id}`` — update a
  certification record (Req 13.1).

Wiring pattern mirrors ``compliance/api/driver_endpoints.py``:

1. A module-level ``_asset_cert_service`` is populated by
   :func:`configure_asset_certification_api` at application startup (see
   ``bootstrap/compliance.py``).
2. Each handler extracts the tenant from :func:`get_tenant_context` so
   all queries are tenant-scoped (Constraint C3).
3. ``AppException`` errors raised by the service layer are propagated
   to the global exception handler registered in ``main.py``.

Validates: Requirements 13.1, 13.7
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from compliance.services.asset_certification_service import (
    AssetCertificationService,
)
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_asset_certification_api()
# ---------------------------------------------------------------------------

_asset_cert_service: Optional[AssetCertificationService] = None

router = APIRouter(
    prefix="/api/compliance/asset-certifications",
    tags=["Compliance"],
)


def configure_asset_certification_api(
    *, asset_certification_service: AssetCertificationService
) -> None:
    """Wire the AssetCertificationService into this module.

    Called once during application startup (``bootstrap/compliance.py``)
    so that per-request handlers can delegate to the service without
    taking a hard import dependency on the container.

    Args:
        asset_certification_service: The application-scoped
            AssetCertificationService instance.
    """
    global _asset_cert_service
    _asset_cert_service = asset_certification_service


def _get_asset_cert_service() -> AssetCertificationService:
    """Return the configured AssetCertificationService or raise."""
    if _asset_cert_service is None:
        raise RuntimeError(
            "Asset Certification API not configured. "
            "Call configure_asset_certification_api() during startup."
        )
    return _asset_cert_service


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AssetCertificationCreateRequest(BaseModel):
    """Body for ``POST /api/compliance/asset-certifications`` (Req 13.1)."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(
        ...,
        description="ID of the vehicle/trailer asset being certified.",
    )
    certification_type: str = Field(
        ...,
        description=(
            "Certification type: V_test, K_test, I_test, P_test, "
            "UT_test, meter_seal, or fire_extinguisher."
        ),
    )
    certification_date: date = Field(
        ...,
        description="Date the certification was performed.",
    )
    expiry_date: date = Field(
        ...,
        description="Date the certification expires.",
    )
    inspector_name: str = Field(
        ...,
        description="Name of the inspector who performed the certification.",
    )
    certificate_number: str = Field(
        ...,
        description="Certificate or inspection report number.",
    )
    status: Optional[str] = Field(
        default="valid",
        description="Certification status: valid, expiring_soon, or expired.",
    )


class AssetCertificationUpdateRequest(BaseModel):
    """Body for ``PUT /api/compliance/asset-certifications/{cert_id}`` (Req 13.1).

    All fields are optional — only provided fields are updated.
    """

    model_config = ConfigDict(extra="forbid")

    certification_date: Optional[date] = Field(
        default=None,
        description="Date the certification was performed.",
    )
    expiry_date: Optional[date] = Field(
        default=None,
        description="Date the certification expires.",
    )
    inspector_name: Optional[str] = Field(
        default=None,
        description="Name of the inspector.",
    )
    certificate_number: Optional[str] = Field(
        default=None,
        description="Certificate or inspection report number.",
    )
    status: Optional[str] = Field(
        default=None,
        description="Certification status: valid, expiring_soon, expired, or superseded.",
    )


# ---------------------------------------------------------------------------
# GET /api/compliance/asset-certifications
# ---------------------------------------------------------------------------


@router.get("")
async def list_asset_certifications(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    asset_id: Optional[str] = Query(
        default=None,
        description="Filter by asset ID.",
    ),
    certification_type: Optional[str] = Query(
        default=None,
        description="Filter by certification type.",
    ),
    status: Optional[str] = Query(
        default=None,
        description="Filter by status: valid, expiring_soon, expired, or superseded.",
    ),
    cursor: Optional[str] = Query(
        default=None,
        description=(
            "Cursor for keyset pagination — the cert_id of the last "
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
    """List asset certifications for the tenant with pagination and optional filters.

    Validates: Requirement 13.1
    """
    svc = _get_asset_cert_service()

    try:
        result = await svc.list(
            tenant.tenant_id,
            asset_id=asset_id,
            certification_type=certification_type,
            status=status,
            cursor=cursor,
            limit=limit,
        )
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "asset_certifications.list: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "asset_certifications.list_failed",
                "message": "Failed to list asset certifications.",
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
# GET /api/compliance/asset-certifications/dashboard
# ---------------------------------------------------------------------------


@router.get("/dashboard")
async def get_fleet_certification_dashboard(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Get the fleet certification dashboard for the tenant.

    Returns all assets with their certification statuses, upcoming
    expirations, and overdue inspections sorted by urgency.

    Validates: Requirement 13.7
    """
    svc = _get_asset_cert_service()

    try:
        dashboard = await svc.get_fleet_dashboard(tenant.tenant_id)
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "asset_certifications.dashboard: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "asset_certifications.dashboard_failed",
                "message": "Failed to generate fleet certification dashboard.",
            },
        )

    # Calculate summary counts
    total_valid = sum(1 for item in dashboard if item.status == "valid")
    total_expiring_soon = sum(1 for item in dashboard if item.status == "expiring_soon")
    total_expired = sum(1 for item in dashboard if item.status == "expired")

    return {
        "data": {
            "assets": [item.model_dump(mode="json") for item in dashboard],
            "total_valid": total_valid,
            "total_expiring_soon": total_expiring_soon,
            "total_expired": total_expired,
        },
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/compliance/asset-certifications
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_asset_certification(
    request: Request,
    body: AssetCertificationCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Create a new asset certification record.

    The router stamps ``tenant_id`` from the verified JWT context so
    clients cannot seed cross-tenant records. Input validation is
    delegated to the AssetCertification Pydantic model via the service
    layer.

    Validates: Requirement 13.1
    """
    svc = _get_asset_cert_service()

    try:
        cert_doc = await svc.create(
            tenant.tenant_id,
            asset_id=body.asset_id,
            certification_type=body.certification_type,
            certification_date=body.certification_date,
            expiry_date=body.expiry_date,
            inspector_name=body.inspector_name,
            certificate_number=body.certificate_number,
            status=body.status or "valid",
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "asset_certifications.invalid_payload",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "asset_certifications.create: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "asset_certifications.create_failed",
                "message": "Failed to create asset certification.",
            },
        )

    logger.info(
        "asset_certifications.create: tenant=%s cert=%s asset=%s type=%s",
        tenant.tenant_id,
        cert_doc.get("cert_id"),
        cert_doc.get("asset_id"),
        cert_doc.get("certification_type"),
    )

    return {
        "data": cert_doc,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/asset-certifications/{cert_id}
# ---------------------------------------------------------------------------


@router.get("/{cert_id}")
async def get_asset_certification(
    request: Request,
    cert_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Retrieve a single asset certification by ID, scoped to the tenant.

    Returns HTTP 404 if the certification does not exist or does not
    belong to the requesting tenant.

    Validates: Requirement 13.1
    """
    svc = _get_asset_cert_service()

    try:
        cert_doc = await svc.get(tenant.tenant_id, cert_id)
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "asset_certifications.get: unexpected error for tenant=%s cert=%s: %s",
            tenant.tenant_id,
            cert_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "asset_certifications.get_failed",
                "message": "Failed to retrieve asset certification.",
            },
        )

    return {
        "data": cert_doc,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# PUT /api/compliance/asset-certifications/{cert_id}
# ---------------------------------------------------------------------------


@router.put("/{cert_id}")
async def update_asset_certification(
    request: Request,
    cert_id: str,
    body: AssetCertificationUpdateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Update an existing asset certification record.

    Only provided (non-None) fields are applied. The ``asset_id`` and
    ``certification_type`` are immutable after creation.

    Validates: Requirement 13.1
    """
    svc = _get_asset_cert_service()

    kwargs: Dict[str, Any] = {}

    if body.certification_date is not None:
        kwargs["certification_date"] = body.certification_date
    if body.expiry_date is not None:
        kwargs["expiry_date"] = body.expiry_date
    if body.inspector_name is not None:
        kwargs["inspector_name"] = body.inspector_name
    if body.certificate_number is not None:
        kwargs["certificate_number"] = body.certificate_number
    if body.status is not None:
        kwargs["status"] = body.status

    try:
        updated_doc = await svc.update(
            tenant.tenant_id,
            cert_id,
            **kwargs,
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "asset_certifications.invalid_payload",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "asset_certifications.update: unexpected error for tenant=%s cert=%s: %s",
            tenant.tenant_id,
            cert_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "asset_certifications.update_failed",
                "message": "Failed to update asset certification.",
            },
        )

    logger.info(
        "asset_certifications.update: tenant=%s cert=%s",
        tenant.tenant_id,
        cert_id,
    )

    return {
        "data": updated_doc,
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_asset_certification_api",
    "router",
    "AssetCertificationCreateRequest",
    "AssetCertificationUpdateRequest",
]
