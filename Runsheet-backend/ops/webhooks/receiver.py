"""
Webhook Receiver for Dinee platform events.

Exposes POST /webhooks/dinee that verifies HMAC-SHA256 signatures,
enforces idempotency via Redis, validates schema versions, and delegates
to the AdapterTransformer for normalization before upserting into
Elasticsearch and broadcasting via WebSocket.

Canonical webhook auth policy: HMAC-SHA256 only. The dinee_webhook_secret
is the sole credential for verifying inbound webhooks. The dinee_api_key
is used exclusively for outbound REST API calls to Dinee (Replay Service).

Requirements: 1.1-1.11
"""

import hashlib
import hmac
import logging
import re
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field

from middleware.rate_limiter import limiter
from config.settings import get_settings
from ops.ingestion.adapter import AdapterTransformer, WebhookPayload
from ops.services.ops_metrics import (
    ops_webhook_received_total,
    ops_webhook_processed_total,
    ops_ingestion_latency_seconds,
    ops_transform_errors_total,
)

logger = logging.getLogger(__name__)

# Semver pattern: major.minor or major.minor.patch
SEMVER_PATTERN = re.compile(r"^\d+\.\d+(\.\d+)?$")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


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
) -> None:
    """
    Wire service dependencies into the webhook receiver module.

    Called once during application startup (from main.py) so that the
    router handlers can access shared services without circular imports.
    """
    global _adapter, _idempotency_service, _poison_queue_service
    global _ops_es_service, _ws_manager, _feature_flag_service
    global _webhook_secret, _webhook_tenant_id, _idempotency_ttl_hours

    _adapter = adapter
    _idempotency_service = idempotency_service
    _poison_queue_service = poison_queue_service
    _ops_es_service = ops_es_service
    _ws_manager = ws_manager
    _feature_flag_service = feature_flag_service
    _webhook_secret = webhook_secret
    _webhook_tenant_id = webhook_tenant_id
    _idempotency_ttl_hours = idempotency_ttl_hours


# ---------------------------------------------------------------------------
# HMAC verification helper
# ---------------------------------------------------------------------------


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify HMAC-SHA256 signature of the raw request body.

    Args:
        body: Raw request body bytes.
        signature: Value of the X-Dinee-Signature header.
        secret: The shared HMAC secret.

    Returns:
        True if the computed HMAC matches the provided signature.
    """
    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


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

    Flow:
    1. Verify HMAC-SHA256 signature (Req 1.2, 1.3)
    2. Validate schema_version is semver (Req 1.9)
    3. Route unknown schema versions to poison queue (Req 1.10)
    4. Check idempotency (Req 1.4, 1.5)
    5. Transform via AdapterTransformer (Req 1.6)
    6. Upsert into ES and broadcast via WebSocket (Req 1.8)
    7. Mark event_id processed with TTL (Req 1.7)
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
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=401,
            content={
                "error_code": "WEBHOOK_SIGNATURE_INVALID",
                "message": "Webhook signature verification failed",
            },
        )

    # --- Parse payload ---
    import json as _json

    try:
        raw = _json.loads(body)
    except Exception:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=400,
            content={
                "error_code": "INVALID_REQUEST",
                "message": "Invalid JSON payload",
            },
        )

    try:
        payload = WebhookPayload(**raw)
    except Exception as exc:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=400,
            content={
                "error_code": "VALIDATION_ERROR",
                "message": f"Payload validation failed: {exc}",
            },
        )

    event_id = payload.event_id

    # Record webhook received metric
    ops_webhook_received_total.labels(
        tenant_id=payload.tenant_id,
        schema_version=payload.schema_version,
    ).inc()

    # --- Tenant verification (Req 9.7) ---
    # The tenant_id is derived exclusively from the HMAC-verified payload body,
    # ensuring it cannot be spoofed. When a webhook_tenant_id is configured
    # (associating the signing secret with a specific tenant), reject payloads
    # whose tenant_id does not match.
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
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=403,
            content={
                "error_code": "TENANT_NOT_FOUND",
                "message": "Payload tenant_id does not match the tenant associated with the webhook signing secret",
            },
        )

    # --- Feature flag check (Req 27.2) ---
    # Accept but skip processing for disabled tenants, return 200
    if _feature_flag_service:
        try:
            if not await _feature_flag_service.is_enabled(payload.tenant_id):
                logger.debug(
                    "Feature flag disabled for tenant_id=%s, skipping event_id=%s, request_id=%s",
                    payload.tenant_id,
                    event_id,
                    request_id,
                )
                return WebhookResponse(event_id=event_id, status="processed")
        except Exception as exc:
            # If feature flag check fails, log and continue processing
            # (fail-open to avoid dropping events on Redis issues)
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
        # Route to poison queue as unknown version
        if _poison_queue_service:
            await _poison_queue_service.store_failed_event(
                payload=raw,
                error=f"Invalid schema_version format: {payload.schema_version}",
                error_type="invalid_schema_version",
                tenant_id=payload.tenant_id,
                trace_id=request_id,
            )
        ops_webhook_processed_total.labels(tenant_id=payload.tenant_id, status="queued").inc()
        return WebhookResponse(event_id=event_id, status="queued_for_review")

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
        return WebhookResponse(event_id=event_id, status="queued_for_review")

    # --- 4. Idempotency check (Req 1.4, 1.5) ---
    if _idempotency_service and await _idempotency_service.is_duplicate(event_id):
        logger.debug(
            "Duplicate event_id=%s, returning 200 without reprocessing, request_id=%s",
            event_id,
            request_id,
        )
        ops_webhook_processed_total.labels(tenant_id=payload.tenant_id, status="duplicate").inc()
        return WebhookResponse(event_id=event_id, status="duplicate")

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
        return WebhookResponse(event_id=event_id, status="queued_for_review")

    # --- 6. Upsert into Elasticsearch (Req 1.6, 1.8) ---
    try:
        if _ops_es_service:
            # Always append event doc (Req 6.3, 6.9)
            if result.event_doc:
                await _ops_es_service.append_shipment_event(result.event_doc)

            # Upsert shipment current state if present
            if result.shipment_current_doc:
                await _ops_es_service.upsert_shipment_current(
                    result.shipment_current_doc
                )

            # Upsert rider current state if present
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
        return WebhookResponse(event_id=event_id, status="queued_for_review")

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
            # WebSocket broadcast failure is non-fatal
            logger.warning(
                "WebSocket broadcast failed for event_id=%s, request_id=%s: %s",
                event_id,
                request_id,
                exc,
            )

    # --- 7. Mark event_id as processed (Req 1.7) ---
    if _idempotency_service:
        await _idempotency_service.mark_processed(event_id)

    # --- 8. Return success (Req 1.8) ---
    ingest_elapsed = time.monotonic() - ingest_start
    ops_ingestion_latency_seconds.labels(
        tenant_id=payload.tenant_id,
        event_type=payload.event_type,
    ).observe(ingest_elapsed)
    ops_webhook_processed_total.labels(tenant_id=payload.tenant_id, status="processed").inc()

    logger.info(
        "Webhook processed: event_id=%s, event_type=%s, tenant_id=%s, request_id=%s",
        event_id,
        payload.event_type,
        payload.tenant_id,
        request_id,
    )
    return WebhookResponse(event_id=event_id, status="processed")
