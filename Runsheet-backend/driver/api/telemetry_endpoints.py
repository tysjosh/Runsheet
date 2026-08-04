"""
Breadcrumb ingestion router — ``POST /api/driver/telemetry/breadcrumbs``.

One handler over
:class:`~driver.services.telemetry_service.DriverTelemetryService`. Every rule
about what a breadcrumb *is* — the accuracy ceiling, the 24-hour staleness
window, the composite document id that makes a repeated sample a create
conflict rather than a duplicate, and the ``driver_presence.last_location``
refresh — lives in the service. This module resolves the caller's identity,
applies the per-driver rate limit, handles ``X-Idempotency-Key``, and stamps the
correlation id.

**The submission has no ``driver_id`` field.** The subject of a batch is the
verified session's driver, taken from
:func:`~auth.authorization.require_driver_identity` and from nowhere else, and
every document id is prefixed ``{tenant_id}:{driver_id}`` — so this surface
cannot write another driver's track or another tenant's (R10.2).

``X-Idempotency-Key`` behaves as it does on every other driver write: a key
already seen for this tenant replays the stored response with
``X-Idempotent-Replayed: true``, an unseen key is processed as a first-time
submission, and no header means no deduplication (R11.1-R11.5). It is a
convenience rather than the correctness mechanism here — the composite
breadcrumb id already makes a redrained batch idempotent (R10.8) — but it lets
the app's offline queue reuse one drain path for every mutation it carries.

Every rejection is an ``AppException`` from ``errors/exceptions.py``; this
module raises **zero** raw ``HTTPException`` (R15.10).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Data Models,
"New index: ``driver_breadcrumbs``".

Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8
- 10.1: the accepted per-sample field set — accuracy in meters, speed in mph,
  heading in degrees, and a client ISO 8601 stamp
- 10.2: the batch subject is the session's driver; the body carries no driver
  identifier to ignore
- 10.3, 10.8: the track store and its id-collision dedup
- 10.4, 10.5: the presence refresh, and its absence on an all-discarded batch
- 10.6, 10.7: the two filters, with the discarded count in the response
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
from driver.services.telemetry_service import (
    DriverTelemetryService,
    MAX_BATCH_SAMPLES,
)
from errors.exceptions import internal_error
from middleware.rate_limiter import driver_rate_key, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level collaborators, wired via configure_telemetry_endpoints().
_es_service: Optional[Any] = None
_telemetry_service: Optional[DriverTelemetryService] = None

router = APIRouter(prefix="/api/driver", tags=["driver-telemetry"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_telemetry_endpoints(*, es_service: Any = None) -> None:
    """Wire the Driver_Telemetry_Service collaborator and build the service.

    Called once during startup from ``bootstrap/driver.py``. Like every other
    ``configure_*`` on this surface it assigns each module global
    unconditionally, so an argument a caller omits is reset to ``None`` — every
    call site must pass its full argument set.

    ``es_service`` is the only collaborator: it is both the track store and the
    writer of the ``driver_presence`` merge. Without it there is no service and
    the handler fails closed rather than accepting a batch nothing persists.
    """
    global _es_service, _telemetry_service

    _es_service = es_service
    _telemetry_service = (
        DriverTelemetryService(es_service=es_service)
        if es_service is not None
        else None
    )


def configured_telemetry_service() -> Optional[DriverTelemetryService]:
    """Return the service :func:`configure_telemetry_endpoints` built, or ``None``."""
    return _telemetry_service


def _get_telemetry_service() -> DriverTelemetryService:
    """Return the configured service, failing closed."""
    if _telemetry_service is None:
        logger.error(
            "Telemetry endpoints not configured. "
            "Call configure_telemetry_endpoints() during startup."
        )
        raise internal_error(
            message="Location tracking is temporarily unavailable",
            details={"reason": "telemetry_endpoints_not_configured"},
        )
    return _telemetry_service


def _get_request_id(request: Request) -> str:
    """Extract ``request_id`` from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class BreadcrumbSample(BaseModel):
    """One location fix (R10.1).

    ``sample_timestamp`` is the **client's** stamp for when the fix was taken,
    which may predate the server's receipt by the length of an offline queue
    drain. ``speed_mph`` is miles per hour and ``accuracy_meters`` is meters —
    the driver surface is US-units for speed and SI for accuracy, matching what
    the device APIs report. ``speed_mph`` and ``heading_degrees`` accept the
    negative "unknown" sentinel the device APIs use, which the service records
    as no reading rather than as a value.
    """

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    sample_timestamp: str = Field(..., min_length=1, max_length=64)
    accuracy_meters: Optional[float] = Field(default=None)
    speed_mph: Optional[float] = Field(default=None)
    heading_degrees: Optional[float] = Field(default=None)


class BreadcrumbBatchRequest(BaseModel):
    """Body for ``POST /api/driver/telemetry/breadcrumbs``.

    There is no ``driver_id`` field: the subject is the verified session's
    driver (R10.2). The batch is capped at :data:`MAX_BATCH_SAMPLES`, which is
    the drain size R10.12 has the app use.
    """

    model_config = ConfigDict(extra="forbid")

    samples: List[BreadcrumbSample] = Field(
        ..., min_length=1, max_length=MAX_BATCH_SAMPLES
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/telemetry/breadcrumbs")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def submit_breadcrumbs(
    body: BreadcrumbBatchRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> Any:
    """Record the calling driver's own breadcrumb batch.

    Args:
        body: The samples, in any order — the service picks the newest retained
            stamp for the presence refresh rather than trusting position.
        request: The inbound request, for the correlation id.
        tenant: The verified Auth_Context.
        idempotency: The ``X-Idempotency-Key`` lookup result.

    Returns:
        ``{"data": <batch summary>, "request_id": ...}`` with HTTP 200. The
        summary carries the retained, stored, duplicate, and discarded counts,
        so the app can tell an accepted batch from a filtered one (R10.6,
        R10.7).

    Raises:
        AppException: 403 ``INSUFFICIENT_ROLE`` / ``DRIVER_IDENTITY_MISSING``
            for a caller that is not a driver; 400 ``INVALID_REQUEST`` for a
            malformed timestamp, an out-of-range coordinate, or an implausible
            speed; 500 ``INTERNAL_ERROR`` when the track cannot be written.

    Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    driver_id = require_driver_identity(tenant)

    summary = await _get_telemetry_service().ingest_batch(
        tenant.tenant_id,
        driver_id,
        samples=body.samples,
    )

    payload: Dict[str, Any] = {
        "data": summary,
        "request_id": _get_request_id(request),
    }
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, payload
        )
    return payload


__all__ = [
    "router",
    "configure_telemetry_endpoints",
    "configured_telemetry_service",
    "BreadcrumbBatchRequest",
    "BreadcrumbSample",
]
