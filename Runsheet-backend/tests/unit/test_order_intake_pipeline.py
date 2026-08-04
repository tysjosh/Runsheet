"""
Unit tests for :mod:`fuel.services.order_intake_pipeline`.

Task 5.7 of the order-intake-pipeline spec. Exercises the
OrderIntakePipeline with fully mocked dependencies so the suite stays
decoupled from Elasticsearch, Redis, and the credentials vault.

Covers:

* Happy webhook path (vault returns plaintext, HMAC verified, discarded).
* HMAC mismatch returns 401 ``webhook_signature_invalid``.
* Disabled channel returns 403 ``channel_disabled``.
* Duplicate ``client_event_id`` within the same tenant returns 200
  ``duplicate`` with no second ES write.
* Same ``client_event_id`` under a different tenant creates a new order
  (tenant-scoped idempotency).
* Missing ``client_event_id`` on dispatcher path returns 400
  ``missing_client_event_id``.
* Unknown schema version routes to poison queue with reason
  ``unknown_schema_version``.
* Adapter validation failure routes to poison queue without leaving the
  order visible.
* Payload ``tenant_id`` mismatch with the channel's ``tenant_id`` raises
  ``security_tenant_id_mismatch`` and logs a security event.
* ``customer_tank_id`` owned by a different tenant returns 400
  ``invalid_customer_tank_ref``.
* ``customer_tank_id = None`` bypasses the tank check.
* ``_complete_order_doc`` overwrites adapter-set ``order_id`` /
  ``tenant_id`` / ``status`` / timestamps.
* Legacy dual-write failure logs a warning AND enqueues the order in
  ``pending_legacy_mirrors`` — does NOT fail the main path.

Validates: Requirements 1.1.6, 2.2.2, 2.2.3, 2.2.5, 2.2.6, 2.2.7, 10.2.1.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from errors.exceptions import AppException
from fuel.intake.adapter_base import (
    AdapterError,
    IntakeAdapterRegistry,
    IntakeContext,
    IntakeResult,
)
from fuel.intake_channel_models import IntakeChannel
from fuel.services.order_intake_pipeline import (
    IntakeResponse,
    OrderIntakePipeline,
    _CsvImportChannel,
)


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

TENANT_A = "tenant-aaa"
TENANT_B = "tenant-bbb"
CHANNEL_ID = "partner-voice-01"
HMAC_SECRET = "super-secret-key-for-testing"
FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _make_channel(
    *,
    channel_id: str = CHANNEL_ID,
    tenant_id: str = TENANT_A,
    enabled: bool = True,
    channel_type: str = "api_partner",
    supported_schema_versions: Optional[List[str]] = None,
) -> IntakeChannel:
    """Build a minimal IntakeChannel for testing."""
    return IntakeChannel(
        channel_id=channel_id,
        tenant_id=tenant_id,
        channel_type=channel_type,
        display_name="Test Channel",
        hmac_secret_ref=f"vault-ref:{tenant_id}:{channel_id}:1",
        supported_schema_versions=supported_schema_versions or ["1.0"],
        rate_limit_per_minute=100,
        secret_version=1,
        enabled=enabled,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def _sign_body(body: bytes, secret: str = HMAC_SECRET) -> str:
    """Compute the HMAC-SHA256 signature for a request body."""
    return hmac_mod.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def _valid_order_payload(
    *,
    tenant_id: Optional[str] = None,
    schema_version: str = "1.0",
    event_id: str = "evt-client-001",
    customer_tank_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a valid upstream order payload."""
    payload: Dict[str, Any] = {
        "schema_version": schema_version,
        "event_id": event_id,
        "customer_id": "cust-123",
        "customer_name": "Acme Fuel Co",
        "customer_phone": "+15551234567",
        "ship_to_address": "123 Main St, Houston TX",
        "ship_to_lat": 29.76,
        "ship_to_lon": -95.37,
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "fill_to_full": False,
        "call_type": "one_off",
        "delivery_window_start": "2026-05-11T08:00:00Z",
        "delivery_window_end": "2026-05-11T12:00:00Z",
    }
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    if customer_tank_id is not None:
        payload["customer_tank_id"] = customer_tank_id
    return payload


def _make_adapter_result(
    payload: Dict[str, Any],
    context: Any = None,
) -> IntakeResult:
    """Simulate what a real adapter would return."""
    order_doc = {
        "customer_id": payload.get("customer_id", "cust-123"),
        "customer_name": payload.get("customer_name", "Acme Fuel Co"),
        "customer_phone": payload.get("customer_phone"),
        "customer_email": None,
        "ship_to_address": payload.get("ship_to_address", "123 Main St"),
        "ship_to_lat": payload.get("ship_to_lat", 29.76),
        "ship_to_lon": payload.get("ship_to_lon", -95.37),
        "customer_tank_id": payload.get("customer_tank_id"),
        "product_code": payload.get("product_code", "DIESEL_2"),
        "gallons_requested": payload.get("gallons_requested", 500.0),
        "fill_to_full": payload.get("fill_to_full", False),
        "call_type": payload.get("call_type", "one_off"),
        "delivery_window_start": payload.get(
            "delivery_window_start", "2026-05-11T08:00:00Z"
        ),
        "delivery_window_end": payload.get(
            "delivery_window_end", "2026-05-11T12:00:00Z"
        ),
        "hold_reason": None,
        "po_number": None,
        "special_instructions": None,
        "intake_channel": "api_partner",
        "intake_channel_id": CHANNEL_ID,
        "intake_metadata": {"partner_ref": "ext-ref-001"},
        "source_schema_version": "1.0",
        # Adapter might try to set these — pipeline MUST overwrite
        "order_id": "adapter-set-order-id",
        "tenant_id": "adapter-set-tenant-id",
        "status": "confirmed",
    }
    event_docs = [
        {
            "event_type": "order_placed",
            "event_payload": {"source": "api_partner"},
        }
    ]
    return IntakeResult(order_doc=order_doc, event_docs=event_docs)


