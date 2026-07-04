"""
Webhook Receiver for Dinee platform events.

Exposes POST /webhooks/dinee that verifies HMAC-SHA256 signatures,
enforces idempotency via Redis, validates schema versions, and delegates
to the AdapterTransformer for normalization before upserting into
Elasticsearch and broadcasting via WebSocket.

During the deprecation window, POST /webhooks/dinee still responds but
internally resolves the reserved channel_id="dinee-legacy" and routes
through the new OrderIntakePipeline. Deprecation headers are emitted on
every response.

Canonical webhook auth policy: HMAC-SHA256 only. The dinee_webhook_secret
is the sole credential for verifying inbound webhooks. The dinee_api_key
is used exclusively for outbound REST API calls to Dinee (Replay Service).

Requirements: 1.1-1.11, 1.3.1, 1.3.3, 1.3.4, 2.2.8
"""

import logging
import re
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from middleware.rate_limiter import limiter
from config.settings import get_settings
from ops.webhooks.hmac_util import verify_hmac_sha256_hex
from ops.ingestion.adapter import AdapterTransformer, WebhookPayload
from ops.services.ops_metrics import (
    ops_webhook_received_total,
    ops_webhook_processed_total,
    ops_ingestion_latency_seconds,
    ops_transform_errors_total,
)

# ---------------------------------------------------------------------------
# Legacy route Prometheus counter (Req 1.3.4)
# ---------------------------------------------------------------------------

from fuel.services.order_metrics import (
    orders_legacy_route_hits_total,
)

logger = logging.getLogger(__name__)

# Semver pattern: major.minor or major.minor.patch
SEMVER_PATTERN = re.compile(r"^\d+\.\d+(\.\d+)?$")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Auth policy declaration for this router (Req 5.2)
# Default: WEBHOOK_HMAC — webhooks use HMAC signature verification
ROUTER_AUTH_POLICY = "webhook_hmac"


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class WebhookResponse(BaseModel):
    """Response returned by the webhook receiver."""

    event_id: str = Field(..., description="The event_id from the payload")
    status: str = Field(
        ...,
        description="Processing outcome: processed | duplicate | queued_for_review",
    )


# ---------------------------------------------------------------------------
# Module-level service references (set during app wiring)
# ---------------------------------------------------------------------------

_adapter: Optional[AdapterTransformer] = None
_idempotency_service = None
_poison_queue_service = None
_ops_es_service = None
_ws_manager = None
_feature_flag_service = None
_webhook_secret: str = ""
_webhook_tenant_id: str = ""
_idempotency_ttl_hours: int = 72

# New pipeline references for deprecation-window routing (Task 7.2)
_order_intake_pipeline: Optional[Any] = None
_intake_channel_repo: Optional[Any] = None
_credentials_vault: Optional[Any] = None

# Reserved channel_id for the legacy dinee route
DINEE_LEGACY_CHANNEL_ID = "dinee-legacy"

# Track whether the dinee-legacy channel has been seeded for each tenant
_dinee_legacy_seeded: dict = {}


def configure_webhook_receiver(
    *,
    adapter: AdapterTransformer,
    idempotency_service,
    poison_queue_service,
    ops_es_service,
    ws_manager=None,
    feature_flag_service=None,
    webhook_secret: str,
    webhook_tenant_id: str = "",
    idempotency_ttl_hours: int = 72,
    order_intake_pipeline=None,
    intake_channel_repo=None,
    credentials_vault=None,
) -> None:
    """
    Wire service dependencies into the webhook receiver module.

    Called once during application startup (from main.py) so that the
    router handlers can access shared services without circular imports.
    """
    global _adapter, _idempotency_service, _poison_queue_service
    global _ops_es_service, _ws_manager, _feature_flag_service
    global _webhook_secret, _webhook_tenant_id, _idempotency_ttl_hours
    global _order_intake_pipeline, _intake_channel_repo, _credentials_vault

    _adapter = adapter
    _idempotency_service = idempotency_service
    _poison_queue_service = poison_queue_service
    _ops_es_service = ops_es_service
    _ws_manager = ws_manager
    _feature_flag_service = feature_flag_service
    _webhook_secret = webhook_secret
    _webhook_tenant_id = webhook_tenant_id
    _idempotency_ttl_hours = idempotency_ttl_hours
    _order_intake_pipeline = order_intake_pipeline
    _intake_channel_repo = intake_channel_repo
    _credentials_vault = credentials_vault


