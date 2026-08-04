"""
Inspection intake router — ``POST /api/driver/inspections``.

One handler over :class:`~driver.services.inspection_service.InspectionService`.
Every rule about what an inspection report *is* — the component vocabulary, the
two severities, the odometer bound, the calendar-day derivation, the
tenant-prefix check on every photo ``file_ref``, the 15-month retention stamp,
and the unconditional out-of-service effect — lives in the service. This module
resolves the caller's identity, applies the driver rate limit, handles
``X-Idempotency-Key``, and stamps the correlation id.

**The submission has no ``driver_id`` field.** The subject of a report is the
verified session's driver, taken from
:func:`~auth.authorization.require_driver_identity` and from nowhere else, and
the document id is ``{tenant_id}:{inspection_id}`` — so this surface cannot file
a report against another driver or another tenant.

``X-Idempotency-Key`` behaves exactly as it does on every other driver write: a
key already seen for this tenant replays the stored response body and status
with ``X-Idempotent-Replayed: true``, an unseen key is processed as a first-time
submission and its response stored, and no header at all means no deduplication
(R8.10, R11.1-R11.5). That is what makes the app's offline queue safe to drain
twice.

**No feature flag is consulted in this module.** A ``pre_trip`` report is
accepted in every tenant. The one conditional value is
``inspection_type: post_trip``, and the flag read that opens it lives in
``InspectionService._post_trip_accepted`` — one of the exactly two places R8.11
permits ``driver.pretrip_inspection_required`` to be read — so this router
carries no branch on it (R8.8, R8.12, R8.13).

Every rejection is an ``AppException`` from ``errors/exceptions.py`` — this
module raises **zero** raw ``HTTPException`` (R15.10).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Data Models,
"New index: ``vehicle_inspections``".

Validates: Requirements 8.3, 8.4, 8.8, 8.10, 8.11, 8.12, 8.13, 15.8
- 8.3, 8.4: the accepted field set and the per-defect field set
- 8.8: both ``inspection_type`` values reach the service on one body shape
- 8.10: a seen key returns the stored response, an unseen key is a first-time
  submission
- 8.11, 8.12, 8.13: this router reads no flag, so a tenant that has not enabled
  the inspection workflow still records pre-trip reports
- 15.8: photo refs are tenant-prefix validated before anything persists
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.authorization import require_driver_identity
from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.services.inspection_service import InspectionService, PRE_TRIP
from errors.exceptions import internal_error
from middleware.rate_limiter import driver_rate_key, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level collaborators, wired via configure_inspection_endpoints().
_es_service: Optional[Any] = None
_file_storage_service: Optional[Any] = None
_feature_flag_service: Optional[Any] = None
_scheduling_ws_manager: Optional[Any] = None
_inspection_service: Optional[InspectionService] = None

router = APIRouter(prefix="/api/driver", tags=["driver-inspections"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_inspection_endpoints(
    *,
    es_service: Any = None,
    file_storage_service: Any = None,
    feature_flag_service: Any = None,
    scheduling_ws_manager: Any = None,
) -> None:
    """Wire the Inspection_Service collaborators and build the service.

    Called once during startup from ``bootstrap/driver.py``. Like every other
    ``configure_*`` on this surface it assigns each module global
    unconditionally, so an argument a caller omits is reset to ``None`` — every
    call site must pass its full argument set.

    ``es_service`` is the store; without it there is no service and the handler
    fails closed rather than accepting a report nothing persists.
    ``file_storage_service`` supplies ``validate_ref``, the tenant-prefix check
    every submitted photo ``file_ref`` goes through (R15.8) — the same validator
    the POD and exception surfaces use. ``feature_flag_service`` reaches the
    service, which reads it in exactly one place to decide whether a
    ``post_trip`` submission is accepted (R8.8, R8.11); absent, that path stays
    closed and every other rule is unaffected.
    ``scheduling_ws_manager`` is the dispatcher channel the out-of-service
    escalation is broadcast on (R8.5) — the same manager the exception surface
    escalates over, not a second one.
    """
    global _es_service, _file_storage_service, _feature_flag_service
    global _scheduling_ws_manager, _inspection_service

    _es_service = es_service
    _file_storage_service = file_storage_service
    _feature_flag_service = feature_flag_service
    _scheduling_ws_manager = scheduling_ws_manager
    _inspection_service = (
        InspectionService(
            es_service=es_service,
            file_storage_service=file_storage_service,
            feature_flag_service=feature_flag_service,
            scheduling_ws_manager=scheduling_ws_manager,
        )
        if es_service is not None
        else None
    )


def configured_inspection_service() -> Optional[InspectionService]:
    """Return the service :func:`configure_inspection_endpoints` built, or ``None``.

    The driver transition gate stack reads inspection-derived state through the
    same instance rather than constructing a second one over the same index.
    """
    return _inspection_service


def _get_inspection_service() -> InspectionService:
    """Return the configured :class:`InspectionService`, failing closed."""
    if _inspection_service is None:
        logger.error(
            "Inspection endpoints not configured. "
            "Call configure_inspection_endpoints() during startup."
        )
        raise internal_error(
            message="Inspection reporting is temporarily unavailable",
            details={"reason": "inspection_endpoints_not_configured"},
        )
    return _inspection_service


def _get_request_id(request: Request) -> str:
    """Extract ``request_id`` from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class InspectionDefect(BaseModel):
    """One defect entry on an inspection report.

    ``component`` and ``severity`` are checked against the service's
    vocabularies rather than declared as enums here, so the two paths — a
    request body and a direct service call — reject exactly the same values
    with exactly the same error.

    Validates: Requirements 8.4
    """

    model_config = ConfigDict(extra="forbid")

    component: str = Field(..., min_length=1, max_length=64)
    severity: str = Field(..., min_length=1, max_length=32)
    note: Optional[str] = Field(default=None, max_length=2000)
    photo_refs: Optional[List[str]] = Field(default=None)


