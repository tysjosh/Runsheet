"""
Job-thread messaging endpoints for the Driver Communication module.

Provides REST endpoints for drivers and dispatchers to exchange messages
within the context of a specific job. Messages are stored in the
``job_messages`` Elasticsearch index and broadcast through both the
Driver WebSocket and the scheduling WebSocket.

Validates: Requirements 6.1, 6.2, 6.3, 6.4
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.models import MessageRequest
from driver.services.driver_es_mappings import JOB_MESSAGES_INDEX
from errors.exceptions import forbidden, resource_not_found
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Module-level service references, wired via configure_message_endpoints()
_es_service: Optional[ElasticsearchService] = None
_job_service = None
_scheduling_ws_manager = None
_driver_ws_manager = None

router = APIRouter(prefix="/api/driver", tags=["driver-messaging"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_message_endpoints(
    *,
    es_service: ElasticsearchService,
    job_service=None,
    scheduling_ws_manager=None,
    driver_ws_manager=None,
) -> None:
    """
    Wire service dependencies into the message endpoints module.

    Called once during application startup (from bootstrap) so that the
    router handlers can access the shared services.
    """
    global _es_service, _job_service, _scheduling_ws_manager, _driver_ws_manager
    _es_service = es_service
    _job_service = job_service
    _scheduling_ws_manager = scheduling_ws_manager
    _driver_ws_manager = driver_ws_manager


def _get_es_service() -> ElasticsearchService:
    """Return the configured ElasticsearchService or raise."""
    if _es_service is None:
        raise RuntimeError(
            "Message endpoints not configured. "
            "Call configure_message_endpoints() during startup."
        )
    return _es_service


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Access control helpers
# ---------------------------------------------------------------------------


async def _validate_sender_access(
    job_id: str,
    sender_id: str,
    sender_role: str,
    tenant_id: str,
) -> dict:
    """Validate that the sender has access to the job thread.

    A sender is authorised if:
    - sender_role is ``driver`` and the sender is the assigned driver, OR
    - sender_role is ``dispatcher`` (dispatchers have access to all tenant jobs)

    Returns the job document on success.

    Raises:
        AppException: 403 if the sender does not have access.
        AppException: 404 if the job is not found.

    Validates: Requirements 6.4, 11.2
    """
    if _job_service is None:
        raise RuntimeError(
            "Message endpoints not configured — job_service is required."
        )

    # Fetch the job document (raises 404 if not found)
    job_doc = await _job_service._get_job_doc(job_id, tenant_id)

    if sender_role == "dispatcher":
        # Dispatchers have access to all jobs within their tenant
        return job_doc

    if sender_role == "driver":
        assigned_driver = job_doc.get("asset_assigned")
        if assigned_driver != sender_id:
            raise forbidden(
                message="Assignment revoked",
                details={
                    "job_id": job_id,
                    "sender_id": sender_id,
                    "assigned_driver": assigned_driver,
                },
            )
        return job_doc

    # Unknown sender_role — reject
    raise forbidden(
        message=f"Invalid sender_role '{sender_role}' for job messaging",
        details={
            "job_id": job_id,
            "sender_role": sender_role,
            "allowed_roles": ["driver", "dispatcher"],
        },
    )


async def _broadcast_message_event(
    event_type: str, event_data: dict, driver_id: Optional[str] = None
) -> None:
    """Broadcast a message event through both WS managers.

    Validates: Requirement 6.3
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


@router.post("/jobs/{job_id}/messages")
@limiter.limit(_driver_rate)
async def send_message(
    job_id: str,
    body: MessageRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Post a message to a job thread.

    Stores the message in the ``job_messages`` ES index and broadcasts
    it through the Driver WebSocket and the scheduling WebSocket.

    The sender must be the assigned driver for the job or a dispatcher
    for the tenant.

    Validates: Requirements 6.1, 6.3, 6.4, 14.1, 14.3, 14.4
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    es = _get_es_service()

    # Validate sender access (raises 403/404 on failure)
    job_doc = await _validate_sender_access(
        job_id=job_id,
        sender_id=body.sender_id,
        sender_role=body.sender_role,
        tenant_id=tenant.tenant_id,
    )

    now = datetime.now(timezone.utc).isoformat()
    message_id = str(uuid.uuid4())

    message_doc = {
        "message_id": message_id,
        "job_id": job_id,
        "sender_id": body.sender_id,
        "sender_role": body.sender_role,
        "body": body.body,
        "timestamp": now,
        "tenant_id": tenant.tenant_id,
    }

    # Store message in ES
    await es.index_document(JOB_MESSAGES_INDEX, message_id, message_doc)

    # Broadcast to WS channels
    driver_id = job_doc.get("asset_assigned")
    await _broadcast_message_event("job_message", message_doc, driver_id=driver_id)

    result = {
        "data": message_doc,
        "request_id": _get_request_id(request),
    }

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result


@router.get("/jobs/{job_id}/messages")
@limiter.limit(_driver_rate)
async def list_messages(
    job_id: str,
    request: Request,
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    size: int = Query(50, ge=1, le=200, description="Page size"),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Return messages for a job thread sorted by timestamp ascending.

    Supports pagination via ``page`` and ``size`` query parameters.

    Validates: Requirements 6.2
    """
    es = _get_es_service()

    offset = (page - 1) * size

    query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"job_id": job_id}},
                    {"term": {"tenant_id": tenant.tenant_id}},
                ]
            }
        },
        "sort": [{"timestamp": {"order": "asc"}}],
        "from": offset,
        "size": size,
    }

    response = await es.search_documents(JOB_MESSAGES_INDEX, query, size=size)
    hits = response.get("hits", {})
    total = hits.get("total", {}).get("value", 0)
    messages = [hit["_source"] for hit in hits.get("hits", [])]

    return {
        "data": messages,
        "pagination": {
            "page": page,
            "size": size,
            "total": total,
            "total_pages": max(1, -(-total // size)),  # ceil division
        },
        "request_id": _get_request_id(request),
    }
