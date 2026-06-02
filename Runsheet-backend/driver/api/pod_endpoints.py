"""
Driver proof of delivery (POD) submission endpoints.

Provides a REST endpoint for drivers to submit proof of delivery
including recipient name, signature, photos, geotag, and optional OTP.
POD records are stored in the ``proof_of_delivery`` Elasticsearch index,
appended to the job event timeline, validated for geotag distance, and
broadcast through WebSocket channels.

Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5
"""

import asyncio
import hmac
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request

from config.settings import get_settings
from driver.middleware.idempotency import (
    IdempotencyResult,
    check_idempotency,
    store_idempotency_response,
)
from driver.models import PODPresignUploadRequest, PODRequest
from driver.services.driver_es_mappings import PROOF_OF_DELIVERY_INDEX
from driver.services.geo_utils import haversine_distance_meters
from errors.codes import ErrorCode
from errors.exceptions import AppException, forbidden, invalid_request
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from scheduling.services.scheduling_es_mappings import TENANT_JOB_POLICIES_INDEX
from services.pod_hash_chain_writer import (
    PodChainLockTimeout,
    PodChainPersistenceError,
    PodHashChainWriter,
)

logger = logging.getLogger(__name__)

# Load rate limit settings
_settings = get_settings()
_driver_rate = f"{_settings.ops_api_rate_limit}/minute"

# Default geotag radius in meters
DEFAULT_POD_RADIUS_METERS = 500

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

