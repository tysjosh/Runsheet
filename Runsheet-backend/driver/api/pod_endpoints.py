"""
Driver proof of delivery (POD) submission endpoints.

Provides REST endpoints for drivers to obtain a presigned upload URL and to
submit proof of delivery including recipient name, signature, photos, geotag,
and optional OTP.

The POD business rule itself does **not** live here. ``submit_pod`` resolves the
path parameter and the verified :class:`TenantContext` into a
:class:`~driver.services.work_ref.WorkRef` and delegates to
:class:`~driver.services.pod_service.PODSubmissionService`, which holds artifact
validation, refusal handling, OTP verification, gallons resolution, the
hash-chain append, BOL finalization, the timeline event, and the broadcast
exactly once (R5.23, R7.18). The order-keyed sibling resolves through the same
resolver and calls the same service, so the two paths cannot diverge on a
validation rule or on an error code (R7.19).

Collaborators still arrive through the module-level globals
:func:`configure_pod_endpoints` sets — that wiring pattern is preserved rather
than replaced (R5.24, R7.20).

Validates: Requirements 5.23, 5.24, 7.15, 7.18, 7.19, 7.20, 8.1, 8.2, 8.3, 8.4,
8.5
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request

from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.models import PODPresignUploadRequest, PODRequest
from driver.services.pod_service import PODSubmissionService
from driver.services.work_ref import WorkRefResolver
from errors.exceptions import forbidden, invalid_request
from middleware.rate_limiter import driver_rate_key, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.pod_hash_chain_writer import PodHashChainWriter

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Categories permitted for POD uploads (Requirement 4.1.3)
_POD_UPLOAD_CATEGORIES: frozenset[str] = frozenset(
    {"signature", "photo", "meter_ticket", "bol"}
)

# Permitted MIME types for POD uploads (Requirement 4.1.5)
_POD_UPLOAD_ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/heic", "application/pdf"}
)

# Default per-tenant max upload size (Requirement 4.1.5). 10 MiB.
DEFAULT_POD_MAX_FILE_BYTES: int = 10 * 1024 * 1024

# Redis key pattern storing the per-tenant POD upload size override in bytes.
_POD_UPLOAD_LIMIT_KEY_PATTERN: str = "tenant:{tenant_id}:pod_max_file_bytes"

# Module-level service references, wired via configure_pod_endpoints()
_es_service = None
_job_service = None
_scheduling_ws_manager = None
_driver_ws_manager = None
_file_storage_service = None
_redis_client = None
_pod_hash_chain_writer: Optional[PodHashChainWriter] = None
# Task 8.6 — optional POD→BOL finalizer. When wired (via bootstrap), the
# submit_pod handler calls this synchronously after the POD is persisted so
# a Bill of Lading PDF is generated and stored under the tenant-scoped
# ``bol`` category (Req 4.3.4). When not wired, POD submission continues
# unchanged — BOL generation is opt-in at the tenant level via the
# ``overlay.bol_generation`` feature flag (Req 4.3.5).
_pod_bol_finalizer = None
# Task 8.4 — optional meter-ticket OCR service. When wired (via bootstrap),
# the submit_pod handler invokes :meth:`MeterTicketOCRService.extract` when
# a ``meter_ticket_ref`` is supplied without a driver-entered
# ``delivered_gallons`` so the delivered gallon count is populated from the
# photographed meter ticket (Req 4.2.4). A timeout, provider error, or a
# ``requires_manual_review`` flag causes the handler to fall through to
# manual entry and record ``delivered_gallons_source="manual"`` with an
# ``ocr_error`` string (Req 4.2.5, 4.2.6). When not wired, OCR is skipped
# entirely and the POD flow runs exactly as before this task.
_ocr_service = None

# The two globals task 5.5 adds: the resolver that turns a path parameter into a
# ``WorkRef`` and the service that holds the whole POD rule. Both are built by
# ``configure_pod_endpoints`` from the same globals above (R5.24, R7.20).
_work_ref_resolver: Optional[WorkRefResolver] = None
_pod_service: Optional[PODSubmissionService] = None

router = APIRouter(prefix="/api/driver", tags=["driver-pod"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_pod_endpoints(
    *,
    es_service,
    job_service=None,
    order_repository=None,
    order_service=None,
    scheduling_ws_manager=None,
    driver_ws_manager=None,
    file_storage_service=None,
    redis_client=None,
    pod_bol_finalizer=None,
    ocr_service=None,
    reconciliation_service=None,
    pod_hash_chain_writer: Optional[PodHashChainWriter] = None,
) -> None:
    """
    Wire service dependencies into the POD endpoints module.

    Called once during application startup (from bootstrap) so that the
    router handlers can access the shared services.

    ``file_storage_service`` is required for the presigned-upload endpoint
    (``POST /api/driver/pod/uploads/presign``). ``redis_client`` is optional
    — when supplied, the presign handler consults
    ``tenant:{tenant_id}:pod_max_file_bytes`` for a per-tenant max-file-size
    override and otherwise falls back to :data:`DEFAULT_POD_MAX_FILE_BYTES`
    (10 MiB).

    ``pod_bol_finalizer`` is optional. When provided it is invoked
    synchronously after a POD is persisted so a Bill of Lading PDF is
    generated and persisted to the ``bill_of_lading`` ES index — gated by
    the per-tenant ``overlay.bol_generation`` feature flag. A failure in
    this path is swallowed and recorded as ``status: pending_regeneration``
    on the BOL record; POD persistence is never blocked (Req 4.3.4,
    4.3.5).

    ``ocr_service`` is optional. When provided and the POD submission
    carries a ``meter_ticket_ref`` without a driver-entered
    ``delivered_gallons``, :meth:`MeterTicketOCRService.extract` is called
    to populate ``delivered_gallons`` from the photographed meter ticket
    (Req 4.2.4). Timeouts, provider errors, or low-confidence results
    cause the handler to fall through to manual entry with
    ``delivered_gallons_source="manual"`` and an ``ocr_error`` string
    (Req 4.2.5, 4.2.6).

    ``pod_hash_chain_writer`` is optional. When not supplied, a writer is
    instantiated lazily from ``es_service`` + ``redis_client`` so single-
    process deployments and tests need not construct one explicitly.
    Multi-replica deployments SHOULD pass a Redis-backed writer so
    ``pod_hash`` / ``previous_pod_hash`` are serialized across replicas via
    the ``pod_chain_lock:{tenant_id}`` Redis lock (Req 4.5.2).

    ``order_repository`` and ``order_service`` are optional and default to
    ``None``. They feed the order-keyed path: the repository is what
    :meth:`WorkRefResolver.resolve_order` reads, and the service is what the
    POD rule uses for the driver-initiated order status transition. Because
    this function assigns every module global unconditionally, a caller that
    omits them resets them to ``None`` — so **every** call site must pass its
    full argument set or a later pass silently un-wires the order path.
    """
    global _es_service, _job_service
    global _scheduling_ws_manager, _driver_ws_manager
    global _file_storage_service, _redis_client, _pod_bol_finalizer
    global _ocr_service
    global _pod_hash_chain_writer
    global _work_ref_resolver, _pod_service
    _es_service = es_service
    _job_service = job_service
    _scheduling_ws_manager = scheduling_ws_manager
    _driver_ws_manager = driver_ws_manager
    _file_storage_service = file_storage_service
    _redis_client = redis_client
    _pod_bol_finalizer = pod_bol_finalizer
    _ocr_service = ocr_service
    if pod_hash_chain_writer is not None:
        _pod_hash_chain_writer = pod_hash_chain_writer
    elif es_service is not None:
        _pod_hash_chain_writer = PodHashChainWriter(
            es_service=es_service,
            redis_client=redis_client,
        )
    else:
        _pod_hash_chain_writer = None

    # The resolver and the service, built from the globals just assigned.
    _work_ref_resolver = WorkRefResolver(
        job_service=job_service,
        order_repository=order_repository,
    )
    _pod_service = (
        PODSubmissionService(
            es_service=es_service,
            job_service=job_service,
            order_service=order_service,
            order_repository=order_repository,
            file_storage_service=file_storage_service,
            pod_hash_chain_writer=_pod_hash_chain_writer,
            pod_bol_finalizer=pod_bol_finalizer,
            ocr_service=ocr_service,
            reconciliation_service=reconciliation_service,
            driver_ws_manager=driver_ws_manager,
            scheduling_ws_manager=scheduling_ws_manager,
            redis_client=redis_client,
        )
        if es_service is not None
        else None
    )


def _get_resolver() -> WorkRefResolver:
    """Return the configured :class:`WorkRefResolver` or raise."""
    if _work_ref_resolver is None:
        raise RuntimeError(
            "POD endpoints not configured. "
            "Call configure_pod_endpoints() during startup."
        )
    return _work_ref_resolver


def _get_pod_service() -> PODSubmissionService:
    """Return the configured :class:`PODSubmissionService` or raise."""
    if _pod_service is None:
        raise RuntimeError(
            "POD endpoints not configured. "
            "Call configure_pod_endpoints() during startup."
        )
    return _pod_service


def _get_file_storage_service():
    """Return the configured FileStorageService or raise."""
    if _file_storage_service is None:
        raise RuntimeError(
            "POD upload endpoints not configured. "
            "Pass file_storage_service to configure_pod_endpoints() during startup."
        )
    return _file_storage_service


async def _resolve_max_file_bytes(tenant_id: str) -> int:
    """Return the tenant's max POD upload size in bytes.

    Looks up the optional Redis key
    ``tenant:{tenant_id}:pod_max_file_bytes`` when a Redis client is wired
    in; any lookup failure or missing key falls back to
    :data:`DEFAULT_POD_MAX_FILE_BYTES` (10 MiB) so a flaky settings backend
    never blocks uploads for legitimate drivers.

    Validates: Requirement 4.1.5
    """
    if not tenant_id or _redis_client is None:
        return DEFAULT_POD_MAX_FILE_BYTES
    key = _POD_UPLOAD_LIMIT_KEY_PATTERN.format(tenant_id=tenant_id)
    try:
        raw = await _redis_client.get(key)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Failed to read POD upload size override for tenant=%s: %s",
            tenant_id,
            exc,
        )
        return DEFAULT_POD_MAX_FILE_BYTES
    if raw is None:
        return DEFAULT_POD_MAX_FILE_BYTES
    try:
        text = raw.decode() if isinstance(raw, bytes) else raw
        value = int(str(text).strip())
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        logger.warning(
            "Malformed POD upload size override for tenant=%s (%r): %s "
            "— using default",
            tenant_id,
            raw,
            exc,
        )
        return DEFAULT_POD_MAX_FILE_BYTES
    if value <= 0:
        logger.warning(
            "Non-positive POD upload size override for tenant=%s (%d) "
            "— using default",
            tenant_id,
            value,
        )
        return DEFAULT_POD_MAX_FILE_BYTES
    return value


def _get_request_id(request: Request) -> str:
    """Extract request_id from request state (set by RequestIDMiddleware)."""
    return getattr(request.state, "request_id", "unknown")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/pod/uploads/presign")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def presign_pod_upload(
    body: PODPresignUploadRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict:
    """
    Return a presigned PUT URL and ``file_ref`` for a POD artifact.

    Validates the requested ``category`` is one of
    ``{signature, photo, meter_ticket, bol}`` and that ``content_type`` is one
    of ``{image/jpeg, image/png, image/heic, application/pdf}``. Resolves the
    per-tenant max-file-size (default 10 MiB) and forwards it to
    :meth:`FileStorageService.presign_upload` so the caller knows the upper
    bound before uploading.

    Returns a JSON document with ``file_ref``, ``upload_url``, ``expires_at``,
    ``content_type``, and ``max_file_bytes``. The driver client persists
    ``file_ref`` and passes it back on POD submission.

    Validates: Requirements 4.1.3, 4.1.5
    """
    category = (body.category or "").strip()
    content_type = (body.content_type or "").strip()

    if category not in _POD_UPLOAD_CATEGORIES:
        raise invalid_request(
            message="Invalid upload category",
            details={
                "category": category,
                "allowed_categories": sorted(_POD_UPLOAD_CATEGORIES),
            },
        )

    if content_type not in _POD_UPLOAD_ALLOWED_MIME_TYPES:
        raise invalid_request(
            message="Content-Type not permitted for POD uploads",
            details={
                "content_type": content_type,
                "allowed_content_types": sorted(_POD_UPLOAD_ALLOWED_MIME_TYPES),
            },
        )

    file_storage = _get_file_storage_service()
    max_file_bytes = await _resolve_max_file_bytes(tenant.tenant_id)

    try:
        presigned = file_storage.presign_upload(
            tenant_id=tenant.tenant_id,
            category=category,
            content_type=content_type,
            actor=tenant.user_id,
            max_file_bytes=max_file_bytes,
        )
    except PermissionError as exc:
        logger.warning(
            "Presign upload denied for tenant=%s category=%s: %s",
            tenant.tenant_id,
            category,
            exc,
        )
        raise forbidden(
            message="Cross-tenant upload denied",
            details={"reason": "cross_tenant_file_ref"},
        )
    except ValueError as exc:
        # FileStorageService raises ValueError / FileStorageValidationError
        # (a ValueError subclass) on invalid categories, MIMEs, TTLs, or sizes.
        raise invalid_request(
            message="Invalid upload request",
            details={"reason": str(exc)},
        )

    return {
        "data": presigned,
        "request_id": _get_request_id(request),
    }


@router.post("/jobs/{job_id}/pod")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def submit_pod(
    job_id: str,
    body: PODRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Submit proof of delivery for a job.

    Resolve the ``Work_Ref``, delegate, store the idempotency response. Every
    validation rule, authorization rule, and persistence rule lives below the
    resolution in :class:`PODSubmissionService` (R7.18), which the order-keyed
    sibling calls through the same resolver, so the two paths cannot diverge on
    a rule or on an error code (R7.19).

    The request and response contract of this endpoint is unchanged (R7.15).

    Validates: Requirements 5.23, 5.24, 7.15, 7.18, 7.19, 8.1, 8.2, 8.3, 8.4,
    8.5, 14.1, 14.3, 14.4
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    ref = await _get_resolver().resolve_job(job_id, tenant)
    result = await _get_pod_service().submit(
        ref, body, request_id=_get_request_id(request)
    )

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result


@router.post("/orders/{order_id}/pod")
@limiter.limit(_driver_rate, key_func=driver_rate_key)
async def submit_pod_for_order(
    order_id: str,
    body: PODRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Submit proof of delivery for a fuel order — the order-keyed sibling.

    Byte-for-byte identical to :func:`submit_pod` below the resolver call:
    resolve the ``Work_Ref`` through :meth:`WorkRefResolver.resolve_order`,
    delegate to the same :class:`PODSubmissionService`, store the idempotency
    response. This handler carries no validation rule of its own, so the two
    paths cannot diverge on a rule or on an error code (R7.19). The order
    reference comes from the path parameter after the resolver has confirmed
    tenant membership and ``assigned_driver_id`` equality — never from a job
    document (R5.21).

    Same rate limit and same ``X-Idempotency-Key`` handling as the job-keyed
    path (R5.20).

    Validates: Requirements 5.20, 5.21, 5.23, 7.19, 8.1, 8.2, 8.3, 8.4, 8.5,
    14.1, 14.3, 14.4
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    ref = await _get_resolver().resolve_order(order_id, tenant)
    result = await _get_pod_service().submit(
        ref, body, request_id=_get_request_id(request)
    )

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result
