"""
Driver acknowledgment endpoints for the Scheduling module.

Provides REST endpoints for drivers to acknowledge, accept, or reject
job assignments. These are job lifecycle operations and live under the
scheduling prefix alongside other job endpoints.

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request

from auth.authorization import require_driver_identity
from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.models import AckRequest, RejectRequest
from errors.exceptions import validation_error, forbidden
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from scheduling.models import JobStatus
from scheduling.services.job_service import JobService

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_scheduling_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level service references, wired via configure_driver_endpoints()
_job_service: Optional[JobService] = None
_scheduling_ws_manager = None
_driver_ws_manager = None

router = APIRouter(prefix="/api/scheduling", tags=["driver"])


# ---------------------------------------------------------------------------
# Allowed states per driver action
# ---------------------------------------------------------------------------

_ALLOWED_STATES = {
    "ack": {JobStatus.ASSIGNED},
    "accept": {JobStatus.SCHEDULED, JobStatus.ASSIGNED},
    "reject": {JobStatus.ASSIGNED},
}

# Mapping from action name to the set of actions allowed in each state
_STATE_ALLOWED_ACTIONS: dict[JobStatus, list[str]] = {
    JobStatus.SCHEDULED: ["accept"],
    JobStatus.ASSIGNED: ["ack", "accept", "reject"],
    JobStatus.IN_PROGRESS: [],
    JobStatus.COMPLETED: [],
    JobStatus.CANCELLED: [],
    JobStatus.FAILED: [],
}


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_driver_endpoints(
    *,
    job_service: JobService,
    scheduling_ws_manager=None,
    driver_ws_manager=None,
) -> None:
    """
    Wire service dependencies into the driver endpoints module.

    Called once during application startup (from bootstrap/scheduling.py)
    so that the router handlers can access the shared services.
    """
    global _job_service, _scheduling_ws_manager, _driver_ws_manager
    _job_service = job_service
    _scheduling_ws_manager = scheduling_ws_manager
    _driver_ws_manager = driver_ws_manager


def _get_job_service() -> JobService:
    """Return the configured JobService or raise."""
    if _job_service is None:
        raise RuntimeError(
            "Driver endpoints not configured. "
            "Call configure_driver_endpoints() during startup."
        )
    return _job_service


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def _authorize_driver(job_doc: dict, tenant: TenantContext, job_id: str) -> None:
    """Reject a driver who is not this job's driver.

    The caller has already passed :func:`require_driver_identity`, so
    ``tenant.driver_id`` is set. The job's named driver is
    ``assigned_driver_id`` (stamped on accept) or ``driver_id`` (the
    dispatcher-set linkage). When one is named it must be the caller.

    With no named driver, ``asset_assigned`` decides: empty means an
    unclaimed job anyone in the tenant's driver pool may accept; the
    caller's ``user_id`` is a pre-migration document that stored the
    SuperTokens id there. Anything else is a truck id that is not the
    caller's claim, so it is refused.

    After a reassignment the previous driver therefore gets 403
    "Assignment revoked" on every action.

    Validates: Requirement 11.2, 15.14 (B5)

    Args:
        job_doc: The raw job document.
        tenant: The verified driver context.
        job_id: The job identifier (the only value echoed in the error).

    Raises:
        AppException: 403 "Assignment revoked" with details ``{job_id}``
            only, never either driver's identity (R15.14).
    """
    named_driver = job_doc.get("assigned_driver_id") or job_doc.get("driver_id")
    if named_driver:
        allowed = named_driver == tenant.driver_id
    else:
        asset_assigned = job_doc.get("asset_assigned")
        allowed = not asset_assigned or asset_assigned == tenant.user_id
    if not allowed:
        raise forbidden(
            message="Assignment revoked",
            details={"job_id": job_id},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_job_state(
    job_doc: dict, action: str, job_id: str
) -> None:
    """Validate that the job is in an allowed state for the given action.

    Raises AppException (400) with current status and allowed actions
    if the state is invalid.

    Validates: Requirement 5.4
    """
    current_status = JobStatus(job_doc["status"])
    allowed = _ALLOWED_STATES[action]

    if current_status not in allowed:
        allowed_actions = _STATE_ALLOWED_ACTIONS.get(current_status, [])
        raise validation_error(
            f"Cannot {action} job '{job_id}': "
            f"current status is '{current_status.value}'",
            details={
                "job_id": job_id,
                "current_status": current_status.value,
                "allowed_actions": allowed_actions,
            },
        )


async def _broadcast_driver_event(
    event_type: str, event_data: dict
) -> None:
    """Broadcast a driver action event through both WS managers.

    Validates: Requirement 5.5
    """
    # Broadcast through scheduling WS
    if _scheduling_ws_manager is not None:
        try:
            await _scheduling_ws_manager.broadcast(
                event_type, event_data, tenant_id=event_data.get("tenant_id", "")
            )
        except Exception as exc:
            logger.warning(
                "Scheduling WS broadcast failed for %s on job %s: %s",
                event_type,
                event_data.get("job_id"),
                exc,
            )

    # Broadcast through driver WS
    if _driver_ws_manager is not None:
        try:
            driver_id = event_data.get("actor_id") or event_data.get(
                "asset_assigned"
            )
            if driver_id and hasattr(_driver_ws_manager, "send_to_driver"):
                await _driver_ws_manager.send_to_driver(
                    driver_id,
                    {"type": event_type, "data": event_data},
                )
            else:
                # No driver to target. A fan-out to every driver socket would
                # cross tenants, so driver delivery is skipped (D3).
                logger.debug(
                    "No driver id; driver socket delivery skipped for %s on job %s",
                    event_type,
                    event_data.get("job_id"),
                )
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


@router.post("/jobs/{job_id}/ack")
@limiter.limit(_scheduling_rate)
async def ack_job(
    job_id: str,
    body: AckRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Record driver acknowledgment of a job assignment.

    The caller must hold the ``driver`` role and a ``driver_id``, and be
    the job's driver (:func:`_authorize_driver`). The job must be in
    ``assigned`` status. Appends an ``ack`` event to the job event
    timeline with timestamp, actor_id, and device_id.

    Validates: Requirements 5.1, 5.4, 5.5, 11.2, 14.1, 14.3, 14.4 (B5)
    """
    # Driver gate runs before replay so a cached body is never served to a
    # non-driver (B5).
    require_driver_identity(tenant)
    if idempotency.is_replay:
        return idempotency.replay_response()

    svc = _get_job_service()
    job_doc = await svc._get_job_doc(job_id, tenant.tenant_id)

    # Access control: only the job's driver may act (Req 11.2, B5)
    _authorize_driver(job_doc, tenant, job_id)

    # Validate state
    _validate_job_state(job_doc, "ack", job_id)

    now = datetime.now(timezone.utc).isoformat()

    # Append ack event to job timeline
    event_payload = {
        "action": "ack",
        "actor_id": tenant.user_id,
        "device_id": body.device_id,
        "timestamp": now,
    }
    await svc._append_event(
        job_id=job_id,
        event_type="ack",
        tenant_id=tenant.tenant_id,
        actor_id=tenant.user_id,
        payload=event_payload,
    )

    # Broadcast event
    broadcast_data = {
        "job_id": job_id,
        "action": "ack",
        "actor_id": tenant.user_id,
        "device_id": body.device_id,
        "timestamp": now,
        "tenant_id": tenant.tenant_id,
    }
    await _broadcast_driver_event("driver_ack", broadcast_data)

    result = {
        "data": {
            "job_id": job_id,
            "action": "ack",
            "actor_id": tenant.user_id,
            "device_id": body.device_id,
            "timestamp": now,
        },
        "request_id": _get_request_id(request),
    }

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result


