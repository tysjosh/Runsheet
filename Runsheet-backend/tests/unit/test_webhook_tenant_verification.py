"""
Unit tests for webhook tenant_id verification (Req 9.7).

The webhook receiver derives tenant_id exclusively from the HMAC-verified
payload body. When a webhook_tenant_id is configured (associating the signing
secret with a specific tenant), payloads whose tenant_id does not match are
rejected with 403.

Validates: Requirement 9.7
"""

import hashlib
import hmac
import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Patch the ElasticsearchService singleton BEFORE any ops imports so that
# importing ops_es_service doesn't trigger a real ES connection.
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ops.webhooks.receiver import (
    configure_webhook_receiver,
    router,
)
from ops.ingestion.adapter import AdapterTransformer, TransformResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test-secret-key"


def _make_payload(
    event_id: str = "evt-001",
    tenant_id: str = "tenant-1",
    event_type: str = "shipment_created",
    schema_version: str = "1.0",
) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": schema_version,
        "tenant_id": tenant_id,
        "timestamp": "2025-01-01T12:00:00Z",
        "data": {"shipment_id": "SHP-001", "status": "created"},
    }


def _sign(payload: dict, secret: str = WEBHOOK_SECRET) -> str:
    body = json.dumps(payload).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _build_app(
    *,
    webhook_tenant_id: str = "",
    feature_flag_service=None,
    adapter=None,
    idempotency_service=None,
    poison_queue_service=None,
    ops_es_service=None,
    ws_manager=None,
) -> TestClient:
    """Create a test FastAPI app with the webhook router wired up."""
    app = FastAPI()
    app.include_router(router)

    if adapter is None:
        adapter = MagicMock(spec=AdapterTransformer)
        adapter.is_version_supported.return_value = True
        adapter.transform.return_value = TransformResult(
            shipment_current_doc={"shipment_id": "SHP-001"},
            rider_current_doc=None,
            event_doc={"event_id": "evt-001"},
        )

    if idempotency_service is None:
        idempotency_service = AsyncMock()
        idempotency_service.is_duplicate = AsyncMock(return_value=False)
        idempotency_service.mark_processed = AsyncMock()

    if poison_queue_service is None:
        poison_queue_service = AsyncMock()

    if ops_es_service is None:
        ops_es_service = AsyncMock()
        ops_es_service.append_shipment_event = AsyncMock()
        ops_es_service.upsert_shipment_current = AsyncMock()
        ops_es_service.upsert_rider_current = AsyncMock()

    configure_webhook_receiver(
        adapter=adapter,
        idempotency_service=idempotency_service,
        poison_queue_service=poison_queue_service,
        ops_es_service=ops_es_service,
        ws_manager=ws_manager,
        feature_flag_service=feature_flag_service,
        webhook_secret=WEBHOOK_SECRET,
        webhook_tenant_id=webhook_tenant_id,
    )

    return TestClient(app), adapter, idempotency_service, ops_es_service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebhookTenantVerification:
    """Tenant_id verification in webhook receiver. Validates: Req 9.7"""

    def test_matching_tenant_id_is_accepted(self):
        """Payload tenant_id matching the configured webhook_tenant_id is processed."""
        client, adapter, idemp, ops_es = _build_app(webhook_tenant_id="tenant-1")

        payload = _make_payload(tenant_id="tenant-1")
        sig = _sign(payload)

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(payload),
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "processed"
        adapter.transform.assert_called_once()

    def test_mismatched_tenant_id_is_rejected_with_403(self):
        """Payload tenant_id not matching the configured webhook_tenant_id returns 403."""
        client, adapter, idemp, ops_es = _build_app(webhook_tenant_id="tenant-1")

        payload = _make_payload(tenant_id="tenant-evil")
        sig = _sign(payload)

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(payload),
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )

        assert resp.status_code == 403
        data = resp.json()
        assert data["error_code"] == "TENANT_NOT_FOUND"
        # No downstream processing should occur
        adapter.transform.assert_not_called()
        ops_es.append_shipment_event.assert_not_called()
        idemp.mark_processed.assert_not_called()

    def test_no_webhook_tenant_id_configured_accepts_any_tenant(self):
        """When webhook_tenant_id is empty, any tenant_id in the payload is accepted."""
        client, adapter, idemp, ops_es = _build_app(webhook_tenant_id="")

        payload = _make_payload(tenant_id="any-tenant")
        sig = _sign(payload)

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(payload),
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "processed"
        adapter.transform.assert_called_once()

    def test_tenant_id_derived_from_hmac_verified_body(self):
        """
        The tenant_id used for verification comes from the HMAC-verified
        payload body, not from headers or query params. A tampered body
        would fail HMAC verification first.
        """
        client, adapter, idemp, ops_es = _build_app(webhook_tenant_id="tenant-1")

        # Create a payload with the correct tenant_id and sign it
        payload = _make_payload(tenant_id="tenant-1")
        sig = _sign(payload)

        # Tamper with the body after signing (change tenant_id)
        tampered_payload = _make_payload(tenant_id="tenant-evil")

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(tampered_payload),
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )

        # Should be rejected at HMAC verification (401), not tenant check
        assert resp.status_code == 401
        adapter.transform.assert_not_called()

    def test_tenant_verification_happens_before_feature_flag_check(self):
        """Tenant mismatch is caught before feature flag check runs."""
        ff_service = AsyncMock()
        ff_service.is_enabled = AsyncMock(return_value=True)

        client, adapter, idemp, ops_es = _build_app(
            webhook_tenant_id="tenant-1",
            feature_flag_service=ff_service,
        )

        payload = _make_payload(tenant_id="wrong-tenant")
        sig = _sign(payload)

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(payload),
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )

        assert resp.status_code == 403
        # Feature flag service should not have been called
        ff_service.is_enabled.assert_not_called()
