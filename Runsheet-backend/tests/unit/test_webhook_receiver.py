"""
Unit tests for the Dinee webhook receiver endpoint.

Tests cover:
- HMAC-SHA256 signature verification (valid, invalid, missing)
- Idempotency (duplicate event_id returns 200 without reprocessing)
- schema_version validation (known, unknown, deprecated)
- Feature flag gating (disabled tenant skips processing)

Validates: Requirements 1.1-1.11, 24.4, 24.5
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
):
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

    client = TestClient(app)
    return client, adapter, idempotency_service, poison_queue_service, ops_es_service


def _post_webhook(client, payload, secret=WEBHOOK_SECRET, headers=None):
    """Helper to POST a signed webhook payload."""
    body = json.dumps(payload)
    sig = _sign(payload, secret)
    default_headers = {
        "X-Dinee-Signature": sig,
        "Content-Type": "application/json",
    }
    if headers:
        default_headers.update(headers)
    return client.post("/webhooks/dinee", content=body, headers=default_headers)


# ---------------------------------------------------------------------------
# HMAC Signature Verification Tests — Validates: Req 1.2, 1.3
# ---------------------------------------------------------------------------


class TestHMACSignatureVerification:
    """HMAC-SHA256 signature verification. Validates: Req 1.2, 1.3"""

    def test_valid_signature_returns_200(self):
        """A correctly signed payload is accepted and processed."""
        client, adapter, idemp, pq, ops_es = _build_app()
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["event_id"] == "evt-001"
        assert data["status"] == "processed"
        adapter.transform.assert_called_once()

    def test_invalid_signature_returns_401(self):
        """A payload signed with the wrong secret is rejected with 401."""
        client, adapter, idemp, pq, ops_es = _build_app()
        payload = _make_payload()
        body = json.dumps(payload)
        wrong_sig = hmac.new(
            b"wrong-secret", body.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        resp = client.post(
            "/webhooks/dinee",
            content=body,
            headers={
                "X-Dinee-Signature": wrong_sig,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 401
        data = resp.json()
        assert data["error_code"] == "WEBHOOK_SIGNATURE_INVALID"
        adapter.transform.assert_not_called()

    def test_missing_signature_header_returns_422(self):
        """A request without the X-Dinee-Signature header is rejected."""
        client, adapter, idemp, pq, ops_es = _build_app()
        payload = _make_payload()

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

        # FastAPI returns 422 for missing required header
        assert resp.status_code == 422
        adapter.transform.assert_not_called()

    def test_tampered_body_fails_signature(self):
        """If the body is tampered after signing, HMAC verification fails."""
        client, adapter, idemp, pq, ops_es = _build_app()
        original = _make_payload()
        sig = _sign(original)

        tampered = _make_payload(event_id="evt-tampered")

        resp = client.post(
            "/webhooks/dinee",
            content=json.dumps(tampered),
            headers={
                "X-Dinee-Signature": sig,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 401
        adapter.transform.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotency Tests — Validates: Req 1.4, 1.5, 1.7
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Idempotent processing via event_id deduplication. Validates: Req 1.4, 1.5, 1.7"""

    def test_duplicate_event_id_returns_200_without_reprocessing(self):
        """A duplicate event_id returns 200 with status 'duplicate' and skips processing."""
        idemp = AsyncMock()
        idemp.is_duplicate = AsyncMock(return_value=True)
        idemp.mark_processed = AsyncMock()

        client, adapter, _, pq, ops_es = _build_app(idempotency_service=idemp)
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "duplicate"
        assert data["event_id"] == "evt-001"
        # No downstream processing
        adapter.transform.assert_not_called()
        ops_es.append_shipment_event.assert_not_called()
        idemp.mark_processed.assert_not_called()

    def test_new_event_id_is_processed_and_marked(self):
        """A new event_id is processed and then marked in the idempotency store."""
        idemp = AsyncMock()
        idemp.is_duplicate = AsyncMock(return_value=False)
        idemp.mark_processed = AsyncMock()

        client, adapter, _, pq, ops_es = _build_app(idempotency_service=idemp)
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"
        idemp.is_duplicate.assert_called_once_with("evt-001")
        idemp.mark_processed.assert_called_once_with("evt-001")
        adapter.transform.assert_called_once()


# ---------------------------------------------------------------------------
# Schema Version Validation Tests — Validates: Req 1.9, 1.10
# ---------------------------------------------------------------------------


