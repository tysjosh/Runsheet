"""
Unit tests for :mod:`fuel.api.order_webhook_endpoints`.

Task 7.5 of the order-intake-pipeline spec. Exercises the webhook
intake surface with a mocked OrderIntakePipeline.

Covers:
* POST /webhooks/orders/{channel_id} happy path
* HMAC signature header required (422 when missing)
* Pipeline receives channel_id, raw body, and signature
* Pipeline error propagation (structured errors)

Validates: Requirements 2.2.1, 2.2.2, 2.2.5, 10.1, 10.2.1
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from errors.exceptions import AppException


# ---------------------------------------------------------------------------
# Fake pipeline result
# ---------------------------------------------------------------------------


@dataclass
class FakeIntakeResponse:
    """Mimics the IntakeResponse from OrderIntakePipeline."""
    event_id: str = "evt_wh001"
    status: str = "processed"
    order_id: Optional[str] = "ord_wh001"


# ---------------------------------------------------------------------------
# Helpers for /webhooks/orders/{channel_id}
# ---------------------------------------------------------------------------


def _build_webhook_app(
    *,
    pipeline_result: Optional[FakeIntakeResponse] = None,
    pipeline_side_effect: Optional[Exception] = None,
):
    """Build a FastAPI app with the order webhook router."""
    from fuel.api.order_webhook_endpoints import (
        configure_order_webhook_endpoints,
        router,
    )

    app = FastAPI()
    app.include_router(router)

    # Register the AppException handler
    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.to_dict()},
        )

    # Mock the pipeline
    pipeline = AsyncMock()
    if pipeline_side_effect:
        pipeline.ingest_webhook = AsyncMock(side_effect=pipeline_side_effect)
    elif pipeline_result:
        pipeline.ingest_webhook = AsyncMock(return_value=pipeline_result)
    else:
        pipeline.ingest_webhook = AsyncMock(
            return_value=FakeIntakeResponse()
        )

    configure_order_webhook_endpoints(order_intake_pipeline=pipeline)

    client = TestClient(app)
    return client, pipeline


# ---------------------------------------------------------------------------
# Tests — /webhooks/orders/{channel_id} (Req 2.2.1, 2.2.2, 2.2.5)
# ---------------------------------------------------------------------------


class TestOrderWebhookEndpoint:
    """POST /webhooks/orders/{channel_id} surface tests."""

    def test_happy_path_returns_200_with_intake_response(self):
        """A valid signed request returns 200 with event_id, status, order_id."""
        client, pipeline = _build_webhook_app()

        resp = client.post(
            "/webhooks/orders/voice-channel-1",
            content=json.dumps({"event_id": "e1", "data": {}}),
            headers={
                "X-Runsheet-Signature": "abc123signature",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["event_id"] == "evt_wh001"
        assert body["status"] == "processed"
        assert body["order_id"] == "ord_wh001"

    def test_pipeline_receives_channel_id(self):
        """Pipeline is called with the channel_id from the URL path."""
        client, pipeline = _build_webhook_app()

        client.post(
            "/webhooks/orders/my-partner-channel",
            content=json.dumps({"event_id": "e1"}),
            headers={
                "X-Runsheet-Signature": "sig",
                "Content-Type": "application/json",
            },
        )

        pipeline.ingest_webhook.assert_called_once()
        call_kwargs = pipeline.ingest_webhook.call_args.kwargs
        assert call_kwargs["channel_id"] == "my-partner-channel"

    def test_pipeline_receives_raw_body_bytes(self):
        """Pipeline receives the raw body as bytes for HMAC computation."""
        client, pipeline = _build_webhook_app()

        payload = json.dumps({"event_id": "e2", "data": {"key": "value"}})
        client.post(
            "/webhooks/orders/ch1",
            content=payload,
            headers={
                "X-Runsheet-Signature": "sig",
                "Content-Type": "application/json",
            },
        )

        call_kwargs = pipeline.ingest_webhook.call_args.kwargs
        assert isinstance(call_kwargs["body"], bytes)
        assert call_kwargs["body"] == payload.encode("utf-8")

    def test_pipeline_receives_signature_header(self):
        """Pipeline receives the X-Runsheet-Signature header value."""
        client, pipeline = _build_webhook_app()

        client.post(
            "/webhooks/orders/ch1",
            content=b"{}",
            headers={
                "X-Runsheet-Signature": "my-hmac-sig-value",
                "Content-Type": "application/json",
            },
        )

        call_kwargs = pipeline.ingest_webhook.call_args.kwargs
        assert call_kwargs["signature"] == "my-hmac-sig-value"

    def test_missing_signature_header_returns_422(self):
        """Missing X-Runsheet-Signature header returns 422 (FastAPI validation)."""
        client, pipeline = _build_webhook_app()

        resp = client.post(
            "/webhooks/orders/ch1",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        # FastAPI returns 422 for missing required headers
        assert resp.status_code == 422

    def test_pipeline_error_propagates_status_code(self):
        """When pipeline raises AppException, the error propagates."""
        from errors.exceptions import channel_disabled

        error = channel_disabled(
            message="Channel is disabled",
            details={"channel_id": "ch-disabled"},
        )
        client, pipeline = _build_webhook_app(pipeline_side_effect=error)

        resp = client.post(
            "/webhooks/orders/ch-disabled",
            content=b"{}",
            headers={
                "X-Runsheet-Signature": "sig",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["error_code"] == "CHANNEL_DISABLED"

    def test_webhook_signature_invalid_returns_401(self):
        """When pipeline raises webhook_signature_invalid, returns 401."""
        from errors.exceptions import webhook_signature_invalid

        error = webhook_signature_invalid(
            message="HMAC mismatch",
            details={"channel_id": "ch1"},
        )
        client, pipeline = _build_webhook_app(pipeline_side_effect=error)

        resp = client.post(
            "/webhooks/orders/ch1",
            content=b"{}",
            headers={
                "X-Runsheet-Signature": "bad-sig",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["detail"]["error_code"] == "WEBHOOK_SIGNATURE_INVALID"

    def test_duplicate_event_returns_200_with_duplicate_status(self):
        """A duplicate event returns 200 with status='duplicate'."""
        result = FakeIntakeResponse(
            event_id="evt-dup", status="duplicate", order_id=None
        )
        client, pipeline = _build_webhook_app(pipeline_result=result)

        resp = client.post(
            "/webhooks/orders/ch1",
            content=b'{"event_id": "evt-dup"}',
            headers={
                "X-Runsheet-Signature": "sig",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "duplicate"
        assert body["order_id"] is None

# ---------------------------------------------------------------------------
# Removed: TestLegacyDineeDeprecationHeaders
# ---------------------------------------------------------------------------
#
# The legacy ``POST /webhooks/dinee`` route emitted ``Deprecation``, ``Sunset``
# and ``Link: rel="successor-version"`` headers pointing at this module's
# ``POST /webhooks/orders/{channel_id}``. The route has now been deleted, so
# there are no deprecation headers to assert and no successor to point at — this
# module *is* the surviving surface. Its own tests are above.
