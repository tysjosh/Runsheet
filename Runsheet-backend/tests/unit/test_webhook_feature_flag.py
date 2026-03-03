"""
Unit tests for feature flag integration in the webhook receiver.

Tests cover:
- Disabled tenant: webhook returns 200 with status "processed" but skips
  all downstream processing (no ES upsert, no adapter transform, no WS broadcast)
- Enabled tenant: webhook proceeds through normal processing pipeline
- Feature flag service unavailable: fail-open, continue processing

Validates: Requirement 27.2
"""

import hashlib
import hmac
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

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

    # Default mocks
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
    )

    return TestClient(app), adapter, idempotency_service, ops_es_service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebhookFeatureFlagGating:
    """Feature flag integration in webhook receiver. Validates: Req 27.2"""

    def test_disabled_tenant_returns_200_and_skips_processing(self):
        """When feature flag is disabled, return 200 but skip all processing."""
        ff_service = AsyncMock()
        ff_service.is_enabled = AsyncMock(return_value=False)

        client, adapter, idemp, ops_es = _build_app(feature_flag_service=ff_service)

        payload = _make_payload(tenant_id="disabled-tenant")
        sig = _sign(payload)

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(payload),
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["event_id"] == "evt-001"
        assert data["status"] == "processed"

        # Verify no downstream processing occurred
        adapter.transform.assert_not_called()
        ops_es.append_shipment_event.assert_not_called()
        ops_es.upsert_shipment_current.assert_not_called()
        idemp.mark_processed.assert_not_called()

    def test_enabled_tenant_proceeds_with_normal_processing(self):
        """When feature flag is enabled, process the webhook normally."""
        ff_service = AsyncMock()
        ff_service.is_enabled = AsyncMock(return_value=True)

        client, adapter, idemp, ops_es = _build_app(feature_flag_service=ff_service)

        payload = _make_payload(tenant_id="enabled-tenant")
        sig = _sign(payload)

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(payload),
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "processed"

        # Verify normal processing occurred
        adapter.transform.assert_called_once()
        idemp.mark_processed.assert_called_once()

    def test_feature_flag_service_error_fails_open(self):
        """If the feature flag check raises, fail-open and continue processing."""
        ff_service = AsyncMock()
        ff_service.is_enabled = AsyncMock(side_effect=RuntimeError("Redis down"))

        client, adapter, idemp, ops_es = _build_app(feature_flag_service=ff_service)

        payload = _make_payload()
        sig = _sign(payload)

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(payload),
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "processed"

        # Processing should have continued despite the error
        adapter.transform.assert_called_once()

    def test_no_feature_flag_service_proceeds_normally(self):
        """When no feature flag service is configured, process normally."""
        client, adapter, idemp, ops_es = _build_app(feature_flag_service=None)

        payload = _make_payload()
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

    def test_disabled_tenant_does_not_check_idempotency(self):
        """Disabled tenant should skip idempotency check entirely."""
        ff_service = AsyncMock()
        ff_service.is_enabled = AsyncMock(return_value=False)

        idemp = AsyncMock()
        idemp.is_duplicate = AsyncMock(return_value=False)

        client, adapter, _, ops_es = _build_app(
            feature_flag_service=ff_service,
            idempotency_service=idemp,
        )

        payload = _make_payload(tenant_id="disabled-tenant")
        sig = _sign(payload)

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(payload),
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )

        assert resp.status_code == 200
        idemp.is_duplicate.assert_not_called()