router = APIRouter(prefix="/api/driver", tags=["driver-pod"])


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def configure_pod_endpoints(
    *,
    es_service,
    job_service=None,
    scheduling_ws_manager=None,
    driver_ws_manager=None,
    file_storage_service=None,
    redis_client=None,
    pod_bol_finalizer=None,
    ocr_service=None,
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
    """
    global _es_service, _job_service
    global _scheduling_ws_manager, _driver_ws_manager
    global _file_storage_service, _redis_client, _pod_bol_finalizer
    global _ocr_service
    global _pod_hash_chain_writer
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


def _get_es_service():
    """Return the configured ElasticsearchService or raise."""
    if _es_service is None:
        raise RuntimeError(
            "POD endpoints not configured. "
            "Call configure_pod_endpoints() during startup."
        )
    return _es_service


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
# Helpers
# ---------------------------------------------------------------------------


async def _get_tenant_policies(tenant_id: str) -> dict:
    """Fetch tenant job policies from ES, returning defaults if not found.

    Returns a dict with keys: pod_required, pod_radius_meters, otp_required.
    """
    es = _get_es_service()
    defaults = {
        "pod_required": False,
        "pod_radius_meters": DEFAULT_POD_RADIUS_METERS,
        "otp_required": False,
    }
    try:
        query = {
            "query": {
                "term": {"tenant_id": tenant_id}
            },
            "size": 1,
        }
        response = await es.search_documents(TENANT_JOB_POLICIES_INDEX, query, size=1)
        hits = response.get("hits", {}).get("hits", [])
        if hits:
            source = hits[0]["_source"]
            return {
                "pod_required": source.get("pod_required", defaults["pod_required"]),
                "pod_radius_meters": source.get("pod_radius_meters", defaults["pod_radius_meters"]),
                "otp_required": source.get("otp_required", defaults["otp_required"]),
            }
    except Exception as exc:
        logger.warning(
            "Failed to fetch tenant policies for %s, using defaults: %s",
            tenant_id,
            exc,
        )
    return defaults


def _extract_expected_otp(job_doc: Optional[dict]) -> Optional[str]:
    """Return the expected delivery OTP provisioned on the job, if any.

    The OTP is provisioned when a delivery is dispatched and shared with
    the recipient out-of-band (SMS/email). It is stored on the job
    document under one of the canonical keys below. Returns ``None`` when
    no OTP has been provisioned so callers can fail closed.
    """
    if not job_doc:
        return None
    for key in ("pod_otp", "delivery_otp", "otp_code", "expected_otp"):
        value = job_doc.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


async def _get_job_destination(job_id: str, tenant_id: str) -> Optional[dict]:
    """Fetch job destination coordinates from the job document.

    Returns a dict with ``lat`` and ``lng`` keys, or None if the job
    has no destination_location.
    """
    if _job_service is None:
        return None
    try:
        job_doc = await _job_service._get_job_doc(job_id, tenant_id)
        dest = job_doc.get("destination_location")
        if dest:
            # ES geo_point can be stored as {"lat": ..., "lon": ...}
            return {"lat": dest.get("lat"), "lng": dest.get("lon", dest.get("lng"))}
    except Exception as exc:
        logger.warning(
            "Failed to fetch job destination for %s: %s", job_id, exc
        )
    return None


def _extract_customer_identity(job_doc: Optional[dict]) -> Optional[str]:
    """Return the customer/account id a POD signature must bind to, if known."""
    if not isinstance(job_doc, dict):
        return None
    for key in ("customer_id", "destination_customer_id", "account_id"):
        value = job_doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    customer = job_doc.get("customer")
    if isinstance(customer, dict):
        value = customer.get("customer_id") or customer.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _validate_geotag(
    geotag_lat: float,
    geotag_lng: float,
    dest_lat: float,
    dest_lng: float,
    radius_meters: float,
) -> bool:
    """Return True if geotag is within radius of destination (no mismatch)."""
    distance = haversine_distance_meters(geotag_lat, geotag_lng, dest_lat, dest_lng)
    return distance <= radius_meters


# ---------------------------------------------------------------------------
# OCR integration (Task 8.4)
# ---------------------------------------------------------------------------


#: Outer OCR budget enforced by the POD endpoint itself. The
#: :class:`MeterTicketOCRService` already applies a per-call timeout of
#: 15 seconds (``DEFAULT_TEXTRACT_TIMEOUT_SECONDS``); this constant acts
#: as a belt-and-braces guard so a misbehaving stub in tests or a
#: custom subclass can't stall the POD endpoint past the documented
#: budget (Req 4.2.6).
_POD_OCR_HARD_TIMEOUT_SECONDS: float = 15.0


#: Return shape from :func:`_resolve_delivered_gallons_via_ocr`.
#:
#: ``delivered_gallons`` carries the numeric value to persist on the POD
#: (or ``None`` when the driver must confirm manually).
#: ``source`` is the value of the ``delivered_gallons_source`` keyword
#: persisted on the POD document (``"ocr"`` or ``"manual"``).
#: ``ocr_result_id`` / ``ocr_confidence`` / ``ocr_requires_manual_review``
#: carry the OCR metadata for observability; any may be ``None`` when OCR
#: was not attempted.
#: ``ocr_error`` is ``None`` on a clean OCR run and a short string
#: (``"textract_timeout"``, ``"ocr_error:<reason>"``,
#: ``"requires_manual_review"``) otherwise — exactly what the spec
#: requires us to log when the flow falls back to manual entry.
async def _resolve_delivered_gallons_via_ocr(
    *,
    tenant_id: str,
    meter_ticket_ref: Optional[str],
    driver_gallons: Optional[float],
    pod_id: str,
    actor: Optional[str],
) -> dict:
    """Drive the meter-ticket OCR pipeline during a POD submission.

    Implements Task 8.4 / Requirements 4.2.4, 4.2.5, 4.2.6:

    * When the driver supplies ``delivered_gallons`` directly (non-null),
      the value is treated as authoritative and OCR is skipped entirely —
      source stays ``"manual"`` and no ``ocr_error`` is recorded.
    * When ``meter_ticket_ref`` is present and ``delivered_gallons`` is
      absent, :meth:`MeterTicketOCRService.extract` is invoked. A
      high-confidence extraction wins: ``delivered_gallons`` is filled
      in and ``source`` is ``"ocr"``.
    * When OCR fails, times out, or surfaces
      ``requires_manual_review=True``, the driver is required to confirm
      or override. We report ``source="manual"``,
      ``delivered_gallons=None``, and populate ``ocr_error`` with a
      short diagnostic string so downstream reconciliation can audit the
      fall-back path.
    * When no ``meter_ticket_ref`` is supplied at all, OCR is skipped and
      the result is a pure-manual default (both fields ``None``, source
      ``"manual"``, no error).

    The 15-second timeout required by Requirement 4.2.6 is enforced by
    :class:`MeterTicketOCRService` itself (``DEFAULT_TEXTRACT_TIMEOUT_SECONDS``);
    on top of that we also wrap the whole call in an ``asyncio.wait_for``
    so a misconfigured OCR service (or an injected test stub that forgets
    to honor the internal timeout) cannot stall the POD endpoint past the
    documented budget.

    ``PermissionError`` propagates unchanged — cross-tenant OCR requests
    must still translate into HTTP 403 on the submit_pod handler.
    """
    # Fast path 1: driver-entered gallons always win (Req 4.2.4 inverse —
    # OCR only runs when the driver did not type a value).
    if driver_gallons is not None:
        return {
            "delivered_gallons": float(driver_gallons),
            "source": "manual",
            "ocr_result_id": None,
            "ocr_confidence": None,
            "ocr_requires_manual_review": None,
            "ocr_error": None,
        }

    # Fast path 2: no ticket ref and no driver value — nothing to do.
    if not meter_ticket_ref:
        return {
            "delivered_gallons": None,
            "source": "manual",
            "ocr_result_id": None,
            "ocr_confidence": None,
            "ocr_requires_manual_review": None,
            "ocr_error": None,
        }

    # Fast path 3: OCR service not wired — silently fall through to
    # manual entry so POD flow is robust in environments that haven't
    # provisioned Textract credentials yet. No ocr_error because no
    # attempt was made.
    if _ocr_service is None:
        return {
            "delivered_gallons": None,
            "source": "manual",
            "ocr_result_id": None,
            "ocr_confidence": None,
            "ocr_requires_manual_review": None,
            "ocr_error": None,
        }

    # Main path: call the OCR service. The service wraps its own call in
    # the 15-second timeout specified by Req 4.2.6 and returns a failure
    # record rather than raising on provider errors — but we still guard
    # with an outer timeout so a misbehaving stub can't stall the POD
    # endpoint indefinitely.
    try:
        result = await asyncio.wait_for(
            _ocr_service.extract(
                tenant_id=tenant_id,
                file_ref=meter_ticket_ref,
                pod_id=pod_id,
                actor=actor,
            ),
            timeout=_POD_OCR_HARD_TIMEOUT_SECONDS,
        )
    except PermissionError:
        # Tenant-isolation failure — surface to the caller so submit_pod
        # can translate it into HTTP 403 (Req 4.1.4, 4.1.6).
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "OCR outer timeout after %.1fs for pod_id=%s tenant=%s",
            _POD_OCR_HARD_TIMEOUT_SECONDS,
            pod_id,
            tenant_id,
        )
        return {
            "delivered_gallons": None,
            "source": "manual",
            "ocr_result_id": None,
            "ocr_confidence": None,
            "ocr_requires_manual_review": None,
            "ocr_error": "textract_timeout",
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "OCR call raised unexpectedly for pod_id=%s tenant=%s: %s",
            pod_id,
            tenant_id,
            exc,
        )
        return {
            "delivered_gallons": None,
            "source": "manual",
            "ocr_result_id": None,
            "ocr_confidence": None,
            "ocr_requires_manual_review": None,
            "ocr_error": f"ocr_error:{type(exc).__name__}",
        }

    ocr_result_id = getattr(result, "ocr_result_id", None)
    confidence = getattr(result, "confidence", None)
    extracted = getattr(result, "extracted_gallons", None)
    requires_review = getattr(result, "requires_manual_review", True)
    service_error = getattr(result, "error_details", None)

    # Requirement 4.2.5: when OCR returns a failure record (service_error
    # non-null) OR requires_manual_review is true OR no numeric value was
    # parsed, fall back to manual entry. Otherwise accept the OCR value.
    if service_error:
        return {
            "delivered_gallons": None,
            "source": "manual",
            "ocr_result_id": ocr_result_id,
            "ocr_confidence": confidence,
            "ocr_requires_manual_review": True,
            "ocr_error": service_error,
        }
    if extracted is None or requires_review:
        return {
            "delivered_gallons": None,
            "source": "manual",
            "ocr_result_id": ocr_result_id,
            "ocr_confidence": confidence,
            "ocr_requires_manual_review": True,
            "ocr_error": "requires_manual_review",
        }

    # Happy path — OCR gave us a usable gallon count (Req 4.2.4).
    return {
        "delivered_gallons": float(extracted),
        "source": "ocr",
        "ocr_result_id": ocr_result_id,
        "ocr_confidence": confidence,
        "ocr_requires_manual_review": False,
        "ocr_error": None,
    }


async def _broadcast_pod_event(
    event_type: str,
    event_data: dict,
    driver_id: Optional[str] = None,
    tenant_id: str = "",
) -> None:
    """Broadcast a POD event through both WS managers.

    Validates: Requirement 8.4
    """
    # Broadcast through scheduling WS (for dispatchers)
    if _scheduling_ws_manager is not None:
        try:
            await _scheduling_ws_manager.broadcast(
                event_type,
                event_data,
                tenant_id=tenant_id or event_data.get("tenant_id", ""),
            )
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


@router.post("/pod/uploads/presign")
@limiter.limit(_driver_rate)
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
@limiter.limit(_driver_rate)
async def submit_pod(
    job_id: str,
    body: PODRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    idempotency: IdempotencyResult = Depends(check_idempotency),
) -> dict:
    """
    Submit proof of delivery for a job.

    Stores the POD record in the ``proof_of_delivery`` ES index, appends
    a ``pod_submitted`` event to the job timeline, validates geotag
    distance against the job destination, and optionally validates OTP
    when the tenant has OTP verification enabled.

    When a ``meter_ticket_ref`` is supplied without a driver-entered
    ``delivered_gallons``, the handler invokes
    :class:`MeterTicketOCRService` to extract the gallon count (Task 8.4
    / Req 4.2.4). A high-confidence OCR result is persisted with
    ``delivered_gallons_source="ocr"``. When OCR fails, times out
    (15-second budget), or flags ``requires_manual_review=True``, the
    handler falls through to manual entry with
    ``delivered_gallons_source="manual"`` and an ``ocr_error`` string
    for downstream reconciliation (Req 4.2.5, 4.2.6).

    Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 14.1, 14.3, 14.4,
    4.2.4, 4.2.5, 4.2.6
    """
    if idempotency.is_replay:
        return idempotency.replay_response()

    es = _get_es_service()

    now = datetime.now(timezone.utc).isoformat()
    pod_id = str(uuid.uuid4())

    # Access control: reject requests from non-assigned driver (Req 11.2)
    job_doc: Optional[dict] = None
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
        except Exception as exc:
            logger.warning(
                "Failed to fetch job %s for POD access checks tenant=%s: %s",
                job_id,
                tenant.tenant_id,
                exc,
            )

    # Resolve artifact references. file_refs are preferred; raw URLs are
    # accepted for backward compatibility but marked deprecated (Req 4.1.4).
    signature_ref = (body.signature_ref or "").strip() or None
    photo_refs_list = [p for p in (body.photo_refs or []) if p]
    meter_ticket_ref = (body.meter_ticket_ref or "").strip() or None
    legacy_signature_url = (body.signature_url or "").strip() or None
    legacy_photo_urls = [p for p in (body.photo_urls or []) if p]

    is_refusal = bool(body.refused_delivery)
    refusal_reason_code = (
        body.refusal_reason_code.value if body.refusal_reason_code is not None else None
    )
    refusal_note = (body.refusal_note or "").strip() or None

    if is_refusal and not refusal_reason_code:
        raise invalid_request(
            message="refusal_reason_code is required for refused deliveries",
            details={
                "missing": ["refusal_reason_code"],
                "allowed_reason_codes": [
                    "customer_refused",
                    "customer_unavailable",
                    "access_denied",
                    "unsafe_site",
                    "wrong_product",
                    "insufficient_capacity",
                    "payment_hold",
                    "other",
                ],
            },
        )

    if not is_refusal and not signature_ref and not legacy_signature_url:
        raise invalid_request(
            message="signature is required",
            details={"missing": ["signature_ref"]},
        )
    if not photo_refs_list and not legacy_photo_urls:
        raise invalid_request(
            message="at least one photo is required",
            details={"missing": ["photo_refs"]},
        )

    expected_customer_id = _extract_customer_identity(job_doc)
    submitted_customer_id = (body.customer_id or "").strip() or None
    signature_customer_validated = False
    if signature_ref and expected_customer_id:
        if not submitted_customer_id:
            raise invalid_request(
                message="customer_id is required when submitting a signature_ref",
                details={
                    "missing": ["customer_id"],
                    "reason": "signature_customer_identity_required",
                },
            )
        if submitted_customer_id != expected_customer_id:
            raise forbidden(
                message="Signature customer identity mismatch",
                details={
                    "reason": "signature_customer_identity_mismatch",
                    "expected_customer_id": expected_customer_id,
                    "submitted_customer_id": submitted_customer_id,
                },
            )
        signature_customer_validated = True

    # Validate every supplied file_ref belongs to the submitting tenant.
    # FileStorageService raises PermissionError on cross-tenant refs, which
    # we translate into HTTP 403 (Req 4.1.4, 4.1.6).
    refs_to_validate: list[tuple[str, str]] = []
    if signature_ref:
        refs_to_validate.append(("signature_ref", signature_ref))
    for idx, ref in enumerate(photo_refs_list):
        refs_to_validate.append((f"photo_refs[{idx}]", ref))
    if meter_ticket_ref:
        refs_to_validate.append(("meter_ticket_ref", meter_ticket_ref))

    if refs_to_validate:
        file_storage = _get_file_storage_service()
        for field_name, ref in refs_to_validate:
            try:
                file_storage.validate_ref(
                    tenant_id=tenant.tenant_id,
                    file_ref=ref,
                    actor=tenant.user_id,
                )
            except PermissionError as exc:
                logger.warning(
                    "Cross-tenant POD file_ref denied: tenant=%s field=%s ref=%s err=%s",
                    tenant.tenant_id,
                    field_name,
                    ref,
                    exc,
                )
                raise forbidden(
                    message="Cross-tenant file_ref denied",
                    details={
                        "reason": "cross_tenant_file_ref",
                        "field": field_name,
                    },
                )
            except ValueError as exc:
                raise invalid_request(
                    message="Invalid file_ref",
                    details={"field": field_name, "reason": str(exc)},
                )

    # Fetch tenant policies for OTP and radius configuration
    policies = await _get_tenant_policies(tenant.tenant_id)
    radius_meters = policies.get("pod_radius_meters", DEFAULT_POD_RADIUS_METERS)
    otp_required = policies.get("otp_required", False)

    # OTP validation (Req 8.5)
    #
    # When a tenant requires OTP, the driver-submitted code is verified
    # against the expected OTP provisioned on the job document
    # (``pod_otp``/``delivery_otp`` — generated when the delivery is
    # dispatched and shared with the customer out-of-band). Verification
    # uses a constant-time comparison to avoid leaking the code through
    # timing. The handler fails *closed*: if the job has no provisioned
    # OTP we cannot prove the driver is at the right delivery, so the
    # submission is rejected rather than rubber-stamped.
    otp_verified = False
    if otp_required:
        submitted_otp = (body.otp or "").strip()
        if not submitted_otp:
            return {
                "error": "OTP is required for this tenant",
                "error_code": "OTP_REQUIRED",
                "request_id": _get_request_id(request),
            }

        expected_otp = _extract_expected_otp(job_doc)
        if not expected_otp:
            # Fail closed — OTP is required but none was provisioned for
            # this job. Accepting any code here would defeat the control.
            logger.warning(
                "POD OTP required but no expected OTP provisioned: "
                "tenant=%s job=%s",
                tenant.tenant_id,
                job_id,
            )
            return {
                "error": (
                    "OTP verification is required but no OTP was "
                    "provisioned for this delivery"
                ),
                "error_code": "OTP_NOT_PROVISIONED",
                "request_id": _get_request_id(request),
            }

        if not hmac.compare_digest(submitted_otp, str(expected_otp).strip()):
            logger.warning(
                "POD OTP mismatch: tenant=%s job=%s driver=%s",
                tenant.tenant_id,
                job_id,
                tenant.user_id,
            )
            return {
                "error": "The OTP provided is invalid",
                "error_code": "OTP_INVALID",
                "request_id": _get_request_id(request),
            }

        otp_verified = True

    # Geotag distance validation (Req 8.3)
    location_mismatch = False
    destination = await _get_job_destination(job_id, tenant.tenant_id)
    if destination and destination.get("lat") is not None and destination.get("lng") is not None:
        location_mismatch = not _validate_geotag(
            body.geotag.lat,
            body.geotag.lng,
            destination["lat"],
            destination["lng"],
            radius_meters,
        )

    # Task 8.4 / Req 4.2.4–4.2.6 — resolve delivered_gallons from either
    # the driver's submitted value or the meter-ticket OCR pipeline.
    # Cross-tenant meter_ticket_refs are caught by file_storage.validate_ref
    # above, but the OCR helper also guards with its own tenant-scoped S3
    # read so the two layers of protection stay in lock-step.
    try:
        ocr_resolution = await _resolve_delivered_gallons_via_ocr(
            tenant_id=tenant.tenant_id,
            meter_ticket_ref=meter_ticket_ref,
            driver_gallons=body.delivered_gallons,
            pod_id=pod_id,
            actor=tenant.user_id,
        )
    except PermissionError as exc:
        logger.warning(
            "Cross-tenant meter_ticket_ref denied during OCR: tenant=%s ref=%s err=%s",
            tenant.tenant_id,
            meter_ticket_ref,
            exc,
        )
        raise forbidden(
            message="Cross-tenant file_ref denied",
            details={
                "reason": "cross_tenant_file_ref",
                "field": "meter_ticket_ref",
            },
        )

    # Build POD document for ES (Req 8.1, 4.1.4). When file_refs are supplied
    # we persist them alongside legacy URL fields for backward-compat readers.
    # The deprecated URL fields are echoed back only when no file_ref is given.
    effective_signature_url = legacy_signature_url if not signature_ref else ""
    effective_photo_urls = legacy_photo_urls if not photo_refs_list else []

    # Resolve order_id + delivered_at for the POD hash chain (Req 4.5.1).
    # ``order_id`` is looked up from the job document when present (orders in
    # the MVP are still modeled by job_id — the canonical payload treats a
    # missing order_id as the empty string so hashing is still deterministic
    # per Task 8.9). ``delivered_at`` mirrors the body ``timestamp`` so the
    # canonicalization emits a single ISO 8601 ``Z``-terminated value.
    pod_order_id = ""
    try:
        if job_doc is not None:
            pod_order_id = str(job_doc.get("order_id") or "")
        elif _job_service is not None:
            job_doc_for_hash = await _job_service._get_job_doc(
                job_id, tenant.tenant_id
            )
            pod_order_id = str(job_doc_for_hash.get("order_id") or "")
    except Exception:  # pragma: no cover - defensive
        pod_order_id = ""
    delivered_at_value = body.timestamp
    pod_status = "refused" if is_refusal else "submitted"
    pod_event_type = "delivery_refused" if is_refusal else "pod_submitted"

    pod_doc = {
        "pod_id": pod_id,
        "job_id": job_id,
        "order_id": pod_order_id,
        "recipient_name": body.recipient_name,
        "customer_id": submitted_customer_id,
        "expected_customer_id": expected_customer_id,
        "signature_customer_validated": signature_customer_validated,
        "signature_ref": signature_ref,
        "photo_refs": photo_refs_list,
        "meter_ticket_ref": meter_ticket_ref,
        "signature_url": effective_signature_url,
        "photo_urls": effective_photo_urls,
        "delivered_gallons": ocr_resolution["delivered_gallons"],
        "delivered_gallons_source": ocr_resolution["source"],
        "ocr_result_id": ocr_resolution["ocr_result_id"],
        "ocr_confidence": ocr_resolution["ocr_confidence"],
        "ocr_requires_manual_review": ocr_resolution["ocr_requires_manual_review"],
        "ocr_error": ocr_resolution["ocr_error"],
        "delivered_at": delivered_at_value,
        "geotag": {"lat": body.geotag.lat, "lon": body.geotag.lng},
        "timestamp": body.timestamp,
        "otp_verified": otp_verified,
        "location_mismatch": location_mismatch,
        "status": pod_status,
        "refused_delivery": is_refusal,
        "refusal_reason_code": refusal_reason_code,
        "refusal_note": refusal_note,
        "tenant_id": tenant.tenant_id,
    }

    # Persist POD with atomic hash-chain fields (Req 4.5.2). The writer
    # serializes concurrent submissions per tenant via a Redis lock
    # (``pod_chain_lock:{tenant_id}`` with 5s TTL), reads the tenant's
    # latest ``pod_hash`` (or the zero-hash for the first POD), computes the
    # new ``pod_hash`` from the canonical payload, and writes the POD
    # document under the lock. The persisted record (with ``pod_hash``,
    # ``previous_pod_hash``, ``chain_sequence``) replaces the pre-hash
    # ``pod_doc`` for all downstream steps so the emitted event / response
    # carries the hash fields.
    if _pod_hash_chain_writer is not None:
        try:
            pod_doc = await _pod_hash_chain_writer.persist(
                tenant_id=tenant.tenant_id,
                pod_doc=pod_doc,
            )
        except PodChainLockTimeout as exc:
            logger.error(
                "POD hash-chain lock timeout for tenant=%s pod_id=%s: %s",
                tenant.tenant_id,
                pod_id,
                exc,
            )
            raise AppException(
                error_code=ErrorCode.SESSION_STORE_UNAVAILABLE,
                message="POD persistence is temporarily busy — please retry",
                status_code=503,
                details={
                    "reason": "pod_chain_lock_timeout",
                    "tenant_id": tenant.tenant_id,
                    "pod_id": pod_id,
                },
            ) from exc
        except PodChainPersistenceError as exc:
            logger.error(
                "POD hash-chain persistence failed for tenant=%s pod_id=%s: %s",
                tenant.tenant_id,
                pod_id,
                exc,
            )
            raise AppException(
                error_code=ErrorCode.INTERNAL_ERROR,
                message="Failed to persist POD record",
                status_code=500,
                details={
                    "reason": "pod_persistence_failed",
                    "tenant_id": tenant.tenant_id,
                    "pod_id": pod_id,
                },
            ) from exc
    else:
        # Fallback for misconfigured deployments: write without a chain so
        # the POD is not lost. A loud log line surfaces the misconfiguration
        # for operators; downstream hash-verification will flag the POD as
        # un-chained on first audit.
        logger.error(
            "POD hash-chain writer not configured — POD %s persisted without "
            "pod_hash / previous_pod_hash (tenant=%s). Call "
            "configure_pod_endpoints() with pod_hash_chain_writer or "
            "es_service to enable hashing.",
            pod_id,
            tenant.tenant_id,
        )
        await es.index_document(PROOF_OF_DELIVERY_INDEX, pod_id, pod_doc)

    # Task 8.6 — synchronous BOL generation on POD finalization (Req 4.3.4,
    # 4.3.5). Gated by the ``overlay.bol_generation`` feature flag per
    # tenant. The finalizer never raises; a BOL failure is swallowed and
    # a ``pending_regeneration`` stub record is persisted to
    # ``bill_of_lading`` so it surfaces via GET /api/fuel/pod/{pod_id}/bol.
    if _pod_bol_finalizer is not None:
        try:
            await _pod_bol_finalizer.maybe_generate(
                tenant_id=tenant.tenant_id,
                pod=pod_doc,
                actor=tenant.user_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Double guard: the finalizer is documented to never raise, but
            # a truly unexpected error must still not block POD persistence.
            logger.warning(
                "POD BOL finalizer raised unexpectedly for pod_id=%s: %s",
                pod_id,
                exc,
            )

    # Append pod_submitted event to job timeline (Req 8.1)
    if _job_service is not None:
        try:
            await _job_service._append_event(
                job_id=job_id,
                event_type=pod_event_type,
                tenant_id=tenant.tenant_id,
                actor_id=tenant.user_id,
                payload={
                    "pod_id": pod_id,
                    "recipient_name": body.recipient_name,
                    "location_mismatch": location_mismatch,
                    "otp_verified": otp_verified,
                    "status": pod_status,
                    "refused_delivery": is_refusal,
                    "refusal_reason_code": refusal_reason_code,
                    "timestamp": now,
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to append pod_submitted event for job %s: %s",
                job_id,
                exc,
            )

    # Broadcast POD event through WS (Req 8.4)
    pod_event_data = {
        "job_id": job_id,
        "pod_id": pod_id,
        "recipient_name": body.recipient_name,
        "location_mismatch": location_mismatch,
        "otp_verified": otp_verified,
        "status": pod_status,
        "refused_delivery": is_refusal,
        "refusal_reason_code": refusal_reason_code,
        "timestamp": now,
        "tenant_id": tenant.tenant_id,
    }
    await _broadcast_pod_event(
        pod_event_type,
        pod_event_data,
        driver_id=tenant.user_id,
        tenant_id=tenant.tenant_id,
    )

    result = {
        "data": pod_doc,
        "request_id": _get_request_id(request),
    }

    # Store idempotency response (Req 14.2)
    if idempotency.key:
        await store_idempotency_response(
            idempotency.key, tenant.tenant_id, result
        )

    return result