class InspectionSubmissionRequest(BaseModel):
    """Body for ``POST /api/driver/inspections``.

    There is no ``driver_id`` field: the subject is the verified session's
    driver. ``inspection_timestamp`` is the **client's** stamp for when the
    driver walked the vehicle, which may predate the server's receipt by the
    length of an offline queue drain, and ``inspection_local_date`` is the
    calendar day in the tenant's timezone, precomputed on the client.

    ``inspection_type`` is ``pre_trip`` or ``post_trip`` and the field set is the
    same either way (R8.8) — one body shape, one handler, one service call. The
    value is checked against the service's vocabulary rather than declared as an
    enum here, so a request body and a direct service call are refused
    identically.

    Validates: Requirements 8.3, 8.4, 8.8
    """

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(..., min_length=1, max_length=128)
    odometer_miles: float = Field(...)
    inspection_timestamp: Optional[str] = Field(default=None, max_length=64)
    inspection_local_date: Optional[str] = Field(default=None, max_length=32)
    inspection_type: str = Field(default=PRE_TRIP, max_length=32)
    defects: Optional[List[InspectionDefect]] = Field(default=None)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/inspections")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def submit_inspection(
    body: InspectionSubmissionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> Any:
    """Record the calling driver's own inspection report.

    Args:
        body: The inspected asset, the odometer reading in miles, the client's
            timestamp and calendar day, and the defect list.
        request: The inbound request, for the correlation id.
        tenant: The verified Auth_Context.
        idempotency: The ``X-Idempotency-Key`` lookup result.

    Returns:
        ``{"data": <persisted report>, "request_id": ...}`` with HTTP 200, or
        the stored response for a key already seen in this tenant.

    Raises:
        AppException: 403 ``INSUFFICIENT_ROLE`` / ``DRIVER_IDENTITY_MISSING``
            for a caller that is not a driver; 400 ``INVALID_REQUEST`` for an
            unknown component or severity, an unusable odometer reading, a
            malformed timestamp, or a ``post_trip`` submission in a tenant that
            has not enabled the inspection workflow; 403 ``FORBIDDEN`` for a
            photo ``file_ref`` outside the caller's tenant prefix.

    Validates: Requirements 8.3, 8.4, 8.8, 8.10, 8.13, 15.8
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    driver_id = require_driver_identity(tenant)

    report = await _get_inspection_service().submit(
        tenant.tenant_id,
        driver_id,
        asset_id=body.asset_id,
        odometer_miles=body.odometer_miles,
        inspection_timestamp=body.inspection_timestamp,
        inspection_local_date=body.inspection_local_date,
        defects=body.defects,
        inspection_type=body.inspection_type,
    )

    payload: Dict[str, Any] = {
        "data": report,
        "request_id": _get_request_id(request),
    }
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, payload
        )
    return payload


__all__ = [
    "router",
    "configure_inspection_endpoints",
    "configured_inspection_service",
    "InspectionSubmissionRequest",
    "InspectionDefect",
]
