"""
Unit tests for the POST /webhooks/dinee deprecation-window behavior (Task 7.2).

Tests cover:
- Deprecation headers emitted on every response (Deprecation, Sunset, Link)
- Legacy route hits counter incremented per request
- Pipeline routing when OrderIntakePipeline is wired
- Bootstrap seeding of the dinee-legacy channel on first hit
- Fallback to legacy path when pipeline is not wired
- Pipeline error handling (structured errors, unexpected errors)

Validates: Requirements 1.3.1, 1.3.3, 1.3.4, 2.2.8
"""

import hashlib
import hmac
import json
import sys
from dataclasses import dataclass
from typing import Optional
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
    DINEE_LEGACY_CHANNEL_ID,
    _dinee_legacy_seeded,
    configure_webhook_receiver,
    orders_legacy_route_hits_total,
    router,
)
from ops.ingestion.adapter import AdapterTransformer, TransformResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test-secret-key"
TEST_TENANT_ID = "tenant-1"
TEST_SUNSET_DATE = "2026-07-01"


@dataclass
class FakeIntakeResponse:
    """Mimics the IntakeResponse from OrderIntakePipeline."""
    event_id: str
    status: str
    order_id: Optional[str] = None


def _make_payload(
    event_id: str = "evt-001",
    tenant_id: str = TEST_TENANT_ID,
    event_type: str = "shipment_created",
    schema_version: str = "1.0",
    shipment_id: str = "SHP-001",
) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": schema_version,
        "tenant_id": tenant_id,
        "timestamp": "2025-01-01T12:00:00Z",
        "shipment_id": shipment_id,
        "data": {"shipment_id": shipment_id, "status": "created"},
    }


def _sign(payload: dict, secret: str = WEBHOOK_SECRET) -> str:
    body = json.dumps(payload).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _build_app_with_pipeline(
    *,
    pipeline_result: Optional[FakeIntakeResponse] = None,
    pipeline_side_effect: Optional[Exception] = None,
    sunset_date: Optional[str] = TEST_SUNSET_DATE,
    intake_channel_repo=None,
    credentials_vault=None,
):
    """Create a test FastAPI app with the pipeline wired for deprecation tests."""
    app = FastAPI()
    app.include_router(router)

    # Mock the legacy adapter (fallback path)
    adapter = MagicMock(spec=AdapterTransformer)
    adapter.is_version_supported.return_value = True
    adapter.transform.return_value = TransformResult(
        shipment_current_doc={"shipment_id": "SHP-001"},
        rider_current_doc=None,
        event_doc={"event_id": "evt-001"},
    )

    idempotency_service = AsyncMock()
    idempotency_service.is_duplicate = AsyncMock(return_value=False)
    idempotency_service.mark_processed = AsyncMock()

    poison_queue_service = AsyncMock()

    ops_es_service = AsyncMock()
    ops_es_service.append_shipment_event = AsyncMock()
    ops_es_service.upsert_shipment_current = AsyncMock()
    ops_es_service.upsert_rider_current = AsyncMock()
    ops_es_service._es = AsyncMock()
    ops_es_service._es.index_document = AsyncMock()

    # Mock the OrderIntakePipeline
    pipeline = AsyncMock()
    if pipeline_side_effect:
        pipeline.ingest_webhook = AsyncMock(side_effect=pipeline_side_effect)
    elif pipeline_result:
        pipeline.ingest_webhook = AsyncMock(return_value=pipeline_result)
    else:
        pipeline.ingest_webhook = AsyncMock(
            return_value=FakeIntakeResponse(
                event_id="evt-001",
                status="processed",
                order_id="ord_abc123",
            )
        )

    if intake_channel_repo is None:
        intake_channel_repo = AsyncMock()
        intake_channel_repo.get_by_channel_id = AsyncMock(return_value=None)

    if credentials_vault is None:
        credentials_vault = AsyncMock()
        credentials_vault.put = AsyncMock(return_value="vault-ref-123")

    # Patch settings to include sunset date
    mock_settings = MagicMock()
    mock_settings.ops_webhook_rate_limit = 500
    mock_settings.orders_legacy_sunset_date = sunset_date

    # Clear the seeded cache before each test
    _dinee_legacy_seeded.clear()

    configure_webhook_receiver(
        adapter=adapter,
        idempotency_service=idempotency_service,
        poison_queue_service=poison_queue_service,
        ops_es_service=ops_es_service,
        ws_manager=None,
        feature_flag_service=None,
        webhook_secret=WEBHOOK_SECRET,
        webhook_tenant_id="",
        order_intake_pipeline=pipeline,
        intake_channel_repo=intake_channel_repo,
        credentials_vault=credentials_vault,
    )

    client = TestClient(app)
    return client, pipeline, intake_channel_repo, credentials_vault, ops_es_service


