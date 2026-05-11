"""Driver Qualification REST endpoints for the Fuel Compliance Backbone.

Exposes CRUD operations for Driver records and the DQF compliance dashboard
under the ``/api/compliance/drivers`` prefix (design §5, "REST API Endpoints
(New)").

Endpoints:

* ``GET  /api/compliance/drivers`` — list drivers with pagination and
  optional status filter (Req 5.1, 5.9).
* ``POST /api/compliance/drivers`` — create a new driver record (Req 5.1).
* ``GET  /api/compliance/drivers/dashboard`` — DQF compliance dashboard
  (Req 5.9).
* ``GET  /api/compliance/drivers/{driver_id}`` — get a single driver
  (Req 5.1).
* ``PUT  /api/compliance/drivers/{driver_id}`` — update a driver record
  (Req 5.1).

Wiring pattern mirrors ``compliance/api/tax_endpoints.py``:

1. A module-level ``_driver_service`` is populated by
   :func:`configure_driver_api` at application startup (see
   ``bootstrap/compliance.py``).
2. Each handler extracts the tenant from :func:`get_tenant_context` so
   all queries are tenant-scoped (Constraint C3).
3. ``AppException`` errors raised by the service layer are propagated
   to the global exception handler registered in ``main.py``.

Validates: Requirements 5.1, 5.9
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from compliance.services.driver_qualification_service import (
    DriverQualificationService,
)
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_driver_api()
# ---------------------------------------------------------------------------

_driver_service: Optional[DriverQualificationService] = None

router = APIRouter(prefix="/api/compliance/drivers", tags=["Compliance"])


def configure_driver_api(*, driver_service: DriverQualificationService) -> None:
    """Wire the DriverQualificationService into this module.

    Called once during application startup (``bootstrap/compliance.py``)
    so that per-request handlers can delegate to the service without
    taking a hard import dependency on the container.

    Args:
        driver_service: The application-scoped DriverQualificationService
            instance.
    """
    global _driver_service
    _driver_service = driver_service


def _get_driver_service() -> DriverQualificationService:
    """Return the configured DriverQualificationService or raise."""
    if _driver_service is None:
        raise RuntimeError(
            "Driver API not configured. Call configure_driver_api() during startup."
        )
    return _driver_service


def _get_request_id(request: Request) -> str:
    """Extract the RequestIDMiddleware-assigned id, with a safe default."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class DriverCreateRequest(BaseModel):
    """Body for ``POST /api/compliance/drivers`` (Req 5.1)."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(
        ...,
        description="Driver's full legal name as it appears on CDL.",
    )
    cdl_number: str = Field(
        ...,
        description="Commercial Driver's License number.",
    )
    cdl_state: str = Field(
        ...,
        description="2-letter US state code where CDL was issued.",
    )
    cdl_class: str = Field(
        ...,
        description="CDL class: A, B, or C.",
    )
    cdl_expiry_date: date = Field(
        ...,
        description="CDL expiration date.",
    )
    medical_card_expiry_date: date = Field(
        ...,
        description="DOT medical card expiration date.",
    )
    hazmat_endorsement_expiry_date: Optional[date] = Field(
        default=None,
        description="HAZMAT endorsement expiration date (None if not endorsed).",
    )
    tanker_endorsement_expiry_date: Optional[date] = Field(
        default=None,
        description="Tanker endorsement expiration date (None if not endorsed).",
    )
    last_drug_test_date: Optional[date] = Field(
        default=None,
        description="Date of most recent drug/alcohol test.",
    )
    last_mvr_date: Optional[date] = Field(
        default=None,
        description="Date of most recent Motor Vehicle Record review.",
    )
    status: Optional[str] = Field(
        default="active",
        description="Driver status: active, suspended, or expired.",
    )
    external_refs: Optional[Dict[str, str]] = Field(
        default=None,
        description="External system references (e.g. geotab_driver_id).",
    )


class DriverUpdateRequest(BaseModel):
    """Body for ``PUT /api/compliance/drivers/{driver_id}`` (Req 5.1).

    All fields are optional — only provided fields are updated.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: Optional[str] = Field(
        default=None,
        description="Driver's full legal name.",
    )
    cdl_number: Optional[str] = Field(
        default=None,
        description="Commercial Driver's License number.",
    )
    cdl_state: Optional[str] = Field(
        default=None,
        description="2-letter US state code where CDL was issued.",
    )
    cdl_class: Optional[str] = Field(
        default=None,
        description="CDL class: A, B, or C.",
    )
    cdl_expiry_date: Optional[date] = Field(
        default=None,
        description="CDL expiration date.",
    )
    medical_card_expiry_date: Optional[date] = Field(
        default=None,
        description="DOT medical card expiration date.",
    )
    hazmat_endorsement_expiry_date: Optional[date] = Field(
        default=None,
        description="HAZMAT endorsement expiration date.",
    )
    tanker_endorsement_expiry_date: Optional[date] = Field(
        default=None,
        description="Tanker endorsement expiration date.",
    )
    last_drug_test_date: Optional[date] = Field(
        default=None,
        description="Date of most recent drug/alcohol test.",
    )
    last_mvr_date: Optional[date] = Field(
        default=None,
        description="Date of most recent Motor Vehicle Record review.",
    )
    status: Optional[str] = Field(
        default=None,
        description="Driver status: active, suspended, or expired.",
    )
    suspension_reason: Optional[str] = Field(
        default=None,
        description="Reason for suspension.",
    )
    external_refs: Optional[Dict[str, str]] = Field(
        default=None,
        description="External system references.",
    )


