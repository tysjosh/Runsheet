"""
Job-thread messaging endpoints for the Driver Communication module.

Provides REST endpoints for drivers to exchange messages within the context of
a specific job.

The messaging business rule does **not** live here. Both handlers resolve the
path parameter and the verified :class:`TenantContext` into a
:class:`~driver.services.work_ref.WorkRef` and delegate to
:class:`~driver.services.message_service.ThreadMessageService`, which holds
sender-identity enforcement, persistence, delivery, and pagination exactly once
(R7.17, R7.18). The order-keyed siblings resolve through the same resolver and
call the same service, so the two paths cannot diverge (R7.19).

Two authorization holes close by construction with this delegation:

* ``_validate_sender_access`` compared the **request body's** ``sender_id`` to
  ``job_doc["asset_assigned"]`` and never to the verified context, so a driver
  could post as any other driver simply by naming them in the body, and any
  caller could bypass the assignment check entirely by claiming
  ``sender_role: "dispatcher"``. The helper is gone: the acting identity is now
  derived here from ``TenantContext`` and passed to the service, which rejects a
  differing body value with 403 ``SENDER_IDENTITY_MISMATCH`` and ignores a body
  ``sender_role`` outright (R7.5, R7.6, R7.7).
* ``list_messages`` filtered on ``job_id`` + ``tenant_id`` and performed no
  assignment check, so any authenticated caller in the tenant could read any
  thread. The read now takes a resolved ``WorkRef``, and resolution *is* the
  authorization (R7.8, R7.9).

Collaborators still arrive through the module-level globals
:func:`configure_message_endpoints` sets — that wiring pattern is preserved
rather than replaced (R7.20).

Validates: Requirements 6.1, 6.2, 6.3, 6.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10,
7.12, 7.15, 7.17, 7.18, 7.19, 7.20
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.models import MessageRequest
from driver.services.message_service import (
    ALLOWED_SENDER_ROLES,
    ThreadMessageService,
)
from driver.services.work_ref import WorkRef, WorkRefResolver
from middleware.rate_limiter import driver_rate_key, limiter
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

# The two globals task 5.5 adds: the resolver that turns a path parameter into a
# ``WorkRef`` and the service that holds the whole messaging rule. Both are
# built by ``configure_message_endpoints`` from the same globals above (R7.20).
_work_ref_resolver: Optional[WorkRefResolver] = None
_message_service: Optional[ThreadMessageService] = None

router = APIRouter(prefix="/api/driver", tags=["driver-messaging"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_message_endpoints(
    *,
    es_service: ElasticsearchService,
    job_service=None,
    order_repository=None,
    scheduling_ws_manager=None,
    driver_ws_manager=None,
    push_notifier=None,
) -> None:
    """
    Wire service dependencies into the message endpoints module.

    Called once during application startup (from bootstrap) so that the
    router handlers can access the shared services.

    ``order_repository`` feeds :meth:`WorkRefResolver.resolve_order` on the
    order-keyed siblings and ``push_notifier`` is reserved for the R7.11 push
    fallback. Both are optional and default to ``None``. Because this function
    assigns every module global unconditionally, a caller that omits an argument
    resets it to ``None`` — every call site must pass its full argument set.
    """
    global _es_service, _job_service, _scheduling_ws_manager, _driver_ws_manager
    global _work_ref_resolver, _message_service
    _es_service = es_service
    _job_service = job_service
    _scheduling_ws_manager = scheduling_ws_manager
    _driver_ws_manager = driver_ws_manager

    # The resolver and the service, built from the globals just assigned.
    _work_ref_resolver = WorkRefResolver(
        job_service=job_service,
        order_repository=order_repository,
    )
    _message_service = (
        ThreadMessageService(
            es_service=es_service,
            job_service=job_service,
            order_repository=order_repository,
            driver_ws_manager=driver_ws_manager,
            scheduling_ws_manager=scheduling_ws_manager,
            push_notifier=push_notifier,
        )
        if es_service is not None
        else None
    )


def _get_resolver() -> WorkRefResolver:
    """Return the configured :class:`WorkRefResolver` or raise."""
    if _work_ref_resolver is None:
        raise RuntimeError(
            "Message endpoints not configured. "
            "Call configure_message_endpoints() during startup."
        )
    return _work_ref_resolver


def _get_message_service() -> ThreadMessageService:
    """Return the configured :class:`ThreadMessageService` or raise."""
    if _message_service is None:
        raise RuntimeError(
            "Message endpoints not configured. "
            "Call configure_message_endpoints() during startup."
        )
    return _message_service


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Sender identity — derived, never accepted
# ---------------------------------------------------------------------------


def _derive_sender_role(tenant: TenantContext) -> str:
    """Return the thread role the caller acts in, from ``TenantContext.roles``.

    Matching is exact and the preference order is
    :data:`~driver.services.message_service.ALLOWED_SENDER_ROLES` — ``driver``
    first, because this is the driver surface and resolution has already proven
    the caller holds that role. A caller holding neither participating role
    yields the empty string, which the service turns into a 403 (R7.7).
    """
    held = {role for role in (tenant.roles or []) if isinstance(role, str)}
    for role in ALLOWED_SENDER_ROLES:
        if role in held:
            return role
    return ""


def _derive_sender(ref: WorkRef, tenant: TenantContext) -> tuple[str, str]:
    """Return the ``(sender_id, sender_role)`` the message is stamped with.

    ``sender_id`` is the canonical ``driver_id`` on the resolved ``WorkRef``,
    which came from the verified session claim rather than from the request
    (R7.5).
    """
    return ref.driver_id, _derive_sender_role(tenant)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/messages")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def send_message(
    job_id: str,
    body: MessageRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Post a message to a job thread.

    Resolve the ``Work_Ref``, derive the acting identity from
    ``TenantContext``, delegate, store the idempotency response. Thread
    authorization, sender-identity enforcement, persistence, and delivery all
    live below the resolution in
    :class:`~driver.services.message_service.ThreadMessageService` (R7.18).

    The request and response contract of this endpoint is unchanged (R7.15).

    Validates: Requirements 6.1, 6.3, 6.4, 7.5, 7.6, 7.7, 7.10, 7.15, 7.18,
    14.1, 14.3, 14.4
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    ref = await _get_resolver().resolve_job(job_id, tenant)
    sender_id, sender_role = _derive_sender(ref, tenant)
    result = await _get_message_service().send(
        ref,
        body,
        sender_id=sender_id,
        sender_role=sender_role,
        request_id=_get_request_id(request),
    )

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

    Resolve the ``Work_Ref``, delegate. Resolution is the authorization: a
    caller who is not assigned to this job never reaches the thread read
    (R7.8, R7.9).

    The request and response contract of this endpoint is unchanged (R7.15).

    Validates: Requirements 6.2, 7.8, 7.9, 7.12, 7.15, 7.18
    """
    ref = await _get_resolver().resolve_job(job_id, tenant)
    return await _get_message_service().list(
        ref,
        page=page,
        size=size,
        request_id=_get_request_id(request),
    )


