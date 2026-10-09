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
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Sequence

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from compliance.api._authz import compliance_ops_dependency
from compliance.services.driver_qualification_service import (
    DriverQualificationService,
)
from errors.exceptions import AppException
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.csv_export import (
    EXPORT_PAGE_SIZE,
    EXPORT_RATE_LIMIT,
    MAX_EXPORT_ROWS,
    ExportColumn,
    export_guard,
    export_rate_key,
    stream_csv_export,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level service reference, wired via configure_driver_api()
# ---------------------------------------------------------------------------

_driver_service: Optional[DriverQualificationService] = None

# DOT / IRS records, gated to the operations roles. Attached to the router
# rather than to each handler so a route added later inherits it: every module
# in this package previously had no role check at all.
router = APIRouter(
    prefix="/api/compliance/drivers", tags=["Compliance"],
    dependencies=[Depends(compliance_ops_dependency)],
)


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
        raise AppException(
            error_code="drivers.list_failed",
            message="Failed to list drivers.",
            status_code=500,
        )

    return {
        "data": result["items"],
        "next_cursor": result.get("next_cursor"),
        "limit": result.get("limit", limit),
        "count": len(result["items"]),
        "request_id": _get_request_id(request),
    }


# ---------------------------------------------------------------------------
# GET /api/compliance/drivers/export (OI-57, owner decision 2026-10-07)
# Declared above GET /{driver_id}, otherwise "export" binds as a driver id.
# ---------------------------------------------------------------------------

#: DQ expiry columns. Excludes the CDL number, phone and email (data-export
#: FR4); ``full_name`` is operational, like customer names (D6).
_DQ_EXPORT_COLUMNS = [
    ExportColumn(name, (lambda n: lambda r: r.get(n))(name))
    for name in (
        "driver_id", "full_name", "driver_status",
        "cdl_expiry_date", "medical_card_expiry_date",
        "hazmat_endorsement_expiry_date", "tanker_endorsement_expiry_date",
        "nearest_expiry_date", "nearest_expiry_type",
        "days_until_nearest_expiry", "overall_status",
    )
]


def _dq_export_row(
    svc: DriverQualificationService, driver: Dict[str, Any], today: date
) -> Dict[str, Any]:
    summary = svc.summarize_qualifications(driver, today, driver.get("driver_id"))
    nearest = min(
        summary.qualifications, key=lambda q: q.expiry_date, default=None
    )
    return {
        "driver_id": driver.get("driver_id"),
        "full_name": driver.get("full_name"),
        "driver_status": summary.driver_status,
        "cdl_expiry_date": driver.get("cdl_expiry_date"),
        "medical_card_expiry_date": driver.get("medical_card_expiry_date"),
        "hazmat_endorsement_expiry_date": driver.get("hazmat_endorsement_expiry_date"),
        "tanker_endorsement_expiry_date": driver.get("tanker_endorsement_expiry_date"),
        "nearest_expiry_date": nearest.expiry_date if nearest else None,
        "nearest_expiry_type": nearest.qualification_type if nearest else None,
        "days_until_nearest_expiry": nearest.days_until_expiry if nearest else None,
        "overall_status": summary.overall_status,
    }


class _DriverQualificationSource:
    """ExportSource over ``DriverQualificationService.list`` (one pass).

    The service has no count, so ``count()`` pages the tenant's drivers once
    (stopping as soon as the cap is passed, so ``stream_csv_export`` answers
    413) and ``pages()`` replays them.
    """

    def __init__(
        self,
        svc: DriverQualificationService,
        tenant_id: str,
        status: Optional[str],
        max_rows: int,
    ) -> None:
        self._svc = svc
        self._tenant_id = tenant_id
        self._status = status
        self._max_rows = max_rows
        self._rows: List[Dict[str, Any]] = []

    async def count(self) -> int:
        today = date.today()
        drivers: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            page = await self._svc.list(
                self._tenant_id, cursor=cursor, limit=EXPORT_PAGE_SIZE,
                status=self._status,
            )
            for driver in page.get("items") or []:
                # Re-check the tenant on the row, in case a backend ignores
                # the filter.
                if driver.get("tenant_id") != self._tenant_id:
                    logger.warning(
                        "drivers.export: dropping row with mismatched tenant_id "
                        "%s (expected %s)",
                        driver.get("tenant_id"), self._tenant_id,
                    )
                    continue
                drivers.append(driver)
            if len(drivers) > self._max_rows:
                return len(drivers)
            cursor = page.get("next_cursor")
            if not cursor:
                break
        self._rows = [_dq_export_row(self._svc, d, today) for d in drivers]
        return len(self._rows)

    async def pages(self) -> AsyncIterator[Sequence[Mapping[str, Any]]]:
        for start in range(0, len(self._rows), EXPORT_PAGE_SIZE):
            yield self._rows[start:start + EXPORT_PAGE_SIZE]


@router.get("/export", response_model=None)
@limiter.limit(EXPORT_RATE_LIMIT, key_func=export_rate_key)
async def export_driver_qualifications(
    request: Request,
    tenant: TenantContext = Depends(export_guard("admin", base=get_tenant_context)),
    status: Optional[str] = Query(
        default=None,
        description="Filter by driver status: active, suspended, or expired.",
    ),
):
    """CSV of driver qualification expiry dates and status (admin).

    ``overall_status`` (``valid`` / ``expiring`` / ``expired``) uses the same
    60-day rule as ``DriverQualificationService.get_qualification_summary``
    (the Drivers → Utilization status chip).
    """
    source = _DriverQualificationSource(
        _get_driver_service(), tenant.tenant_id, status, MAX_EXPORT_ROWS
    )
    return await stream_csv_export(
        request=request, tenant=tenant, export_type="driver_qualifications",
        columns=_DQ_EXPORT_COLUMNS, source=source, filters={"status": status},
        max_rows=MAX_EXPORT_ROWS,
    )


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
        raise AppException(
            error_code="drivers.dashboard_failed",
            message="Failed to generate DQF dashboard.",
            status_code=500,
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
        raise AppException(
            error_code="drivers.invalid_payload",
            message=str(exc),
            status_code=422,
        )
    except Exception as exc:
        logger.error(
            "drivers.create: unexpected error for tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise AppException(
            error_code="drivers.create_failed",
            message="Failed to create driver.",
            status_code=500,
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
        raise AppException(
            error_code="drivers.get_failed",
            message="Failed to retrieve driver.",
            status_code=500,
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
        raise AppException(
            error_code="drivers.invalid_payload",
            message=str(exc),
            status_code=422,
        )
    except Exception as exc:
        logger.error(
            "drivers.update: unexpected error for tenant=%s driver=%s: %s",
            tenant.tenant_id,
            driver_id,
            exc,
        )
        raise AppException(
            error_code="drivers.update_failed",
            message="Failed to update driver.",
            status_code=500,
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