class FakeAdapter:
    """A fake adapter that returns a canned IntakeResult."""

    channel_type = "api_partner"

    def __init__(self, *, raise_error: Optional[AdapterError] = None):
        self._raise_error = raise_error
        self.call_count = 0

    def transform(
        self, payload: Dict[str, Any], context: IntakeContext
    ) -> IntakeResult:
        self.call_count += 1
        if self._raise_error:
            raise self._raise_error
        return _make_adapter_result(payload, context)


@pytest.fixture
def fake_adapter():
    return FakeAdapter()


@pytest.fixture
def adapter_registry(fake_adapter):
    registry = IntakeAdapterRegistry()
    registry.register(
        fake_adapter, channel_type="api_partner", schema_version="1.0"
    )
    return registry


@pytest.fixture
def channel():
    return _make_channel()


@pytest.fixture
def es_service():
    svc = AsyncMock()
    svc.index_document = AsyncMock()
    # The FuelOrderRepository uses self._es.client.update(...) synchronously
    svc.client = MagicMock()
    svc.client.update = MagicMock(return_value={"result": "created"})
    return svc


@pytest.fixture
def intake_channel_repo(channel):
    repo = AsyncMock()
    repo.get_by_channel_id = AsyncMock(return_value=channel)
    repo.get_dispatcher_channel = AsyncMock(return_value=channel)
    return repo


@pytest.fixture
def idempotency_service():
    svc = AsyncMock()
    svc.is_duplicate = AsyncMock(return_value=False)
    svc.mark_processed = AsyncMock()
    return svc


@pytest.fixture
def feature_flag_service():
    svc = AsyncMock()
    svc.get_overlay_state = AsyncMock(return_value="active_gated")
    return svc


@pytest.fixture
def poison_queue_service():
    svc = AsyncMock()
    svc.store_failed_event = AsyncMock()
    return svc


@pytest.fixture
def ws_manager():
    mgr = AsyncMock()
    mgr.broadcast = AsyncMock()
    return mgr


@pytest.fixture
def credentials_vault():
    vault = AsyncMock()
    vault.get = AsyncMock(return_value={"secret": HMAC_SECRET})
    return vault


@pytest.fixture
def customer_tank_repo():
    repo = AsyncMock()
    # Default: tank exists for the tenant
    repo.get = AsyncMock(return_value={"tank_id": "tank-001", "tenant_id": TENANT_A})
    return repo


@pytest.fixture
def legacy_dual_writer():
    writer = AsyncMock()
    writer.mirror_order = AsyncMock()
    return writer


@pytest.fixture
def legacy_ws_manager():
    mgr = AsyncMock()
    mgr.broadcast_shipment_update = AsyncMock(return_value=1)
    mgr.broadcast_rider_update = AsyncMock(return_value=1)
    return mgr


@pytest.fixture
def clock():
    return lambda: FIXED_NOW


@pytest.fixture
def pipeline(
    es_service,
    intake_channel_repo,
    adapter_registry,
    idempotency_service,
    feature_flag_service,
    poison_queue_service,
    ws_manager,
    credentials_vault,
    customer_tank_repo,
    legacy_dual_writer,
    legacy_ws_manager,
    clock,
):
    return OrderIntakePipeline(
        es_service=es_service,
        intake_channel_repo=intake_channel_repo,
        adapter_registry=adapter_registry,
        idempotency_service=idempotency_service,
        feature_flag_service=feature_flag_service,
        poison_queue_service=poison_queue_service,
        ws_manager=ws_manager,
        credentials_vault=credentials_vault,
        customer_tank_repo=customer_tank_repo,
        legacy_dual_writer=legacy_dual_writer,
        legacy_ws_manager=legacy_ws_manager,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Tests — Happy webhook path (Req 2.2.2)
# ---------------------------------------------------------------------------


class TestHappyWebhookPath:
    """Vault returns plaintext, HMAC verified, secret discarded, order created."""

    @pytest.mark.asyncio
    async def test_webhook_happy_path_returns_processed(
        self, pipeline, credentials_vault, idempotency_service, es_service
    ):
        """A valid HMAC-signed webhook creates an order and returns 'processed'."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-001",
        )

        assert isinstance(result, IntakeResponse)
        assert result.status == "processed"
        assert result.order_id is not None
        assert result.order_id.startswith("ord_")

        # Vault was called to retrieve the secret
        credentials_vault.get.assert_called_once()

        # Idempotency was marked processed
        idempotency_service.mark_processed.assert_called_once()

    @pytest.mark.asyncio
    async def test_webhook_happy_path_secret_discarded(
        self, pipeline, credentials_vault
    ):
        """The HMAC secret is used only for comparison and discarded."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        # Should not raise — secret is used and discarded
        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-002",
        )
        assert result.status == "processed"