def _build_app_without_pipeline():
    """Create a test FastAPI app WITHOUT the pipeline (legacy-only path)."""
    app = FastAPI()
    app.include_router(router)

    adapter = MagicMock(spec=AdapterTransformer)
    adapter.is_version_supported.return_value = True
    adapter.transform.return_value = TransformResult(
        shipment_current_doc={"shipment_id": "SHP-001"},
        rider_current_doc=None,
        event_doc={"event_id": "evt-001"},
    )

    idempotency_service = AsyncMock()
    idempotency_service.is_duplicate = AsyncMock(return_value=False)
    idempotency_service.mark_processed = AsyncMock()

    ops_es_service = AsyncMock()
    ops_es_service.append_shipment_event = AsyncMock()
    ops_es_service.upsert_shipment_current = AsyncMock()
    ops_es_service.upsert_rider_current = AsyncMock()

    # Clear the seeded cache
    _dinee_legacy_seeded.clear()

    configure_webhook_receiver(
        adapter=adapter,
        idempotency_service=idempotency_service,
        poison_queue_service=AsyncMock(),
        ops_es_service=ops_es_service,
        ws_manager=None,
        feature_flag_service=None,
        webhook_secret=WEBHOOK_SECRET,
        webhook_tenant_id="",
        order_intake_pipeline=None,
        intake_channel_repo=None,
        credentials_vault=None,
    )

    client = TestClient(app)
    return client, adapter


def _post_webhook(client, payload, secret=WEBHOOK_SECRET):
    """Helper to POST a signed webhook payload."""
    body = json.dumps(payload)
    sig = _sign(payload, secret)
    return client.post(
        "/webhooks/dinee",
        content=body,
        headers={
            "X-Dinee-Signature": sig,
            "Content-Type": "application/json",
        },
    )


# ---------------------------------------------------------------------------
# Deprecation Header Tests — Validates: Req 1.3.3
# ---------------------------------------------------------------------------


