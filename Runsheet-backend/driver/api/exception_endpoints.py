"""
Driver exception reporting endpoints for the Driver Communication module.

Provides a REST endpoint for drivers to report field exceptions
(road closures, vehicle breakdowns, customer unavailable, etc.).
Exceptions are stored in the ``driver_exceptions`` Elasticsearch index,
appended to the job event timeline, converted to RiskSignals for agent
consumption, and broadcast through WebSocket for high/critical severity.

Validates: Requirements 7.1, 7.2, 7.3, 7.4
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request

from Agents.overlay.data_contracts import RiskSignal, Severity
from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.models import ExceptionRequest
from driver.services.driver_es_mappings import DRIVER_EXCEPTIONS_INDEX
from errors.exceptions import AppException, forbidden
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level service references, wired via configure_exception_endpoints()
_es_service = None
_job_service = None
_signal_bus = None
_scheduling_ws_manager = None
_driver_ws_manager = None

router = APIRouter(prefix="/api/driver", tags=["driver-exceptions"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_exception_endpoints(
    *,
    es_service,
    job_service=None,
    signal_bus=None,
    scheduling_ws_manager=None,
    driver_ws_manager=None,
) -> None:
    """
    Wire service dependencies into the exception endpoints module.

    Called once during application startup (from bootstrap) so that the
    router handlers can access the shared services.
    """
    global _es_service, _job_service, _signal_bus
    global _scheduling_ws_manager, _driver_ws_manager
    _es_service = es_service
    _job_service = job_service
    _signal_bus = signal_bus
    _scheduling_ws_manager = scheduling_ws_manager
    _driver_ws_manager = driver_ws_manager


def _get_es_service():
    """Return the configured ElasticsearchService or raise."""
    if _es_service is None:
        raise RuntimeError(
            "Exception endpoints not configured. "
            "Call configure_exception_endpoints() during startup."
        )
    return _es_service


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_risk_signal(
    exception_id: str,
    job_id: str,
    body: ExceptionRequest,
    tenant_id: str,
) -> RiskSignal:
    """Convert a driver exception report into a RiskSignal for the SignalBus.

    Maps exception_type to entity_type and severity directly to the
    RiskSignal severity field. The signal is consumed by
    exception_commander and exception_replanning agents.

    Validates: Requirement 7.2
    """
    return RiskSignal(
        source_agent="driver_exception_reporter",
        entity_id=job_id,
        entity_type=body.exception_type.value,
        severity=body.severity,
        confidence=0.9,
        ttl_seconds=3600,
        tenant_id=tenant_id,
        context={
            "exception_id": exception_id,
            "note": body.note,
            "location": body.location.model_dump() if body.location else None,
            "media_refs": body.media_refs or [],
        },
    )


async def _broadcast_escalation_event(
    event_type: str,
    event_data: dict,
    driver_id: Optional[str] = None,
) -> None:
    """Broadcast an escalation event through both WS managers.

    Only called for severity ``high`` or ``critical``.

    Validates: Requirement 7.4
    """
    # Broadcast through scheduling WS (for dispatchers)
    if _scheduling_ws_manager is not None:
        try:
            await _scheduling_ws_manager.broadcast(event_type, event_data)
        except Exception as exc:
            logger.warning(
                "Scheduling WS broadcast failed for %s on job %s: %s",
                event_type,
                event_data.get("job_id"),
                exc,
            )

    # Broadcast through driver WS (for the assigned driver)
    if _driver_ws_manager is not None:
        try:
            if driver_id and hasattr(_driver_ws_manager, "send_to_driver"):
                await _driver_ws_manager.send_to_driver(
                    driver_id,
                    {"type": event_type, "data": event_data},
                )
            elif hasattr(_driver_ws_manager, "broadcast"):
                await _driver_ws_manager.broadcast(event_type, event_data)
        except Exception as exc:
            logger.warning(
                "Driver WS broadcast failed for %s on job %s: %s",
                event_type,
                event_data.get("job_id"),
                exc,
            )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/exceptions")
@limiter.limit(_driver_rate)
async def report_exception(
    job_id: str,
    body: ExceptionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Report a field exception for a job.

    Stores the exception in the ``driver_exceptions`` ES index, appends
    an ``exception_reported`` event to the job timeline, converts the
    exception to a ``RiskSignal`` and publishes it to the ``SignalBus``
    for consumption by exception_commander and exception_replanning
    agents. For severity ``high`` or ``critical``, broadcasts an
    escalation event through WebSocket channels.

    The ``exception_type`` field is validated by Pydantic against the
    ``ExceptionType`` enum (road_closure, vehicle_breakdown,
    customer_unavailable, access_denied, weather, cargo_damage, other).

    Validates: Requirements 7.1, 7.2, 7.3, 7.4, 14.1, 14.3, 14.4
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    es = _get_es_service()

    # Access control: reject requests from non-assigned driver (Req 11.2)
    if _job_service is not None:
        try:
            job_doc = await _job_service._get_job_doc(job_id, tenant.tenant_id)
            assigned_driver = job_doc.get("asset_assigned")
            if assigned_driver and assigned_driver != tenant.user_id:
                raise forbidden(
                    message="Assignment revoked",
                    details={
                        "job_id": job_id,
                        "requesting_driver": tenant.user_id,
                        "assigned_driver": assigned_driver,
                    },
                )
        except AppException:
            raise
        except Exception:
            pass  # If job lookup fails for other reasons, proceed (non-blocking)

    now = datetime.now(timezone.utc).isoformat()
    exception_id = str(uuid.uuid4())

    # Build exception document for ES
    exception_doc = {
        "exception_id": exception_id,
        "job_id": job_id,
        "exception_type": body.exception_type.value,
        "severity": body.severity.value,
        "note": body.note,
        "location": body.location.model_dump() if body.location else None,
        "media_refs": body.media_refs or [],
        "tenant_id": tenant.tenant_id,
        "timestamp": now,
    }

    # Store exception in ES (Req 7.1)
    await es.index_document(DRIVER_EXCEPTIONS_INDEX, exception_id, exception_doc)

    # Append exception_reported event to job timeline (Req 7.1)
    if _job_service is not None:
        try:
            await _job_service._append_event(
                job_id=job_id,
                event_type="exception_reported",
                tenant_id=tenant.tenant_id,
                actor_id=tenant.user_id,
                payload={
                    "exception_id": exception_id,
                    "exception_type": body.exception_type.value,
                    "severity": body.severity.value,
                    "note": body.note,
                    "timestamp": now,
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to append exception_reported event for job %s: %s",
                job_id,
                exc,
            )

    # Convert to RiskSignal and publish to SignalBus (Req 7.2)
    if _signal_bus is not None:
        try:
            risk_signal = _build_risk_signal(
                exception_id=exception_id,
                job_id=job_id,
                body=body,
                tenant_id=tenant.tenant_id,
            )
            await _signal_bus.publish(risk_signal)
        except Exception as exc:
            logger.warning(
                "Failed to publish RiskSignal for exception %s: %s",
                exception_id,
                exc,
            )

    # Broadcast escalation for high/critical severity (Req 7.4)
    if body.severity in (Severity.HIGH, Severity.CRITICAL):
        escalation_data = {
            "job_id": job_id,
            "exception_id": exception_id,
            "exception_type": body.exception_type.value,
            "severity": body.severity.value,
            "note": body.note,
            "timestamp": now,
            "tenant_id": tenant.tenant_id,
        }
        await _broadcast_escalation_event(
            "exception_escalation",
            escalation_data,
            driver_id=tenant.user_id,
        )

    result = {
        "data": exception_doc,
        "request_id": _get_request_id(request),
    }

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result