@router.post("/jobs/{job_id}/accept")
@limiter.limit(_scheduling_rate)
async def accept_job(
    job_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Driver accepts a job assignment.

    The caller must hold the ``driver`` role and a ``driver_id``, and be
    the job's driver or the job must be unclaimed (:func:`_authorize_driver`).
    If the job is ``scheduled``, transitions to ``assigned``.
    If the job is already ``assigned``, confirms the assignment.
    Appends an ``accept`` event to the job event timeline.

    Records the canonical ``drivers_current`` identifier in
    ``assigned_driver_id``. ``asset_assigned`` is never written: it holds
    the dispatcher's truck, which accepting must not overwrite (B5).
    Historical documents are not backfilled — an absent
    ``assigned_driver_id`` means a pre-migration document
    (Requirements 1.13, 1.14).

    Validates: Requirements 5.2, 5.4, 5.5, 11.2, 14.1, 14.3, 14.4, 1.13, 1.14 (B5)
    """
    # Driver gate runs before replay so a cached body is never served to a
    # non-driver (B5).
    driver_id = require_driver_identity(tenant)
    if idempotency.is_replay:
        return idempotency.replay_response()

    svc = _get_job_service()
    job_doc = await svc._get_job_doc(job_id, tenant.tenant_id)

    # Access control: only the job's driver, or anyone on an unclaimed job
    # (Req 11.2, B5)
    _authorize_driver(job_doc, tenant, job_id)

    # Validate state
    _validate_job_state(job_doc, "accept", job_id)

    now = datetime.now(timezone.utc).isoformat()
    current_status = JobStatus(job_doc["status"])

    update_fields: dict = {}

    # If scheduled, transition to assigned
    if current_status == JobStatus.SCHEDULED:
        update_fields = {
            "status": JobStatus.ASSIGNED.value,
            "updated_at": now,
        }

    # Stamp the canonical driver identifier (Req 1.13). Written only when the
    # stored value is not already that driver, so a confirmation of an
    # already-linked assignment stays a read.
    if (job_doc.get("assigned_driver_id") or "").strip() != driver_id:
        update_fields["assigned_driver_id"] = driver_id
        update_fields.setdefault("updated_at", now)

    if update_fields:
        await svc._es.update_document(
            "jobs_current", job_id, update_fields
        )
        job_doc.update(update_fields)

    # Append accept event to job timeline
    event_payload = {
        "action": "accept",
        "actor_id": tenant.user_id,
        "previous_status": current_status.value,
        "new_status": job_doc["status"],
        "timestamp": now,
    }
    await svc._append_event(
        job_id=job_id,
        event_type="accept",
        tenant_id=tenant.tenant_id,
        actor_id=tenant.user_id,
        payload=event_payload,
    )

    # Broadcast event
    broadcast_data = {
        "job_id": job_id,
        "action": "accept",
        "actor_id": tenant.user_id,
        "previous_status": current_status.value,
        "new_status": job_doc["status"],
        "timestamp": now,
        "tenant_id": tenant.tenant_id,
    }
    await _broadcast_driver_event("driver_accept", broadcast_data)

    result = {
        "data": {
            "job_id": job_id,
            "action": "accept",
            "actor_id": tenant.user_id,
            "previous_status": current_status.value,
            "new_status": job_doc["status"],
            "timestamp": now,
        },
        "request_id": _get_request_id(request),
    }

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result


@router.post("/jobs/{job_id}/reject")
@limiter.limit(_scheduling_rate)
async def reject_job(
    job_id: str,
    body: RejectRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Driver rejects a job assignment.

    The caller must hold the ``driver`` role and a ``driver_id``, and be
    the job's driver (:func:`_authorize_driver`). Requires a ``reason`` in
    the request body. If the job is ``assigned``, reverts to ``scheduled``.
    Appends a ``reject`` event to the job event timeline.

    Validates: Requirements 5.3, 5.4, 5.5, 11.2, 14.1, 14.3, 14.4 (B5)
    """
    # Driver gate runs before replay so a cached body is never served to a
    # non-driver (B5).
    require_driver_identity(tenant)
    if idempotency.is_replay:
        return idempotency.replay_response()

    svc = _get_job_service()
    job_doc = await svc._get_job_doc(job_id, tenant.tenant_id)

    # Access control: only the job's driver may act (Req 11.2, B5)
    _authorize_driver(job_doc, tenant, job_id)

    # Validate state
    _validate_job_state(job_doc, "reject", job_id)

    now = datetime.now(timezone.utc).isoformat()
    current_status = JobStatus(job_doc["status"])

    # Revert to scheduled if currently assigned
    if current_status == JobStatus.ASSIGNED:
        update_fields = {
            "status": JobStatus.SCHEDULED.value,
            "updated_at": now,
        }
        await svc._es.update_document(
            "jobs_current", job_id, update_fields
        )
        job_doc.update(update_fields)

    # Append reject event to job timeline
    event_payload = {
        "action": "reject",
        "actor_id": tenant.user_id,
        "reason": body.reason,
        "previous_status": current_status.value,
        "new_status": job_doc["status"],
        "timestamp": now,
    }
    await svc._append_event(
        job_id=job_id,
        event_type="reject",
        tenant_id=tenant.tenant_id,
        actor_id=tenant.user_id,
        payload=event_payload,
    )

    # Broadcast event
    broadcast_data = {
        "job_id": job_id,
        "action": "reject",
        "actor_id": tenant.user_id,
        "reason": body.reason,
        "previous_status": current_status.value,
        "new_status": job_doc["status"],
        "timestamp": now,
        "tenant_id": tenant.tenant_id,
    }
    await _broadcast_driver_event("driver_reject", broadcast_data)

    result = {
        "data": {
            "job_id": job_id,
            "action": "reject",
            "actor_id": tenant.user_id,
            "reason": body.reason,
            "previous_status": current_status.value,
            "new_status": job_doc["status"],
            "timestamp": now,
        },
        "request_id": _get_request_id(request),
    }

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result