class TestSchemaVersionValidation:
    """schema_version validation and routing. Validates: Req 1.9, 1.10"""

    def test_known_version_is_processed(self):
        """A payload with a known schema_version is processed normally."""
        client, adapter, idemp, pq, ops_es = _build_app()
        payload = _make_payload(schema_version="1.0")
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"
        adapter.transform.assert_called_once()

    def test_unknown_version_routes_to_poison_queue(self):
        """A payload with an unknown schema_version is routed to the poison queue."""
        adapter = MagicMock(spec=AdapterTransformer)
        adapter.is_version_supported.return_value = False

        pq = AsyncMock()

        client, _, idemp, _, ops_es = _build_app(adapter=adapter, poison_queue_service=pq)
        payload = _make_payload(schema_version="99.0")
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued_for_review"
        assert data["event_id"] == "evt-001"
        pq.store_failed_event.assert_called_once()
        call_kwargs = pq.store_failed_event.call_args
        assert "unknown_schema_version" in str(call_kwargs)
        adapter.transform.assert_not_called()

    def test_invalid_semver_format_routes_to_poison_queue(self):
        """A payload with a non-semver schema_version is routed to the poison queue."""
        pq = AsyncMock()
        client, adapter, idemp, _, ops_es = _build_app(poison_queue_service=pq)
        payload = _make_payload(schema_version="not-a-version")
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued_for_review"
        pq.store_failed_event.assert_called_once()
        call_kwargs = pq.store_failed_event.call_args
        assert "invalid_schema_version" in str(call_kwargs)
        adapter.transform.assert_not_called()

    def test_deprecated_version_is_still_processed(self):
        """A deprecated but supported schema_version is processed (not routed to poison queue)."""
        adapter = MagicMock(spec=AdapterTransformer)
        adapter.is_version_supported.return_value = True
        adapter.transform.return_value = TransformResult(
            shipment_current_doc={"shipment_id": "SHP-001"},
            rider_current_doc=None,
            event_doc={"event_id": "evt-001"},
        )

        pq = AsyncMock()
        client, _, idemp, _, ops_es = _build_app(adapter=adapter, poison_queue_service=pq)
        payload = _make_payload(schema_version="0.9")
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"
        adapter.transform.assert_called_once()
        pq.store_failed_event.assert_not_called()

    def test_three_part_semver_is_accepted(self):
        """A three-part semver (e.g. 1.0.1) is accepted."""
        client, adapter, idemp, pq, ops_es = _build_app()
        payload = _make_payload(schema_version="1.0.1")
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"


# ---------------------------------------------------------------------------
# Feature Flag Gating Tests — Validates: Req 27.2
# ---------------------------------------------------------------------------


class TestFeatureFlagGating:
    """Feature flag gating for disabled tenants. Validates: Req 27.2"""

    def test_disabled_tenant_returns_200_without_processing(self):
        """When feature flag is disabled, webhook returns 200 but skips all processing."""
        ff_service = AsyncMock()
        ff_service.is_enabled = AsyncMock(return_value=False)

        client, adapter, idemp, pq, ops_es = _build_app(feature_flag_service=ff_service)
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["event_id"] == "evt-001"
        assert data["status"] == "processed"
        # No downstream processing should occur
        adapter.transform.assert_not_called()
        ops_es.append_shipment_event.assert_not_called()
        ops_es.upsert_shipment_current.assert_not_called()
        idemp.mark_processed.assert_not_called()

    def test_enabled_tenant_is_processed_normally(self):
        """When feature flag is enabled, webhook is processed normally."""
        ff_service = AsyncMock()
        ff_service.is_enabled = AsyncMock(return_value=True)

        client, adapter, idemp, pq, ops_es = _build_app(feature_flag_service=ff_service)
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"
        ff_service.is_enabled.assert_called_once_with("tenant-1")
        adapter.transform.assert_called_once()

    def test_no_feature_flag_service_processes_normally(self):
        """When no feature flag service is configured, webhook is processed normally."""
        client, adapter, idemp, pq, ops_es = _build_app(feature_flag_service=None)
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"
        adapter.transform.assert_called_once()

    def test_feature_flag_check_failure_continues_processing(self):
        """If the feature flag check raises an exception, processing continues (fail-open)."""
        ff_service = AsyncMock()
        ff_service.is_enabled = AsyncMock(side_effect=Exception("Redis down"))

        client, adapter, idemp, pq, ops_es = _build_app(feature_flag_service=ff_service)
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"
        adapter.transform.assert_called_once()

    def test_feature_flag_checked_after_hmac_verification(self):
        """Feature flag is only checked after HMAC verification passes."""
        ff_service = AsyncMock()
        ff_service.is_enabled = AsyncMock(return_value=False)

        client, adapter, idemp, pq, ops_es = _build_app(feature_flag_service=ff_service)
        payload = _make_payload()
        body = json.dumps(payload)
        wrong_sig = hmac.new(
            b"wrong-secret", body.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        resp = client.post(
            "/webhooks/dinee",
            content=body,
            headers={
                "X-Dinee-Signature": wrong_sig,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 401
        ff_service.is_enabled.assert_not_called()
