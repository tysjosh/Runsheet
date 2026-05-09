"""
Webhook endpoint for channel-aware order intake.

Exposes ``POST /webhooks/orders/{channel_id}`` that receives HMAC-signed
payloads from registered intake channels, delegates to the
:class:`~fuel.services.order_intake_pipeline.OrderIntakePipeline`, and
returns the :class:`IntakeResponseModel`.

This handler is NOT behind the tenant guard — authentication comes from
the HMAC signature verification + channel resolution inside the pipeline.
The channel's ``tenant_id`` is resolved from the ``intake_channels`` index
during channel lookup.

Validates: Requirements 2.2.1, 2.2.2, 2.2.5.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Router — mounted under /webhooks (no tenant guard dependency)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/webhooks", tags=["order-webhooks"])

# Auth policy: WEBHOOK_HMAC — authentication is handled by the pipeline's
# HMAC verification against the per-channel secret stored in the vault.
ROUTER_AUTH_POLICY = "webhook_hmac"


# ---------------------------------------------------------------------------
# Response model (Pydantic mirror of the pipeline's IntakeResponse dataclass)
# ---------------------------------------------------------------------------


class IntakeResponseModel(BaseModel):
    """HTTP response body for the order webhook endpoint.

    Mirrors :class:`~fuel.services.order_intake_pipeline.IntakeResponse`
    as a Pydantic model so FastAPI can serialize it and generate OpenAPI
    docs.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., description="The idempotency key for this intake attempt")
    status: str = Field(
        ...,
        description="Processing outcome: processed | duplicate | queued_for_review",
    )
    order_id: Optional[str] = Field(
        default=None,
        description="Platform-assigned order ID (set only when status='processed')",
    )


# ---------------------------------------------------------------------------
# Module-level service reference (set during app wiring)
# ---------------------------------------------------------------------------

_order_intake_pipeline = None


def configure_order_webhook_endpoints(*, order_intake_pipeline) -> None:
    """Wire the OrderIntakePipeline into this module.

    Called once during application startup (from ``bootstrap/fuel.py``)
    so that the router handler can access the shared pipeline instance
    without circular imports.

    Args:
        order_intake_pipeline: An instance of
            :class:`~fuel.services.order_intake_pipeline.OrderIntakePipeline`.
    """
    global _order_intake_pipeline
    _order_intake_pipeline = order_intake_pipeline


def _get_pipeline():
    """Return the configured OrderIntakePipeline or raise."""
    if _order_intake_pipeline is None:
        raise RuntimeError(
            "Order webhook endpoint not configured. "
            "Call configure_order_webhook_endpoints() during startup."
        )
    return _order_intake_pipeline


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/orders/{channel_id}", response_model=IntakeResponseModel)
async def receive_order_webhook(
    channel_id: str,
    request: Request,
    x_runsheet_signature: str = Header(..., alias="X-Runsheet-Signature"),
) -> IntakeResponseModel:
    """Receive an HMAC-signed order payload from a registered intake channel.

    Flow:
        1. Read the raw request body as bytes (so the HMAC can be
           computed on the exact bytes received).
        2. Extract the ``X-Runsheet-Signature`` header.
        3. Delegate to ``OrderIntakePipeline.ingest_webhook`` which
           handles channel resolution, HMAC verification, idempotency,
           adapter dispatch, validation, persistence, and broadcast.
        4. Return the :class:`IntakeResponseModel`.

    This endpoint is NOT behind the tenant guard — authentication is
    performed by the pipeline via HMAC verification against the
    per-channel secret stored in the credentials vault.

    Args:
        channel_id: The intake channel identifier from the URL path.
        request: The incoming FastAPI request.
        x_runsheet_signature: The HMAC-SHA256 signature from the
            ``X-Runsheet-Signature`` header.

    Returns:
        An :class:`IntakeResponseModel` with the processing outcome.
    """
    # Generate a request_id for tracing
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())

    # 1. Read raw body as bytes for HMAC computation
    body: bytes = await request.body()

    # 2. Delegate to the pipeline
    pipeline = _get_pipeline()
    result = await pipeline.ingest_webhook(
        channel_id=channel_id,
        body=body,
        signature=x_runsheet_signature,
        request_id=request_id,
    )

    # 3. Return the response model
    return IntakeResponseModel(
        event_id=result.event_id,
        status=result.status,
        order_id=result.order_id,
    )