class TestDeprecationHeaders:
    """Deprecation headers emitted on every response. Validates: Req 1.3.3"""

    @patch("ops.webhooks.receiver.get_settings")
    def test_deprecation_header_present_on_success(self, mock_get_settings):
        """Every successful response includes Deprecation: true header."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        client, pipeline, *_ = _build_app_with_pipeline()
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "true"

    @patch("ops.webhooks.receiver.get_settings")
    def test_sunset_header_contains_configured_date(self, mock_get_settings):
        """Sunset header contains the orders_legacy_sunset_date from settings."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = "2026-09-15"
        mock_get_settings.return_value = mock_settings

        client, pipeline, *_ = _build_app_with_pipeline(sunset_date="2026-09-15")
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.headers.get("sunset") == "2026-09-15"

    @patch("ops.webhooks.receiver.get_settings")
    def test_link_header_points_to_successor_route(self, mock_get_settings):
        """Link header points to /webhooks/orders/{channel_id} with rel=successor-version."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        client, pipeline, *_ = _build_app_with_pipeline()
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        link_header = resp.headers.get("link")
        assert link_header is not None
        assert f"/webhooks/orders/{DINEE_LEGACY_CHANNEL_ID}" in link_header
        assert 'rel="successor-version"' in link_header

    @patch("ops.webhooks.receiver.get_settings")
    def test_deprecation_headers_on_legacy_fallback_path(self, mock_get_settings):
        """Deprecation headers are emitted even on the legacy fallback path."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        client, adapter = _build_app_without_pipeline()
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "true"
        assert resp.headers.get("sunset") == TEST_SUNSET_DATE

    @patch("ops.webhooks.receiver.get_settings")
    def test_no_sunset_header_when_date_not_configured(self, mock_get_settings):
        """When orders_legacy_sunset_date is None, Sunset header is omitted."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = None
        mock_get_settings.return_value = mock_settings

        client, pipeline, *_ = _build_app_with_pipeline(sunset_date=None)
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "true"
        # Sunset header should not be present when date is None
        assert resp.headers.get("sunset") is None


# ---------------------------------------------------------------------------
# Pipeline Routing Tests — Validates: Req 2.2.8
# ---------------------------------------------------------------------------


class TestPipelineRouting:
    """Pipeline routing during deprecation window. Validates: Req 2.2.8"""

    @patch("ops.webhooks.receiver.get_settings")
    def test_routes_through_pipeline_when_available(self, mock_get_settings):
        """When OrderIntakePipeline is wired, requests route through it."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        client, pipeline, *_ = _build_app_with_pipeline()
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "processed"
        assert data["order_id"] == "ord_abc123"

        # Verify pipeline was called with the correct channel_id
        pipeline.ingest_webhook.assert_called_once()
        call_kwargs = pipeline.ingest_webhook.call_args
        assert call_kwargs.kwargs.get("channel_id") == DINEE_LEGACY_CHANNEL_ID

    @patch("ops.webhooks.receiver.get_settings")
    def test_pipeline_receives_raw_body_and_signature(self, mock_get_settings):
        """Pipeline receives the raw body bytes and the HMAC signature."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        client, pipeline, *_ = _build_app_with_pipeline()
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        call_kwargs = pipeline.ingest_webhook.call_args.kwargs
        assert isinstance(call_kwargs["body"], bytes)
        assert call_kwargs["signature"] is not None
        assert call_kwargs["request_id"] is not None

    @patch("ops.webhooks.receiver.get_settings")
    def test_duplicate_returns_200_without_order_id(self, mock_get_settings):
        """A duplicate event returns 200 with status 'duplicate' and no order_id."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        result = FakeIntakeResponse(
            event_id="evt-001", status="duplicate", order_id=None
        )
        client, pipeline, *_ = _build_app_with_pipeline(pipeline_result=result)
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "duplicate"
        assert "order_id" not in data

    @patch("ops.webhooks.receiver.get_settings")
    def test_falls_back_to_legacy_when_pipeline_not_wired(self, mock_get_settings):
        """When pipeline is None, falls back to legacy processing path."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        client, adapter = _build_app_without_pipeline()
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "processed"
        # Legacy adapter should have been called
        adapter.transform.assert_called_once()


# ---------------------------------------------------------------------------
# Pipeline Error Handling Tests — Validates: Req 1.3.1, 2.2.8
# ---------------------------------------------------------------------------


class TestPipelineErrorHandling:
    """Pipeline error handling during deprecation window. Validates: Req 1.3.1"""

    @patch("ops.webhooks.receiver.get_settings")
    def test_structured_error_returns_with_deprecation_headers(
        self, mock_get_settings
    ):
        """Structured pipeline errors return proper status with deprecation headers."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        # Create a structured error with error_code and status_code
        error = Exception("Channel disabled")
        error.error_code = "CHANNEL_DISABLED"
        error.status_code = 403

        client, pipeline, *_ = _build_app_with_pipeline(
            pipeline_side_effect=error
        )
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 403
        data = resp.json()
        assert data["error_code"] == "CHANNEL_DISABLED"
        # Deprecation headers should still be present
        assert resp.headers.get("deprecation") == "true"

    @patch("ops.webhooks.receiver.get_settings")
    def test_unexpected_error_falls_back_to_legacy_path(self, mock_get_settings):
        """Unexpected pipeline errors fall back to the legacy processing path."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        # An error without error_code/status_code attributes
        error = RuntimeError("Unexpected internal error")

        client, pipeline, *_ = _build_app_with_pipeline(
            pipeline_side_effect=error
        )
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        # Should fall through to legacy path and succeed
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "processed"


# ---------------------------------------------------------------------------
# Channel Seeding Tests — Validates: Req 2.2.8
# ---------------------------------------------------------------------------


class TestDineeLegacyChannelSeeding:
    """Bootstrap seeding of dinee-legacy channel. Validates: Req 2.2.8"""

    @patch("ops.webhooks.receiver.get_settings")
    def test_seeds_channel_on_first_hit(self, mock_get_settings):
        """The dinee-legacy channel is seeded on the first request for a tenant."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        intake_channel_repo = AsyncMock()
        intake_channel_repo.get_by_channel_id = AsyncMock(return_value=None)

        credentials_vault = AsyncMock()
        credentials_vault.put = AsyncMock(return_value="vault-ref-dinee")

        client, pipeline, _, _, ops_es = _build_app_with_pipeline(
            intake_channel_repo=intake_channel_repo,
            credentials_vault=credentials_vault,
        )
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        # Verify the vault was called to store the HMAC secret
        credentials_vault.put.assert_called_once()
        put_kwargs = credentials_vault.put.call_args.kwargs
        assert put_kwargs["tenant_id"] == TEST_TENANT_ID
        assert "dinee-legacy" in put_kwargs["key"]

    @patch("ops.webhooks.receiver.get_settings")
    def test_skips_seeding_when_channel_already_exists(self, mock_get_settings):
        """If the dinee-legacy channel already exists, seeding is skipped."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        # Simulate channel already existing
        existing_channel = MagicMock()
        existing_channel.channel_id = DINEE_LEGACY_CHANNEL_ID
        existing_channel.tenant_id = TEST_TENANT_ID

        intake_channel_repo = AsyncMock()
        intake_channel_repo.get_by_channel_id = AsyncMock(
            return_value=existing_channel
        )

        credentials_vault = AsyncMock()
        credentials_vault.put = AsyncMock(return_value="vault-ref-dinee")

        client, pipeline, _, _, ops_es = _build_app_with_pipeline(
            intake_channel_repo=intake_channel_repo,
            credentials_vault=credentials_vault,
        )
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200
        # Vault should NOT be called since channel already exists
        credentials_vault.put.assert_not_called()

    @patch("ops.webhooks.receiver.get_settings")
    def test_seeding_failure_does_not_block_request(self, mock_get_settings):
        """If channel seeding fails, the request still proceeds."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        intake_channel_repo = AsyncMock()
        intake_channel_repo.get_by_channel_id = AsyncMock(return_value=None)

        credentials_vault = AsyncMock()
        credentials_vault.put = AsyncMock(
            side_effect=RuntimeError("Vault unavailable")
        )

        client, pipeline, _, _, ops_es = _build_app_with_pipeline(
            intake_channel_repo=intake_channel_repo,
            credentials_vault=credentials_vault,
        )
        payload = _make_payload()
        resp = _post_webhook(client, payload)

        # Request should still succeed (pipeline is called regardless)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Legacy Route Hits Counter Tests — Validates: Req 1.3.4