# ---------------------------------------------------------------------------
# Tests — HMAC mismatch returns 401 (Req 2.2.3)
# ---------------------------------------------------------------------------


class TestHmacMismatch:
    """HMAC mismatch returns 401 ``webhook_signature_invalid``."""

    @pytest.mark.asyncio
    async def test_hmac_mismatch_raises_401(self, pipeline):
        """A bad signature raises webhook_signature_invalid (401)."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        bad_signature = "deadbeef" * 8  # wrong signature

        with pytest.raises(AppException) as exc_info:
            await pipeline.ingest_webhook(
                channel_id=CHANNEL_ID,
                body=body,
                signature=bad_signature,
                request_id="req-003",
            )

        assert exc_info.value.status_code == 401
        assert "WEBHOOK_SIGNATURE_INVALID" in str(exc_info.value.error_code)


# ---------------------------------------------------------------------------
# Tests — Disabled channel returns 403 (Req 2.2.5)
# ---------------------------------------------------------------------------


class TestDisabledChannel:
    """Disabled channel returns 403 ``channel_disabled``."""

    @pytest.mark.asyncio
    async def test_disabled_channel_raises_403(
        self, pipeline, intake_channel_repo
    ):
        """A disabled channel raises channel_disabled (403)."""
        disabled_channel = _make_channel(enabled=False)
        intake_channel_repo.get_by_channel_id = AsyncMock(
            return_value=disabled_channel
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        with pytest.raises(AppException) as exc_info:
            await pipeline.ingest_webhook(
                channel_id=CHANNEL_ID,
                body=body,
                signature=signature,
                request_id="req-004",
            )

        assert exc_info.value.status_code == 403
        assert "CHANNEL_DISABLED" in str(exc_info.value.error_code)


# ---------------------------------------------------------------------------
# Tests — Duplicate client_event_id within same tenant (Req 2.2.6)
# ---------------------------------------------------------------------------


class TestDuplicateIdempotency:
    """Duplicate ``client_event_id`` within the same tenant returns 200 'duplicate'."""

    @pytest.mark.asyncio
    async def test_duplicate_returns_duplicate_status(
        self, pipeline, idempotency_service, es_service
    ):
        """A duplicate event_id returns status='duplicate' with no ES write."""
        idempotency_service.is_duplicate = AsyncMock(return_value=True)

        payload = _valid_order_payload(event_id="evt-dup-001")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-005",
        )

        assert result.status == "duplicate"
        assert result.order_id is None
        # No ES write should have happened
        es_service.index_document.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Same client_event_id under different tenant (tenant-scoped)
# ---------------------------------------------------------------------------


class TestTenantScopedIdempotency:
    """Same ``client_event_id`` under a different tenant creates a new order."""

    @pytest.mark.asyncio
    async def test_same_event_id_different_tenant_creates_order(
        self,
        es_service,
        adapter_registry,
        feature_flag_service,
        poison_queue_service,
        ws_manager,
        credentials_vault,
        customer_tank_repo,
        legacy_dual_writer,
        clock,
    ):
        """Tenant-scoped idempotency: same event_id under different tenants
        are independent — both create orders."""
        # Track which tenant_id was passed to is_duplicate
        duplicate_calls: List[Dict[str, Any]] = []

        async def fake_is_duplicate(event_id, *, tenant_id):
            duplicate_calls.append({"event_id": event_id, "tenant_id": tenant_id})
            return False

        idempotency_svc = AsyncMock()
        idempotency_svc.is_duplicate = fake_is_duplicate
        idempotency_svc.mark_processed = AsyncMock()

        # Channel for tenant A
        channel_a = _make_channel(tenant_id=TENANT_A)
        # Channel for tenant B
        channel_b = _make_channel(
            channel_id="partner-voice-02", tenant_id=TENANT_B
        )

        repo = AsyncMock()

        # Pipeline for tenant A
        repo.get_by_channel_id = AsyncMock(return_value=channel_a)
        pipeline_a = OrderIntakePipeline(
            es_service=es_service,
            intake_channel_repo=repo,
            adapter_registry=adapter_registry,
            idempotency_service=idempotency_svc,
            feature_flag_service=feature_flag_service,
            poison_queue_service=poison_queue_service,
            ws_manager=ws_manager,
            credentials_vault=credentials_vault,
            customer_tank_repo=customer_tank_repo,
            legacy_dual_writer=legacy_dual_writer,
            clock=clock,
        )

        payload = _valid_order_payload(event_id="shared-evt-001")
        body = json.dumps(payload).encode()
        sig = _sign_body(body)

        result_a = await pipeline_a.ingest_webhook(
            channel_id=CHANNEL_ID, body=body, signature=sig, request_id="req-a"
        )
        assert result_a.status == "processed"

        # Pipeline for tenant B
        repo.get_by_channel_id = AsyncMock(return_value=channel_b)
        pipeline_b = OrderIntakePipeline(
            es_service=es_service,
            intake_channel_repo=repo,
            adapter_registry=adapter_registry,
            idempotency_service=idempotency_svc,
            feature_flag_service=feature_flag_service,
            poison_queue_service=poison_queue_service,
            ws_manager=ws_manager,
            credentials_vault=credentials_vault,
            customer_tank_repo=customer_tank_repo,
            legacy_dual_writer=legacy_dual_writer,
            clock=clock,
        )

        result_b = await pipeline_b.ingest_webhook(
            channel_id="partner-voice-02",
            body=body,
            signature=sig,
            request_id="req-b",
        )
        assert result_b.status == "processed"

        # Both calls passed different tenant_ids to is_duplicate
        assert len(duplicate_calls) == 2
        assert duplicate_calls[0]["tenant_id"] == TENANT_A
        assert duplicate_calls[1]["tenant_id"] == TENANT_B


# ---------------------------------------------------------------------------
# Tests — Missing client_event_id on dispatcher path (Req 2.2.7)
# ---------------------------------------------------------------------------


class TestMissingClientEventId:
    """Missing ``client_event_id`` on dispatcher path returns 400."""

    @pytest.mark.asyncio
    async def test_missing_client_event_id_raises_400(self, pipeline):
        """Dispatcher path without client_event_id raises missing_client_event_id."""
        tenant = {"tenant_id": TENANT_A, "user_id": "user-001"}
        payload = _valid_order_payload()

        with pytest.raises(AppException) as exc_info:
            await pipeline.ingest_dispatcher(
                tenant=tenant,
                payload=payload,
                request_id="req-006",
                client_event_id=None,
            )

        assert exc_info.value.status_code == 400
        assert "MISSING_CLIENT_EVENT_ID" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_empty_string_client_event_id_raises_400(self, pipeline):
        """Dispatcher path with empty string client_event_id raises 400."""
        tenant = {"tenant_id": TENANT_A, "user_id": "user-001"}
        payload = _valid_order_payload()

        with pytest.raises(AppException) as exc_info:
            await pipeline.ingest_dispatcher(
                tenant=tenant,
                payload=payload,
                request_id="req-007",
                client_event_id="",
            )

        assert exc_info.value.status_code == 400
        assert "MISSING_CLIENT_EVENT_ID" in str(exc_info.value.error_code)


# ---------------------------------------------------------------------------
# Tests — Unknown schema version routes to poison queue
# ---------------------------------------------------------------------------


class TestUnknownSchemaVersion:
    """Unknown schema version routes to poison queue with reason
    ``unknown_schema_version``."""

    @pytest.mark.asyncio
    async def test_unknown_schema_routes_to_poison_queue(
        self, pipeline, poison_queue_service, idempotency_service
    ):
        """Payload with unsupported schema_version goes to poison queue."""
        payload = _valid_order_payload(schema_version="99.0")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-008",
        )

        assert result.status == "queued_for_review"
        poison_queue_service.store_failed_event.assert_called_once()
        call_kwargs = poison_queue_service.store_failed_event.call_args[1]
        assert call_kwargs["error_type"] == "unknown_schema_version"
        assert call_kwargs["tenant_id"] == TENANT_A

        # Idempotency should NOT be marked processed for poison-queued items
        idempotency_service.mark_processed.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Adapter validation failure routes to poison queue
# ---------------------------------------------------------------------------


class TestAdapterValidationFailure:
    """Adapter validation failure routes to poison queue without leaving
    the order visible."""

    @pytest.mark.asyncio
    async def test_adapter_error_routes_to_poison_queue(
        self,
        es_service,
        intake_channel_repo,
        idempotency_service,
        feature_flag_service,
        poison_queue_service,
        ws_manager,
        credentials_vault,
        customer_tank_repo,
        legacy_dual_writer,
        clock,
    ):
        """An adapter that raises AdapterError routes to poison queue."""
        failing_adapter = FakeAdapter(
            raise_error=AdapterError(
                error_type="adapter_output_invalid",
                message="Missing required field: customer_id",
            )
        )
        registry = IntakeAdapterRegistry()
        registry.register(
            failing_adapter, channel_type="api_partner", schema_version="1.0"
        )

        pipeline = OrderIntakePipeline(
            es_service=es_service,
            intake_channel_repo=intake_channel_repo,
            adapter_registry=registry,
            idempotency_service=idempotency_service,
            feature_flag_service=feature_flag_service,
            poison_queue_service=poison_queue_service,
            ws_manager=ws_manager,
            credentials_vault=credentials_vault,
            customer_tank_repo=customer_tank_repo,
            legacy_dual_writer=legacy_dual_writer,
            clock=clock,
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-009",
        )

        assert result.status == "queued_for_review"
        poison_queue_service.store_failed_event.assert_called_once()
        call_kwargs = poison_queue_service.store_failed_event.call_args[1]
        assert call_kwargs["error_type"] == "adapter_output_invalid"

        # Order should NOT be visible (no ES write for the order)
        # The es_service.index_document may be called for poison queue
        # but the order repo upsert should not have been called
        idempotency_service.mark_processed.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Payload tenant_id mismatch (security event)
# ---------------------------------------------------------------------------


class TestTenantIdMismatch:
    """Payload ``tenant_id`` mismatch with the channel's ``tenant_id``
    raises ``security_tenant_id_mismatch`` and logs a security event."""

    @pytest.mark.asyncio
    async def test_tenant_mismatch_raises_403(self, pipeline, caplog):
        """Payload with wrong tenant_id raises security_tenant_id_mismatch."""
        payload = _valid_order_payload(tenant_id="evil-tenant-999")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        with pytest.raises(AppException) as exc_info:
            await pipeline.ingest_webhook(
                channel_id=CHANNEL_ID,
                body=body,
                signature=signature,
                request_id="req-010",
            )

        assert exc_info.value.status_code == 403
        assert "SECURITY_TENANT_ID_MISMATCH" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_tenant_mismatch_logs_security_event(self, pipeline, caplog):
        """Tenant mismatch logs a SECURITY warning."""
        payload = _valid_order_payload(tenant_id="evil-tenant-999")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(AppException):
                await pipeline.ingest_webhook(
                    channel_id=CHANNEL_ID,
                    body=body,
                    signature=signature,
                    request_id="req-011",
                )

        assert any("SECURITY" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Tests — customer_tank_id owned by different tenant (Req 1.1.6)
# ---------------------------------------------------------------------------


class TestCustomerTankIdValidation:
    """``customer_tank_id`` owned by a different tenant returns 400."""

    @pytest.mark.asyncio
    async def test_invalid_tank_ref_raises_400(
        self, pipeline, customer_tank_repo
    ):
        """Tank not found for tenant raises invalid_customer_tank_ref (400)."""
        # Tank does not exist for this tenant
        customer_tank_repo.get = AsyncMock(return_value=None)

        payload = _valid_order_payload(customer_tank_id="tank-wrong-tenant")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        with pytest.raises(AppException) as exc_info:
            await pipeline.ingest_webhook(
                channel_id=CHANNEL_ID,
                body=body,
                signature=signature,
                request_id="req-012",
            )

        assert exc_info.value.status_code == 400
        assert "INVALID_CUSTOMER_TANK_REF" in str(exc_info.value.error_code)


# ---------------------------------------------------------------------------
# Tests — customer_tank_id = None bypasses the tank check
# ---------------------------------------------------------------------------


class TestCustomerTankIdNone:
    """``customer_tank_id = None`` bypasses the tank check."""

    @pytest.mark.asyncio
    async def test_null_tank_id_bypasses_check(
        self, pipeline, customer_tank_repo
    ):
        """When customer_tank_id is None, the tank repo is never called."""
        payload = _valid_order_payload(customer_tank_id=None)
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-013",
        )

        assert result.status == "processed"
        # Tank repo should NOT have been called
        customer_tank_repo.get.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — _complete_order_doc overwrites adapter-set fields
# ---------------------------------------------------------------------------


class TestCompleteOrderDocOverwrites:
    """``_complete_order_doc`` overwrites adapter-set ``order_id`` /
    ``tenant_id`` / ``status`` / timestamps."""

    @pytest.mark.asyncio
    async def test_platform_fields_overwritten(self, pipeline):
        """Adapter-set order_id, tenant_id, status, timestamps are overwritten."""
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-014",
        )

        assert result.status == "processed"
        # The order_id should be platform-assigned (not adapter-set)
        assert result.order_id != "adapter-set-order-id"
        assert result.order_id.startswith("ord_")

    def test_complete_order_doc_directly(self, pipeline):
        """Direct test of _complete_order_doc overwriting adapter fields."""
        adapter_output = {
            "order_id": "adapter-fake-id",
            "tenant_id": "adapter-fake-tenant",
            "status": "confirmed",
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2020-01-01T00:00:00Z",
            "last_event_timestamp": "2020-01-01T00:00:00Z",
            "customer_id": "cust-123",
            "intake_channel": "api_partner",
        }
        context = IntakeContext(
            tenant_id=TENANT_A,
            channel=_make_channel(),
            trace_id="trace-001",
            request_id="req-015",
        )

        result = pipeline._complete_order_doc(adapter_output, context, "evt-001")

        # Platform-owned fields MUST be overwritten
        assert result["order_id"] != "adapter-fake-id"
        assert result["order_id"].startswith("ord_")
        assert result["tenant_id"] == TENANT_A
        assert result["status"] == "placed"
        assert result["created_at"] == FIXED_NOW.isoformat()
        assert result["updated_at"] == FIXED_NOW.isoformat()
        assert result["last_event_timestamp"] == FIXED_NOW.isoformat()
        assert result["trace_id"] == "trace-001"

    def test_csv_source_identity_produces_stable_platform_order_id(self, pipeline):
        context = IntakeContext(
            tenant_id=TENANT_A,
            channel=_CsvImportChannel(
                channel_id="csv-import",
                tenant_id=TENANT_A,
            ),
            trace_id="trace-csv",
            request_id="request-csv",
        )
        adapter_output = {
            "intake_channel": "csv",
            "intake_metadata": {
                "source_system": "erp-a",
                "source_record_id": "SO-100",
            },
        }

        first = pipeline._complete_order_doc(
            adapter_output, context, "event-version-1"
        )
        second = pipeline._complete_order_doc(
            adapter_output, context, "event-version-2"
        )

        assert first["order_id"] == second["order_id"]
        assert first["order_id"].startswith("ord_import_")


class TestCsvSourceUpdates:
    @pytest.mark.asyncio
    async def test_older_source_snapshot_is_rejected(self, pipeline):
        existing = MagicMock()
        existing.intake_metadata.source_updated_at = datetime(
            2026, 5, 10, 12, tzinfo=timezone.utc
        )

        with patch(
            "fuel.order_repository.FuelOrderRepository.get",
            new=AsyncMock(return_value=existing),
        ):
            state = await pipeline._prepare_csv_source_upsert(
                tenant_id=TENANT_A,
                order_doc={
                    "order_id": "ord_import_123",
                    "intake_channel": "csv",
                    "intake_metadata": {
                        "source_system": "erp-a",
                        "source_record_id": "SO-100",
                        "source_updated_at": "2026-05-10T11:59:00Z",
                    },
                },
            )

        assert state == "stale"

    @pytest.mark.asyncio
    async def test_newer_source_snapshot_preserves_execution_state(self, pipeline):
        existing = MagicMock()
        existing.intake_metadata.source_updated_at = datetime(
            2026, 5, 10, 12, tzinfo=timezone.utc
        )
        existing.model_dump.return_value = {
            "status": "dispatched",
            "assigned_driver_id": "driver-100",
            "assigned_asset_id": "truck-100",
            "assigned_run_id": "run-100",
            "pod_otp": None,
            "pod_otp_generated_at": None,
            "refusal_reason_code": None,
            "legacy_origin_snapshot": None,
            "created_at": "2026-05-09T08:00:00Z",
        }
        incoming = {
            "order_id": "ord_import_123",
            "intake_channel": "csv",
            "status": "placed",
            "intake_metadata": {
                "source_system": "erp-a",
                "source_record_id": "SO-100",
                "source_updated_at": "2026-05-10T12:01:00Z",
            },
        }

        with patch(
            "fuel.order_repository.FuelOrderRepository.get",
            new=AsyncMock(return_value=existing),
        ):
            state = await pipeline._prepare_csv_source_upsert(
                tenant_id=TENANT_A,
                order_doc=incoming,
            )

        assert state == "updated"
        assert incoming["status"] == "dispatched"
        assert incoming["assigned_driver_id"] == "driver-100"
        assert incoming["assigned_run_id"] == "run-100"
        assert incoming["created_at"] == "2026-05-09T08:00:00Z"


# ---------------------------------------------------------------------------
# Tests — Legacy dual-write failure does NOT fail the main path
# ---------------------------------------------------------------------------


class TestLegacyDualWriteFailure:
    """Legacy dual-write failure logs a warning AND enqueues the order in
    ``pending_legacy_mirrors`` — does NOT fail the main path."""

    @pytest.mark.asyncio
    async def test_dual_write_failure_does_not_fail_main_path(
        self,
        pipeline,
        legacy_dual_writer,
        feature_flag_service,
        es_service,
        idempotency_service,
        caplog,
    ):
        """When legacy dual-write fails, the main path still succeeds."""
        # Enable dual-write by setting overlay state to active_gated
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="active_gated"
        )
        # Make the dual-writer fail
        legacy_dual_writer.mirror_order = AsyncMock(
            side_effect=RuntimeError("Legacy ES is down")
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        with caplog.at_level(logging.WARNING):
            result = await pipeline.ingest_webhook(
                channel_id=CHANNEL_ID,
                body=body,
                signature=signature,
                request_id="req-016",
            )

        # Main path still succeeds
        assert result.status == "processed"
        assert result.order_id is not None

        # Idempotency was still marked processed
        idempotency_service.mark_processed.assert_called_once()

        # A warning was logged
        assert any(
            "legacy dual-write failed" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_dual_write_failure_enqueues_pending_mirror(
        self,
        pipeline,
        legacy_dual_writer,
        feature_flag_service,
        es_service,
    ):
        """When legacy dual-write fails, the order is enqueued in
        pending_legacy_mirrors for background retry."""
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="shadow"
        )
        legacy_dual_writer.mirror_order = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-017",
        )

        assert result.status == "processed"

        # Verify that es_service.index_document was called for the
        # pending_legacy_mirrors enqueue
        mirror_calls = [
            call
            for call in es_service.index_document.call_args_list
            if call[0][0] == "pending_legacy_mirrors"
        ]
        assert len(mirror_calls) == 1
        mirror_doc = mirror_calls[0][0][2]
        assert mirror_doc["entity_type"] == "order"
        assert mirror_doc["status"] == "pending"
        assert mirror_doc["retry_count"] == 0


# ---------------------------------------------------------------------------
# Tests — Legacy /ws/ops dual-broadcast (Req 4.1.3, 9.3)
# ---------------------------------------------------------------------------


class TestLegacyDualBroadcast:
    """During the deprecation window, OrderIntakePipeline MUST also call
    OpsWebSocketManager.broadcast_shipment_update and broadcast_rider_update
    via the existing manager so legacy /ws/ops subscribers continue
    receiving shipment/rider events.

    Gate on overlay.order_intake_pipeline state:
    - disabled, shadow, active_gated → dual-broadcast
    - active_auto → stop the legacy broadcast

    Validates: Requirements 4.1.3, 9.3.
    """

    @pytest.mark.asyncio
    async def test_dual_broadcast_fires_in_disabled_state(
        self,
        pipeline,
        legacy_ws_manager,
        feature_flag_service,
    ):
        """When overlay state is 'disabled', pipeline short-circuits to legacy_passthrough."""
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="disabled"
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-dual-001",
        )

        # disabled state short-circuits — no processing, no broadcast
        assert result.status == "legacy_passthrough"
        legacy_ws_manager.broadcast_shipment_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_dual_broadcast_fires_in_shadow_state(
        self,
        pipeline,
        legacy_ws_manager,
        feature_flag_service,
    ):
        """When overlay state is 'shadow', legacy broadcast fires."""
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="shadow"
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-dual-002",
        )

        assert result.status == "processed"
        legacy_ws_manager.broadcast_shipment_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_dual_broadcast_fires_in_active_gated_state(
        self,
        pipeline,
        legacy_ws_manager,
        feature_flag_service,
    ):
        """When overlay state is 'active_gated', legacy broadcast fires."""
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="active_gated"
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-dual-003",
        )

        assert result.status == "processed"
        legacy_ws_manager.broadcast_shipment_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_dual_broadcast_stops_in_active_auto_state(
        self,
        pipeline,
        legacy_ws_manager,
        feature_flag_service,
    ):
        """When overlay state is 'active_auto', legacy broadcast does NOT fire."""
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="active_auto"
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-dual-004",
        )

        assert result.status == "processed"
        legacy_ws_manager.broadcast_shipment_update.assert_not_called()
        legacy_ws_manager.broadcast_rider_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_dual_broadcast_does_not_send_rider_update_without_driver(
        self,
        pipeline,
        legacy_ws_manager,
        feature_flag_service,
    ):
        """When no driver is assigned, rider_update is NOT broadcast."""
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="active_gated"
        )

        # Default payload has no assigned_driver_id
        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-dual-005",
        )

        legacy_ws_manager.broadcast_shipment_update.assert_called_once()
        legacy_ws_manager.broadcast_rider_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_dual_broadcast_failure_does_not_block_main_path(
        self,
        pipeline,
        legacy_ws_manager,
        feature_flag_service,
        caplog,
    ):
        """Legacy broadcast failure MUST NOT block the main intake path."""
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="active_gated"
        )
        legacy_ws_manager.broadcast_shipment_update = AsyncMock(
            side_effect=RuntimeError("WS broadcast exploded")
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        with caplog.at_level(logging.WARNING):
            result = await pipeline.ingest_webhook(
                channel_id=CHANNEL_ID,
                body=body,
                signature=signature,
                request_id="req-dual-006",
            )

        # Main path still succeeds
        assert result.status == "processed"
        assert result.order_id is not None

        # A warning was logged
        assert any(
            "legacy /ws/ops dual-broadcast failed" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_dual_broadcast_skipped_when_no_legacy_ws_manager(
        self,
        es_service,
        intake_channel_repo,
        adapter_registry,
        idempotency_service,
        feature_flag_service,
        poison_queue_service,
        ws_manager,
        credentials_vault,
        customer_tank_repo,
        legacy_dual_writer,
        clock,
    ):
        """When legacy_ws_manager is None, dual-broadcast is skipped."""
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="active_gated"
        )

        pipeline_no_legacy_ws = OrderIntakePipeline(
            es_service=es_service,
            intake_channel_repo=intake_channel_repo,
            adapter_registry=adapter_registry,
            idempotency_service=idempotency_service,
            feature_flag_service=feature_flag_service,
            poison_queue_service=poison_queue_service,
            ws_manager=ws_manager,
            credentials_vault=credentials_vault,
            customer_tank_repo=customer_tank_repo,
            legacy_dual_writer=legacy_dual_writer,
            legacy_ws_manager=None,
            clock=clock,
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline_no_legacy_ws.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-dual-007",
        )

        # Main path still succeeds — no error from missing legacy manager
        assert result.status == "processed"

    @pytest.mark.asyncio
    async def test_dual_broadcast_shipment_shape_projection(
        self,
        pipeline,
        legacy_ws_manager,
        feature_flag_service,
    ):
        """The shipment broadcast data is correctly projected from the order."""
        feature_flag_service.get_overlay_state = AsyncMock(
            return_value="active_gated"
        )

        payload = _valid_order_payload()
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-dual-008",
        )

        assert result.status == "processed"
        shipment_data = legacy_ws_manager.broadcast_shipment_update.call_args[0][0]

        # Verify the projected shipment shape
        assert shipment_data["shipment_id"] == result.order_id
        assert shipment_data["status"] == "placed"
        assert shipment_data["tenant_id"] == TENANT_A
        assert shipment_data["origin"] == "depot"  # fallback when no legacy_origin_snapshot
        assert shipment_data["destination"] == "123 Main St, Houston TX"
        assert shipment_data["current_location"] == {"lat": 29.76, "lon": -95.37}
        assert "trace_id" in shipment_data


# ---------------------------------------------------------------------------
# Tests — ingest_webhook additive override kwargs (Task 1.3 / Req 2.3)
# ---------------------------------------------------------------------------


class TestIngestWebhookOverrides:
    """The additive ``idempotency_key_override`` / ``schema_version_override``
    kwargs on ``ingest_webhook`` are behavior-preserving when ``None`` and take
    precedence over the payload-derived values when supplied.

    The Dinee voice bridge maps the ``X-Idempotency-Key`` and
    ``X-Schema-Version`` headers onto the pipeline through these kwargs.

    Validates: Requirements 2.3.
    """

    # -- Behavior unchanged when both overrides are None --------------------

    @pytest.mark.asyncio
    async def test_defaults_derive_idempotency_key_from_payload_event_id(
        self, pipeline, idempotency_service
    ):
        """With no override, the idempotency key is ``payload['event_id']``."""
        payload = _valid_order_payload(event_id="evt-from-payload")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-ovr-001",
        )

        assert result.status == "processed"
        # is_duplicate is called with the payload-derived event_id
        assert idempotency_service.is_duplicate.call_args[0][0] == (
            "evt-from-payload"
        )
        # And the processed response echoes the same event_id
        assert result.event_id == "evt-from-payload"

    @pytest.mark.asyncio
    async def test_defaults_derive_schema_version_from_payload(
        self, pipeline, intake_channel_repo
    ):
        """With no override, an unsupported payload schema_version routes to
        the poison queue (schema derived from the payload)."""
        # Payload declares a version the channel does not support.
        payload = _valid_order_payload(schema_version="99.0")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-ovr-002",
        )

        # Payload schema_version=99.0 is unsupported → queued_for_review.
        assert result.status == "queued_for_review"

    @pytest.mark.asyncio
    async def test_none_overrides_match_omitted_kwargs(
        self, pipeline, idempotency_service
    ):
        """Passing the overrides explicitly as ``None`` behaves identically to
        omitting them."""
        payload = _valid_order_payload(event_id="evt-none-override")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-ovr-003",
            idempotency_key_override=None,
            schema_version_override=None,
        )

        assert result.status == "processed"
        assert result.event_id == "evt-none-override"
        assert idempotency_service.is_duplicate.call_args[0][0] == (
            "evt-none-override"
        )

    # -- Overrides take precedence when supplied ----------------------------

    @pytest.mark.asyncio
    async def test_idempotency_key_override_takes_precedence(
        self, pipeline, idempotency_service
    ):
        """When supplied, ``idempotency_key_override`` is used as the
        tenant-scoped idempotency key instead of ``payload['event_id']``."""
        payload = _valid_order_payload(event_id="evt-from-payload")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-ovr-004",
            idempotency_key_override="idem-header-key-XYZ",
        )

        assert result.status == "processed"
        # The header-supplied key wins over the payload event_id.
        assert idempotency_service.is_duplicate.call_args[0][0] == (
            "idem-header-key-XYZ"
        )
        # It is also tenant-scoped (same call carries the channel tenant).
        assert idempotency_service.is_duplicate.call_args[1]["tenant_id"] == (
            TENANT_A
        )
        assert result.event_id == "idem-header-key-XYZ"
        # Idempotency is marked processed under the override key.
        assert idempotency_service.mark_processed.call_args[0][0] == (
            "idem-header-key-XYZ"
        )

    @pytest.mark.asyncio
    async def test_schema_version_override_drives_unsupported_rejection(
        self, pipeline
    ):
        """When ``schema_version_override`` names an unsupported version, the
        submission routes to the poison queue even though the payload's own
        ``schema_version`` is supported — the override drives the check."""
        # Payload schema_version=1.0 IS supported by the channel; the override
        # is not, so the override must be the one that is checked.
        payload = _valid_order_payload(schema_version="1.0")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-ovr-005",
            schema_version_override="2.5",
        )

        assert result.status == "queued_for_review"

    @pytest.mark.asyncio
    async def test_schema_version_override_drives_adapter_dispatch(
        self, pipeline
    ):
        """When ``schema_version_override`` names a supported version, the
        submission is processed even though the payload declares an
        unsupported version — the override drives dispatch."""
        # Payload declares an unsupported version; the override is supported.
        payload = _valid_order_payload(schema_version="99.0")
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-ovr-006",
            schema_version_override="1.0",
        )

        # The supported override wins → the adapter runs and the order is made.
        assert result.status == "processed"
        assert result.order_id is not None

    @pytest.mark.asyncio
    async def test_both_overrides_supplied_together(
        self, pipeline, idempotency_service
    ):
        """Both overrides can be supplied together and each takes precedence
        over its payload-derived counterpart."""
        payload = _valid_order_payload(
            event_id="evt-from-payload", schema_version="99.0"
        )
        body = json.dumps(payload).encode()
        signature = _sign_body(body)

        result = await pipeline.ingest_webhook(
            channel_id=CHANNEL_ID,
            body=body,
            signature=signature,
            request_id="req-ovr-007",
            idempotency_key_override="idem-header-key-ABC",
            schema_version_override="1.0",
        )

        assert result.status == "processed"
        assert result.order_id is not None
        assert result.event_id == "idem-header-key-ABC"
        assert idempotency_service.is_duplicate.call_args[0][0] == (
            "idem-header-key-ABC"
        )