# ---------------------------------------------------------------------------
# Order-keyed siblings (R7.14)
# ---------------------------------------------------------------------------


@router.post("/orders/{order_id}/messages")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def send_order_message(
    order_id: str,
    body: MessageRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Post a message to an order thread.

    The job-keyed sibling above with ``resolve_order`` substituted for
    ``resolve_job``: resolve, derive the acting identity, delegate, store the
    idempotency response. No messaging rule lives here, so the two paths cannot
    diverge on a validation rule or an error code (R7.17, R7.19). Assignment
    authorization is the resolution, on the canonical ``assigned_driver_id``
    alone with no dual acceptance (R7.21).

    Validates: Requirements 7.14, 7.17, 7.19, 7.21, 14.1, 14.3, 14.4
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    ref = await _get_resolver().resolve_order(order_id, tenant)
    sender_id, sender_role = _derive_sender(ref, tenant)
    result = await _get_message_service().send(
        ref,
        body,
        sender_id=sender_id,
        sender_role=sender_role,
        request_id=_get_request_id(request),
    )

    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result


@router.get("/orders/{order_id}/messages")
@limiter.limit(_driver_rate)
async def list_order_messages(
    order_id: str,
    request: Request,
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    size: int = Query(50, ge=1, le=200, description="Page size"),
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Return messages for an order thread sorted by timestamp ascending.

    The job-keyed sibling above with ``resolve_order`` substituted for
    ``resolve_job``. Resolution is the authorization: a caller who is not the
    order's assigned driver never reaches the thread read (R7.21).

    Validates: Requirements 7.14, 7.17, 7.19, 7.21
    """
    ref = await _get_resolver().resolve_order(order_id, tenant)
    return await _get_message_service().list(
        ref,
        page=page,
        size=size,
        request_id=_get_request_id(request),
    )