# ---------------------------------------------------------------------------


class TestLegacyRouteHitsCounter:
    """Prometheus counter for legacy route hits. Validates: Req 1.3.4"""

    @patch("ops.webhooks.receiver.get_settings")
    def test_counter_incremented_on_every_request(self, mock_get_settings):
        """orders_legacy_route_hits_total is incremented on every request."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        client, pipeline, *_ = _build_app_with_pipeline()

        # Get the counter value before the request
        before = orders_legacy_route_hits_total.labels(
            route="/webhooks/dinee", tenant_id=TEST_TENANT_ID
        )._value.get()

        payload = _make_payload()
        resp = _post_webhook(client, payload)

        assert resp.status_code == 200

        # Counter should have incremented
        after = orders_legacy_route_hits_total.labels(
            route="/webhooks/dinee", tenant_id=TEST_TENANT_ID
        )._value.get()
        assert after == before + 1

    @patch("ops.webhooks.receiver.get_settings")
    def test_counter_labels_include_route_and_tenant(self, mock_get_settings):
        """Counter labels include route='/webhooks/dinee' and the tenant_id."""
        mock_settings = MagicMock()
        mock_settings.ops_webhook_rate_limit = 500
        mock_settings.orders_legacy_sunset_date = TEST_SUNSET_DATE
        mock_get_settings.return_value = mock_settings

        client, pipeline, *_ = _build_app_with_pipeline()

        # Use a different tenant_id to verify labeling
        payload = _make_payload(tenant_id="tenant-xyz")

        before = orders_legacy_route_hits_total.labels(
            route="/webhooks/dinee", tenant_id="tenant-xyz"
        )._value.get()

        resp = _post_webhook(client, payload)
        assert resp.status_code == 200

        after = orders_legacy_route_hits_total.labels(
            route="/webhooks/dinee", tenant_id="tenant-xyz"
        )._value.get()
        assert after == before + 1


# ---------------------------------------------------------------------------
# DINEE_LEGACY_CHANNEL_ID constant test
# ---------------------------------------------------------------------------


class TestDineeLegacyChannelId:
    """Verify the reserved channel_id constant. Validates: Req 2.2.8"""

    def test_channel_id_is_dinee_legacy(self):
        """The reserved channel_id for the legacy route is 'dinee-legacy'."""
        assert DINEE_LEGACY_CHANNEL_ID == "dinee-legacy"