# ---------------------------------------------------------------------------
# GET /api/compliance/drivers
# ---------------------------------------------------------------------------


@router.get("")
async def list_drivers(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    status: Optional[str] = Query(
        default=None,
        description="Filter by driver status: active, suspended, or expired.",
    ),
    cursor: Optional[str] = Query(
        default=None,
        description=(
            "Cursor for keyset pagination — the driver_id of the last "
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
    """List drivers for the tenant with pagination and optional status filter.

    Validates: Requirement 5.1
    """
    svc = _get_driver_service()

    try:
        result = await svc.list(
            tenant.tenant_id,
            cursor=cursor,
            limit=limit,
            status=status,
        )
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "drivers.list: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "drivers.list_failed",
                "message": "Failed to list drivers.",
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
# GET /api/compliance/drivers/dashboard
# ---------------------------------------------------------------------------


@router.get("/dashboard")
async def get_driver_dashboard(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Get the DQF compliance dashboard for the tenant.

    Returns aggregate counts of driver statuses, upcoming expirations,
    and overdue drug tests.

    Validates: Requirement 5.9
    """
    svc = _get_driver_service()

    try:
        dashboard = await svc.get_dqf_dashboard(tenant.tenant_id)
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "drivers.dashboard: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "drivers.dashboard_failed",
                "message": "Failed to generate DQF dashboard.",
            },
        )

    return {
        "data": dashboard.model_dump(mode="json"),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# POST /api/compliance/drivers
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_driver(
    request: Request,
    body: DriverCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Create a new driver record.

    The router stamps ``tenant_id`` from the verified JWT context so
    clients cannot seed cross-tenant records. Input validation is
    delegated to the Driver Pydantic model via the service layer.

    Validates: Requirement 5.1
    """
    svc = _get_driver_service()

    try:
        driver_doc = await svc.create(
            tenant.tenant_id,
            full_name=body.full_name,
            cdl_number=body.cdl_number,
            cdl_state=body.cdl_state,
            cdl_class=body.cdl_class,
            cdl_expiry_date=body.cdl_expiry_date,
            medical_card_expiry_date=body.medical_card_expiry_date,
            hazmat_endorsement_expiry_date=body.hazmat_endorsement_expiry_date,
            tanker_endorsement_expiry_date=body.tanker_endorsement_expiry_date,
            last_drug_test_date=body.last_drug_test_date,
            last_mvr_date=body.last_mvr_date,
            status=body.status or "active",
            external_refs=body.external_refs,
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "drivers.invalid_payload",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "drivers.create: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "drivers.create_failed",
                "message": "Failed to create driver.",
            },
        )

    logger.info(
        "drivers.create: tenant=%s driver=%s name=%s",
        tenant.tenant_id,
        driver_doc.get("driver_id"),
        driver_doc.get("full_name"),
    )

    return {
        "data": driver_doc,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/drivers/{driver_id}
# ---------------------------------------------------------------------------


@router.get("/{driver_id}")
async def get_driver(
    request: Request,
    driver_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Retrieve a single driver by ID, scoped to the tenant.

    Returns HTTP 404 if the driver does not exist or does not belong
    to the requesting tenant.

    Validates: Requirement 5.1
    """
    svc = _get_driver_service()

    try:
        driver_doc = await svc.get(tenant.tenant_id, driver_id)
    except AppException:
        raise
    except Exception as exc:
        logger.error(
            "drivers.get: unexpected error for tenant=%s driver=%s: %s",
            tenant.tenant_id,
            driver_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "drivers.get_failed",
                "message": "Failed to retrieve driver.",
            },
        )

    return {
        "data": driver_doc,
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# PUT /api/compliance/drivers/{driver_id}
# ---------------------------------------------------------------------------


@router.put("/{driver_id}")
async def update_driver(
    request: Request,
    driver_id: str,
    body: DriverUpdateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """Update an existing driver record.

    Only provided (non-None) fields are applied. The service layer
    uses the sentinel pattern to distinguish "not provided" from
    "set to None" for optional date fields.

    Validates: Requirement 5.1
    """
    svc = _get_driver_service()

    # Build kwargs, using the sentinel (...) for fields not provided
    # in the request body so the service can distinguish "not sent"
    # from "explicitly set to null".
    kwargs: Dict[str, Any] = {}

    if body.full_name is not None:
        kwargs["full_name"] = body.full_name
    if body.cdl_number is not None:
        kwargs["cdl_number"] = body.cdl_number
    if body.cdl_state is not None:
        kwargs["cdl_state"] = body.cdl_state
    if body.cdl_class is not None:
        kwargs["cdl_class"] = body.cdl_class
    if body.cdl_expiry_date is not None:
        kwargs["cdl_expiry_date"] = body.cdl_expiry_date
    if body.medical_card_expiry_date is not None:
        kwargs["medical_card_expiry_date"] = body.medical_card_expiry_date
    if body.hazmat_endorsement_expiry_date is not None:
        kwargs["hazmat_endorsement_expiry_date"] = body.hazmat_endorsement_expiry_date
    if body.tanker_endorsement_expiry_date is not None:
        kwargs["tanker_endorsement_expiry_date"] = body.tanker_endorsement_expiry_date
    if body.last_drug_test_date is not None:
        kwargs["last_drug_test_date"] = body.last_drug_test_date
    if body.last_mvr_date is not None:
        kwargs["last_mvr_date"] = body.last_mvr_date
    if body.status is not None:
        kwargs["status"] = body.status
    if body.suspension_reason is not None:
        kwargs["suspension_reason"] = body.suspension_reason
    if body.external_refs is not None:
        kwargs["external_refs"] = body.external_refs

    try:
        updated_doc = await svc.update(
            tenant.tenant_id,
            driver_id,
            **kwargs,
        )
    except AppException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "drivers.invalid_payload",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "drivers.update: unexpected error for tenant=%s driver=%s: %s",
            tenant.tenant_id,
            driver_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "drivers.update_failed",
                "message": "Failed to update driver.",
            },
        )

    logger.info(
        "drivers.update: tenant=%s driver=%s",
        tenant.tenant_id,
        driver_id,
    )

    return {
        "data": updated_doc,
        "request_id": _get_request_id(request),
    }


__all__ = [
    "configure_driver_api",
    "router",
    "DriverCreateRequest",
    "DriverUpdateRequest",
]