# ---------------------------------------------------------------------------
# HMAC verification helper
# ---------------------------------------------------------------------------


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify HMAC-SHA256 signature of the raw request body.

    Delegates to the shared :func:`verify_hmac_sha256_hex` helper so there is
    a single HMAC verification implementation across the codebase.

    Args:
        body: Raw request body bytes.
        signature: Value of the X-Dinee-Signature header.
        secret: The shared HMAC secret.

    Returns:
        True if the computed HMAC matches the provided signature.
    """
    return verify_hmac_sha256_hex(secret, body, signature)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_deprecation_headers(tenant_id: str) -> dict:
    """Build the deprecation response headers per Req 1.3.3.

    Returns a dict of headers:
        Deprecation: true
        Sunset: <ISO-8601 date from settings>
        Link: </webhooks/orders/{channel_id}>; rel="successor-version"
    """
    settings = get_settings()
    headers: dict = {"Deprecation": "true"}

    sunset_date = settings.orders_legacy_sunset_date
    if sunset_date:
        headers["Sunset"] = sunset_date

    headers["Link"] = (
        f'</webhooks/orders/{DINEE_LEGACY_CHANNEL_ID}>; rel="successor-version"'
    )
    return headers


async def _ensure_dinee_legacy_channel_seeded(tenant_id: str) -> None:
    """Seed the reserved 'dinee-legacy' intake channel on first hit.

    Creates the channel in the intake_channels index and stores the
    existing webhook secret in the credentials vault so the
    OrderIntakePipeline can resolve and verify it.

    This is idempotent — subsequent calls for the same tenant are no-ops.
    """
    global _dinee_legacy_seeded

    if tenant_id in _dinee_legacy_seeded:
        return

    if not _intake_channel_repo or not _credentials_vault:
        # Pipeline services not wired — skip seeding
        return

    # Check if the channel already exists
    existing = await _intake_channel_repo.get_by_channel_id(
        DINEE_LEGACY_CHANNEL_ID
    )
    if existing is not None:
        _dinee_legacy_seeded[tenant_id] = True
        return

    # Seed the channel — store the existing webhook secret in the vault
    try:
        vault_ref = await _credentials_vault.put(
            tenant_id=tenant_id,
            key=f"intake_channel_hmac:{DINEE_LEGACY_CHANNEL_ID}",
            plaintext={"secret": _webhook_secret},
            provider_name="intake_channel",
        )

        from fuel.services.order_es_mappings import INTAKE_CHANNELS_INDEX
        from services.time_utils import utcnow

        now = utcnow().isoformat()
        channel_doc = {
            "channel_id": DINEE_LEGACY_CHANNEL_ID,
            "tenant_id": tenant_id,
            "channel_type": "api_partner",
            "display_name": "Legacy Dinee Webhook (deprecated)",
            "hmac_secret_ref": vault_ref,
            "supported_schema_versions": ["1.0"],
            "rate_limit_per_minute": None,
            "secret_version": 1,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        }

        await _ops_es_service._es.index_document(
            INTAKE_CHANNELS_INDEX, DINEE_LEGACY_CHANNEL_ID, channel_doc
        )

        # Dual-write the legacy intake channel to the Postgres source-of-truth
        # so the PG row (and any rebuild-from-PG of the intake_channels index)
        # includes it. Best-effort: ES write already succeeded above.
        try:
            from commerce.services.commerce_persistence_bridge import (
                mirror_current_state_upsert,
            )
            await mirror_current_state_upsert(
                "intake_channel", channel_doc, doc_id=DINEE_LEGACY_CHANNEL_ID
            )
        except Exception:  # noqa: BLE001 — best-effort during soak
            logger.exception(
                "Postgres dual-write failed for dinee-legacy intake channel "
                "(tenant=%s)", tenant_id,
            )

        _dinee_legacy_seeded[tenant_id] = True
        logger.info(
            "Seeded dinee-legacy intake channel for tenant=%s", tenant_id
        )
    except Exception as exc:
        logger.warning(
            "Failed to seed dinee-legacy channel for tenant=%s: %s",
            tenant_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/dinee", response_model=WebhookResponse)
@limiter.limit(f"{get_settings().ops_webhook_rate_limit}/minute")
async def receive_dinee_webhook(
    request: Request,
    x_dinee_signature: str = Header(..., alias="X-Dinee-Signature"),
) -> WebhookResponse:
    """
    Receive and process a signed Dinee webhook event.

    During the deprecation window, this endpoint still responds but
    internally routes through the new OrderIntakePipeline with the
    reserved channel_id="dinee-legacy". Deprecation headers are emitted
    on every response (Req 1.3.3).

    Flow:
    1. Verify HMAC-SHA256 signature (Req 1.2, 1.3)
    2. If OrderIntakePipeline is available, route through it (Req 2.2.8)
    3. Otherwise, fall back to the legacy processing path
    4. Emit deprecation headers on every response (Req 1.3.3)
    5. Increment orders_legacy_route_hits_total (Req 1.3.4)
    """
    # Generate a request_id for tracing (Req 20.1)
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    source_ip = request.client.host if request.client else "unknown"
    ingest_start = time.monotonic()

    # --- 1. Read raw body and verify HMAC-SHA256 signature ---
    body = await request.body()

    if not _verify_signature(body, x_dinee_signature, _webhook_secret):
        logger.warning(
            "Webhook signature verification failed: request_id=%s, source_ip=%s",
            request_id,
            source_ip,
        )
        ops_webhook_processed_total.labels(tenant_id="unknown", status="rejected").inc()
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "WEBHOOK_SIGNATURE_INVALID",
                "message": "Webhook signature verification failed",
            },
        )

    # --- Parse payload to extract tenant_id for metrics ---
    import json as _json

    try:
        raw = _json.loads(body)
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "INVALID_REQUEST",
                "message": "Invalid JSON payload",
            },
        )

    # Determine tenant_id from payload or configured value
    payload_tenant_id = raw.get("tenant_id", _webhook_tenant_id or "unknown")

    # --- Increment legacy route hits counter (Req 1.3.4) ---
    orders_legacy_route_hits_total.labels(
        route="/webhooks/dinee",
        tenant_id=payload_tenant_id,
    ).inc()

    # --- Route through OrderIntakePipeline if available (Req 2.2.8) ---
    if _order_intake_pipeline is not None:
        try:
            # Seed the dinee-legacy channel on first hit
            await _ensure_dinee_legacy_channel_seeded(payload_tenant_id)

            # Route through the new pipeline
            result = await _order_intake_pipeline.ingest_webhook(
                channel_id=DINEE_LEGACY_CHANNEL_ID,
                body=body,
                signature=x_dinee_signature,
                request_id=request_id,
            )

            # Build response with deprecation headers
            deprecation_headers = _build_deprecation_headers(payload_tenant_id)
            response_content = {
                "event_id": result.event_id,
                "status": result.status,
            }
            if result.order_id:
                response_content["order_id"] = result.order_id

            return JSONResponse(
                status_code=200,
                content=response_content,
                headers=deprecation_headers,
            )
        except Exception as exc:
            # If the pipeline raises a known error, convert to response
            # Check if it's a structured error from errors.exceptions
            error_code = getattr(exc, "error_code", None)
            status_code_val = getattr(exc, "status_code", None)
            if error_code and status_code_val:
                deprecation_headers = _build_deprecation_headers(
                    payload_tenant_id
                )
                return JSONResponse(
                    status_code=status_code_val,
                    content={
                        "error_code": error_code,
                        "message": str(exc),
                    },
                    headers=deprecation_headers,
                )

            # For unexpected errors, log and fall through to legacy path
            logger.warning(
                "OrderIntakePipeline failed for legacy /webhooks/dinee, "
                "falling back to legacy path: request_id=%s, error=%s",
                request_id,
                exc,
            )

    # --- Legacy processing path (fallback) ---
    # This path is used when the OrderIntakePipeline is not wired or
    # when the pipeline encounters an unexpected error.
    try:
        payload = WebhookPayload(**raw)
    except Exception as exc:
        deprecation_headers = _build_deprecation_headers(payload_tenant_id)
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "VALIDATION_ERROR",
                "message": f"Payload validation failed: {exc}",
            },
            headers=deprecation_headers,
        )

    event_id = payload.event_id

    # Record webhook received metric
    ops_webhook_received_total.labels(
        tenant_id=payload.tenant_id,
        schema_version=payload.schema_version,
    ).inc()

    # --- Tenant verification (Req 9.7) ---
    if _webhook_tenant_id and payload.tenant_id != _webhook_tenant_id:
        logger.warning(
            "Webhook tenant_id mismatch: payload tenant_id=%s does not match "
            "tenant associated with signing secret (%s), request_id=%s, source_ip=%s",
            payload.tenant_id,
            _webhook_tenant_id,
            request_id,
            source_ip,
        )
        ops_webhook_processed_total.labels(tenant_id=payload.tenant_id, status="rejected").inc()
        deprecation_headers = _build_deprecation_headers(payload.tenant_id)
        return JSONResponse(
            status_code=403,
            content={
                "error_code": "TENANT_NOT_FOUND",
                "message": "Payload tenant_id does not match the tenant associated with the webhook signing secret",
            },
            headers=deprecation_headers,
        )

    # --- Feature flag check (Req 27.2) ---
    if _feature_flag_service:
        try:
            if not await _feature_flag_service.is_enabled(payload.tenant_id):
                logger.debug(
                    "Feature flag disabled for tenant_id=%s, skipping event_id=%s, request_id=%s",
                    payload.tenant_id,
                    event_id,
                    request_id,
                )
                deprecation_headers = _build_deprecation_headers(payload.tenant_id)
                return JSONResponse(
                    status_code=200,
                    content={"event_id": event_id, "status": "processed"},
                    headers=deprecation_headers,
                )
        except Exception as exc:
            logger.warning(
                "Feature flag check failed for tenant_id=%s, proceeding with processing: %s, request_id=%s",
                payload.tenant_id,
                exc,
                request_id,
            )

    # --- 2. Validate schema_version is semver (Req 1.9) ---
    if not SEMVER_PATTERN.match(payload.schema_version):
        logger.warning(
            "Invalid schema_version format '%s': event_id=%s, request_id=%s",
            payload.schema_version,
            event_id,
            request_id,
        )
        if _poison_queue_service:
            await _poison_queue_service.store_failed_event(
                payload=raw,
                error=f"Invalid schema_version format: {payload.schema_version}",
                error_type="invalid_schema_version",
                tenant_id=payload.tenant_id,
                trace_id=request_id,
            )
        ops_webhook_processed_total.labels(tenant_id=payload.tenant_id, status="queued").inc()
        deprecation_headers = _build_deprecation_headers(payload.tenant_id)
        return JSONResponse(
            status_code=200,
            content={"event_id": event_id, "status": "queued_for_review"},
            headers=deprecation_headers,
        )

    # --- 3. Route unknown schema versions to poison queue (Req 1.10) ---
    if _adapter and not _adapter.is_version_supported(payload.schema_version):
        logger.warning(
            "Unknown schema_version '%s': event_id=%s, request_id=%s — routing to poison queue",
            payload.schema_version,
            event_id,
            request_id,
        )
        if _poison_queue_service:
            await _poison_queue_service.store_failed_event(
                payload=raw,
                error=f"Unknown schema version: {payload.schema_version}",
                error_type="unknown_schema_version",
                tenant_id=payload.tenant_id,
                trace_id=request_id,
            )
        ops_webhook_processed_total.labels(tenant_id=payload.tenant_id, status="queued").inc()
        deprecation_headers = _build_deprecation_headers(payload.tenant_id)
        return JSONResponse(
            status_code=200,
            content={"event_id": event_id, "status": "queued_for_review"},
            headers=deprecation_headers,
        )

    # --- 4. Idempotency check (Req 1.4, 1.5) ---
    if _idempotency_service and await _idempotency_service.is_duplicate(
        event_id, tenant_id=payload.tenant_id
    ):
        logger.debug(
            "Duplicate event_id=%s, returning 200 without reprocessing, request_id=%s",
            event_id,
            request_id,
        )
        ops_webhook_processed_total.labels(tenant_id=payload.tenant_id, status="duplicate").inc()
        deprecation_headers = _build_deprecation_headers(payload.tenant_id)
        return JSONResponse(
            status_code=200,
            content={"event_id": event_id, "status": "duplicate"},
            headers=deprecation_headers,
        )

    # --- 5. Transform via AdapterTransformer (Req 1.6) ---
    try:
        result = _adapter.transform(payload, request_id)
    except Exception as exc:
        logger.error(
            "Adapter transform failed for event_id=%s, request_id=%s: %s",
            event_id,
            request_id,
            exc,
        )
        ops_transform_errors_total.labels(
            tenant_id=payload.tenant_id,
            error_type="transform_error",
        ).inc()
        ops_webhook_processed_total.labels(tenant_id=payload.tenant_id, status="rejected").inc()
        if _poison_queue_service:
            await _poison_queue_service.store_failed_event(
                payload=raw,
                error=str(exc),
                error_type="transform_error",
                tenant_id=payload.tenant_id,
                trace_id=request_id,
            )
        deprecation_headers = _build_deprecation_headers(payload.tenant_id)
        return JSONResponse(
            status_code=200,
            content={"event_id": event_id, "status": "queued_for_review"},
            headers=deprecation_headers,
        )

    # --- 6. Upsert into Elasticsearch (Req 1.6, 1.8) ---
    try:
        if _ops_es_service:
            if result.event_doc:
                await _ops_es_service.append_shipment_event(result.event_doc)
            if result.shipment_current_doc:
                await _ops_es_service.upsert_shipment_current(
                    result.shipment_current_doc
                )
            if result.rider_current_doc:
                await _ops_es_service.upsert_rider_current(
                    result.rider_current_doc
                )
    except Exception as exc:
        logger.error(
            "ES indexing failed for event_id=%s, request_id=%s: %s",
            event_id,
            request_id,
            exc,
        )
        if _poison_queue_service:
            await _poison_queue_service.store_failed_event(
                payload=raw,
                error=str(exc),
                error_type="indexing_error",
                tenant_id=payload.tenant_id,
                trace_id=request_id,
            )
        deprecation_headers = _build_deprecation_headers(payload.tenant_id)
        return JSONResponse(
            status_code=200,
            content={"event_id": event_id, "status": "queued_for_review"},
            headers=deprecation_headers,
        )

    # --- Broadcast via WebSocket (Req 16.2, 16.3) ---
    if _ws_manager:
        try:
            if result.shipment_current_doc:
                await _ws_manager.broadcast_shipment_update(
                    result.shipment_current_doc
                )
            if result.rider_current_doc:
                await _ws_manager.broadcast_rider_update(
                    result.rider_current_doc
                )
        except Exception as exc:
            logger.warning(
                "WebSocket broadcast failed for event_id=%s, request_id=%s: %s",
                event_id,
                request_id,
                exc,
            )

    # --- 7. Mark event_id as processed (Req 1.7) ---
    if _idempotency_service:
        await _idempotency_service.mark_processed(
            event_id, tenant_id=payload.tenant_id
        )

    # --- 8. Return success with deprecation headers ---
    ingest_elapsed = time.monotonic() - ingest_start
    ops_ingestion_latency_seconds.labels(
        tenant_id=payload.tenant_id,
        event_type=payload.event_type,
    ).observe(ingest_elapsed)
    ops_webhook_processed_total.labels(tenant_id=payload.tenant_id, status="processed").inc()

    logger.info(
        "Webhook processed (legacy path): event_id=%s, event_type=%s, tenant_id=%s, request_id=%s",
        event_id,
        payload.event_type,
        payload.tenant_id,
        request_id,
    )

    deprecation_headers = _build_deprecation_headers(payload.tenant_id)
    return JSONResponse(
        status_code=200,
        content={"event_id": event_id, "status": "processed"},
        headers=deprecation_headers,
    )
